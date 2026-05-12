"""
Weinstein Trader — Flask backend
Endpoints for IBKR data and Claude AI analysis.
"""
import os
import json
import threading
import time
import logging
from flask import Flask, jsonify, request, Response, stream_with_context
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ── IBKR connection (lazy, non-fatal if TWS not running) ─────────────────────
_ib = None
_ib_lock = threading.Lock()
_connecting = False

def get_ib():
    global _ib, _connecting
    with _ib_lock:
        if _ib and _ib.isConnected():
            return _ib
        if _connecting:
            return None
        _connecting = True

    def connect():
        global _ib, _connecting
        try:
            import asyncio
            asyncio.set_event_loop(asyncio.new_event_loop())
            from ib_insync import IB
            ib = IB()
            ib.connect('127.0.0.1', 7496, clientId=10, timeout=10, readonly=True)
            with _ib_lock:
                _ib = ib
                _connecting = False
            logger.info('Connected to TWS')
        except Exception as e:
            with _ib_lock:
                _connecting = False
            logger.warning(f'TWS connection failed: {e}')

    t = threading.Thread(target=connect, daemon=True)
    t.start()
    return None

# Try connecting at startup
threading.Thread(target=get_ib, daemon=True).start()


# ── Wishlist store (in-memory + persist to JSON) ──────────────────────────────
WISHLIST_FILE = os.path.join(os.path.dirname(__file__), 'wishlist.json')

def load_wishlist():
    if os.path.exists(WISHLIST_FILE):
        try:
            with open(WISHLIST_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_wishlist(items):
    with open(WISHLIST_FILE, 'w') as f:
        json.dump(items, f, indent=2)

_wishlist = load_wishlist()
_wishlist_lock = threading.Lock()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/api/status')
def status():
    ib = get_ib()
    connected = bool(ib and ib.isConnected())
    return jsonify({'connected': connected, 'tws_port': 7496})


@app.route('/api/portfolio')
def portfolio():
    ib = get_ib()
    if not ib or not ib.isConnected():
        return jsonify({'error': 'TWS not connected', 'positions': [], 'account': {}})

    try:
        account_data = {}
        for item in ib.accountSummary():
            account_data[item.tag] = item.value

        positions = []
        raw = ib.positions()
        contracts = [p.contract for p in raw if p.position != 0]

        if contracts:
            try:
                ib.qualifyContracts(*contracts)
            except Exception:
                pass

        tickers = {}
        if contracts:
            try:
                mkts = ib.reqTickers(*contracts)
                ib.sleep(1)
                tickers = {t.contract.symbol: t for t in mkts}
            except Exception as e:
                logger.warning(f'Ticker request failed: {e}')

        for pos in raw:
            if pos.position == 0:
                continue
            sym = pos.contract.symbol
            avg_cost = pos.avgCost or 0

            current_price = avg_cost
            ticker = tickers.get(sym)
            if ticker:
                px = ticker.marketPrice()
                if px and px > 0:
                    current_price = px
                elif ticker.close and ticker.close > 0:
                    current_price = ticker.close

            qty = pos.position
            mkt_val = qty * current_price
            cost_basis = qty * avg_cost
            upnl = mkt_val - cost_basis
            upnl_pct = (upnl / cost_basis * 100) if cost_basis else 0
            is_fractional = (qty != int(qty))

            positions.append({
                'symbol': sym,
                'qty': round(qty, 6),
                'avg_cost': round(avg_cost, 4),
                'current_price': round(current_price, 4),
                'market_value': round(mkt_val, 2),
                'unrealized_pnl': round(upnl, 2),
                'unrealized_pnl_pct': round(upnl_pct, 2),
                'is_fractional': is_fractional,
            })

        return jsonify({'positions': positions, 'account': account_data})
    except Exception as e:
        logger.error(f'Portfolio error: {e}', exc_info=True)
        return jsonify({'error': str(e), 'positions': [], 'account': {}})


@app.route('/api/cash')
def cash():
    ib = get_ib()
    if not ib or not ib.isConnected():
        return jsonify({'error': 'TWS not connected', 'settled_cash': 0})
    try:
        summary = {item.tag: item.value for item in ib.accountSummary()}
        settled = float(summary.get('AvailableFunds', summary.get('TotalCashValue', 0)) or 0)
        total = float(summary.get('NetLiquidation', 0) or 0)
        return jsonify({'settled_cash': settled, 'net_liquidation': total, 'summary': summary})
    except Exception as e:
        return jsonify({'error': str(e), 'settled_cash': 0})


# ── Wishlist endpoints ────────────────────────────────────────────────────────

@app.route('/api/wishlist', methods=['GET'])
def get_wishlist():
    with _wishlist_lock:
        return jsonify(_wishlist)

@app.route('/api/wishlist', methods=['POST'])
def add_wishlist():
    global _wishlist
    data = request.get_json()
    ticker = (data.get('ticker') or '').upper().strip()
    if not ticker:
        return jsonify({'error': 'ticker required'}), 400
    with _wishlist_lock:
        if any(w['ticker'] == ticker for w in _wishlist):
            return jsonify({'error': f'{ticker} already in wishlist'}), 400
        item = {'ticker': ticker, 'notes': data.get('notes', ''), 'added': time.strftime('%Y-%m-%d')}
        _wishlist.append(item)
        save_wishlist(_wishlist)
    return jsonify({'ok': True, 'item': item})

@app.route('/api/wishlist/<ticker>', methods=['DELETE'])
def delete_wishlist(ticker):
    global _wishlist
    ticker = ticker.upper()
    with _wishlist_lock:
        _wishlist = [w for w in _wishlist if w['ticker'] != ticker]
        save_wishlist(_wishlist)
    return jsonify({'ok': True})


# ── Claude AI endpoints ───────────────────────────────────────────────────────

import anthropic

def get_claude():
    return anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))


def _build_portfolio_prompt(positions):
    lines = []
    for p in positions:
        pnl_sign = '+' if p['unrealized_pnl'] >= 0 else ''
        lines.append(
            f"  {p['symbol']}: {p['qty']} shares @ avg ${p['avg_cost']}, "
            f"current ${p['current_price']}, "
            f"market value ${p['market_value']}, "
            f"unrealised P&L {pnl_sign}{p['unrealized_pnl_pct']:.1f}% (${p['unrealized_pnl']:+.2f})"
            + (' [FRACTIONAL]' if p.get('is_fractional') else '')
        )
    return '\n'.join(lines)


@app.route('/api/ai/portfolio', methods=['POST'])
def ai_portfolio():
    """Stream Claude analysis of all portfolio positions."""
    data = request.get_json()
    positions = data.get('positions', [])

    if not positions:
        return jsonify({'error': 'no positions'}), 400

    portfolio_text = _build_portfolio_prompt(positions)

    system_prompt = """You are an expert stock analyst specialising in Stan Weinstein's Stage Analysis methodology.
You have access to a web_search tool — use it to look up current news and price data for each ticker.
Always use web search to get up-to-date information before analysing each stock.

For each position, provide:
1. STAGE (1-4): Stage 1=Basing, Stage 2=Advancing, Stage 3=Topping, Stage 4=Declining — with brief reasoning
2. NEWS: 2-3 bullet points of recent relevant news (use web_search)
3. SENTIMENT: Bullish/Neutral/Bearish with confidence %
4. RECOMMENDATION: Hold / Buy More / Sell — with specific reasoning
5. ALTERNATIVES: If underperforming (stage 3-4 or negative P&L), suggest 1-2 better alternatives in same sector
6. FRACTIONAL NOTE: If flagged as fractional, confirm broker supports it

Format each stock as:
## [TICKER] — Stage [N]: [Label]
**News:** ...
**Sentiment:** ...
**Recommendation:** ...
**Alternatives (if applicable):** ...

End with a brief PORTFOLIO SUMMARY covering overall health and top priority action."""

    user_prompt = f"""Analyse my current portfolio using Weinstein Stage Analysis.
Search for the latest news on each ticker before analysing.

Portfolio:
{portfolio_text}

This is a CASH account (no margin, no leverage). Fractional shares are supported."""

    def generate():
        try:
            client = get_claude()
            with client.messages.stream(
                model='claude-sonnet-4-20250514',
                max_tokens=4096,
                system=system_prompt,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 15}],
                messages=[{"role": "user", "content": user_prompt}]
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: {\"done\": true}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/ai/wishlist', methods=['POST'])
def ai_wishlist():
    """Stream Claude analysis of wishlist tickers, ranked by priority."""
    data = request.get_json()
    tickers = data.get('tickers', [])

    if not tickers:
        return jsonify({'error': 'no tickers'}), 400

    ticker_list = ', '.join(t['ticker'] for t in tickers)

    system_prompt = """You are an expert stock analyst using Stan Weinstein's Stage Analysis.
Use web_search to research each ticker before analysing. Always search for current price, news, and chart pattern.

For each ticker provide:
1. STAGE (1-4) with reasoning
2. Recent news (2 bullets, from web search)
3. Sentiment: Bullish/Neutral/Bearish
4. Target milestone: estimated % gain to next significant resistance
5. Priority Score: 1-10 for buying priority (10=highest priority)
6. Suggested position size: Small/Medium/Large based on risk/reward

Then rank ALL tickers by priority score from highest to lowest.
Format: ## [TICKER] — Priority [N]/10 | Stage [N]
End with: ## RANKED BUYING LIST (table format)"""

    user_prompt = f"""Analyse these watchlist stocks using Weinstein Stage Analysis and rank them by buying priority for milestone growth (targeting $10k → $20k → $50k → $100k portfolio milestones).

Tickers: {ticker_list}

Use web_search to get current data on each. Cash account, fractional shares supported."""

    def generate():
        try:
            client = get_claude()
            with client.messages.stream(
                model='claude-sonnet-4-20250514',
                max_tokens=4096,
                system=system_prompt,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 15}],
                messages=[{"role": "user", "content": user_prompt}]
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: {\"done\": true}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/ai/next-move', methods=['POST'])
def ai_next_move():
    """Stream Claude's cash deployment suggestions."""
    data = request.get_json()
    settled_cash = data.get('settled_cash', 0)
    net_liquidation = data.get('net_liquidation', 0)
    positions = data.get('positions', [])

    portfolio_text = _build_portfolio_prompt(positions) if positions else 'No current positions.'

    system_prompt = """You are a pragmatic portfolio manager using Stan Weinstein's Stage Analysis for a retail cash account.
Use web_search to find current buying opportunities before making suggestions.
Be specific with numbers and show your working in plain English — no jargon.

Structure your response EXACTLY as:
## CASH ANALYSIS
- Settled cash available: $X
- Recommended dry powder (keep): $X (explain why — market conditions, upcoming catalysts)
- Available to deploy: $X

## DEPLOYMENT PLAN
For each suggestion (give 2-3):
### BUY [TICKER] — $[amount]
- Why now: [plain English reason, reference stage]
- Entry price: ~$X (search for current price)
- Position size working: $[deploy amount] ÷ $[price] = [shares] shares
- Target: $X (+X%) at [timeframe]
- Stop loss: $X (-X%)
- Risk/Reward: [X:1]

## SUMMARY
One paragraph plain English summary of the plan."""

    user_prompt = f"""I have ${settled_cash:,.2f} in settled cash (portfolio value: ${net_liquidation:,.2f}).
Suggest how much to keep as dry powder vs deploy, with 2-3 specific buy suggestions.
Use web_search to find current Stage 2 breakout candidates.

Current holdings:
{portfolio_text}

Cash account only, no margin. Fractional shares OK."""

    def generate():
        try:
            client = get_claude()
            with client.messages.stream(
                model='claude-sonnet-4-20250514',
                max_tokens=3000,
                system=system_prompt,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}],
                messages=[{"role": "user", "content": user_prompt}]
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: {\"done\": true}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/ai/chat', methods=['POST'])
def ai_chat():
    """Stream Claude chat with portfolio context."""
    data = request.get_json()
    messages = data.get('messages', [])
    positions = data.get('positions', [])

    portfolio_context = _build_portfolio_prompt(positions) if positions else 'No portfolio data available.'

    system_prompt = f"""You are Weinstein Trader AI — a helpful trading assistant specialising in Stan Weinstein's Stage Analysis methodology.
You have live portfolio context injected below. Use web_search to look up current data when asked about specific stocks or market conditions.

LIVE PORTFOLIO CONTEXT:
{portfolio_context}

You help with:
- Stage analysis questions
- Portfolio review
- Buy/sell decisions
- Market research
- Risk management for a cash-only account

Be concise, direct, and show your reasoning. Use web_search for current prices and news."""

    def generate():
        try:
            client = get_claude()
            with client.messages.stream(
                model='claude-sonnet-4-20250514',
                max_tokens=2048,
                system=system_prompt,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
                messages=messages
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: {\"done\": true}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5050, debug=False, threaded=True)
