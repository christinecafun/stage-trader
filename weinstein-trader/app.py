"""
Weinstein Trader — single-file Python web app.
Run with: python app.py
Then open: http://localhost:5050
"""
import os, json, threading, time, logging, webbrowser
from flask import Flask, jsonify, request, Response, stream_with_context, render_template_string
from flask_cors import CORS

logging.basicConfig(level=logging.WARNING)
app = Flask(__name__)
CORS(app)

# ── IBKR ─────────────────────────────────────────────────────────────────────
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
    def _connect():
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
        except Exception as e:
            with _ib_lock:
                _connecting = False
    threading.Thread(target=_connect, daemon=True).start()
    return None

threading.Thread(target=get_ib, daemon=True).start()

# ── Wishlist ──────────────────────────────────────────────────────────────────
WISHLIST_FILE = os.path.join(os.path.dirname(__file__), 'wishlist.json')

def load_wl():
    try:
        return json.load(open(WISHLIST_FILE)) if os.path.exists(WISHLIST_FILE) else []
    except: return []

def save_wl(items):
    json.dump(items, open(WISHLIST_FILE, 'w'), indent=2)

_wl = load_wl()
_wl_lock = threading.Lock()

# ── API routes ────────────────────────────────────────────────────────────────
@app.route('/api/status')
def status():
    ib = get_ib()
    return jsonify({'connected': bool(ib and ib.isConnected())})

@app.route('/api/portfolio')
def portfolio():
    ib = get_ib()
    if not ib or not ib.isConnected():
        return jsonify({'error': 'TWS not connected', 'positions': [], 'account': {}})
    try:
        account = {i.tag: i.value for i in ib.accountSummary()}
        raw = ib.positions()
        contracts = [p.contract for p in raw if p.position != 0]
        if contracts:
            try: ib.qualifyContracts(*contracts)
            except: pass
        tickers = {}
        if contracts:
            try:
                mkts = ib.reqTickers(*contracts)
                ib.sleep(1)
                tickers = {t.contract.symbol: t for t in mkts}
            except: pass
        positions = []
        for pos in raw:
            if pos.position == 0: continue
            sym = pos.contract.symbol
            avg = pos.avgCost or 0
            px = avg
            t = tickers.get(sym)
            if t:
                mp = t.marketPrice()
                if mp and mp > 0: px = mp
                elif t.close and t.close > 0: px = t.close
            qty = pos.position
            mv = qty * px
            cb = qty * avg
            upnl = mv - cb
            positions.append({
                'symbol': sym, 'qty': round(qty,6),
                'avg_cost': round(avg,4), 'current_price': round(px,4),
                'market_value': round(mv,2), 'unrealized_pnl': round(upnl,2),
                'unrealized_pnl_pct': round(upnl/cb*100 if cb else 0, 2),
                'is_fractional': qty != int(qty)
            })
        return jsonify({'positions': positions, 'account': account})
    except Exception as e:
        return jsonify({'error': str(e), 'positions': [], 'account': {}})

@app.route('/api/cash')
def cash():
    ib = get_ib()
    if not ib or not ib.isConnected():
        return jsonify({'settled_cash': 0, 'net_liquidation': 0})
    try:
        s = {i.tag: i.value for i in ib.accountSummary()}
        return jsonify({
            'settled_cash': float(s.get('AvailableFunds', s.get('TotalCashValue', 0)) or 0),
            'net_liquidation': float(s.get('NetLiquidation', 0) or 0)
        })
    except: return jsonify({'settled_cash': 0, 'net_liquidation': 0})

@app.route('/api/wishlist', methods=['GET'])
def get_wl(): return jsonify(_wl)

@app.route('/api/wishlist', methods=['POST'])
def add_wl():
    global _wl
    ticker = (request.get_json().get('ticker') or '').upper().strip()
    if not ticker: return jsonify({'error': 'ticker required'}), 400
    with _wl_lock:
        if any(w['ticker'] == ticker for w in _wl):
            return jsonify({'error': f'{ticker} already in list'}), 400
        item = {'ticker': ticker, 'added': time.strftime('%Y-%m-%d')}
        _wl.append(item); save_wl(_wl)
    return jsonify({'ok': True, 'item': item})

@app.route('/api/wishlist/<ticker>', methods=['DELETE'])
def del_wl(ticker):
    global _wl
    with _wl_lock:
        _wl = [w for w in _wl if w['ticker'] != ticker.upper()]
        save_wl(_wl)
    return jsonify({'ok': True})

# ── Claude helpers ────────────────────────────────────────────────────────────
import anthropic

def claude(): return anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

def fmt_positions(positions):
    if not positions: return 'No positions.'
    lines = []
    for p in positions:
        s = '+' if p['unrealized_pnl'] >= 0 else ''
        lines.append(f"  {p['symbol']}: {p['qty']} shares @ avg ${p['avg_cost']}, "
                     f"current ${p['current_price']}, mkt val ${p['market_value']}, "
                     f"P&L {s}{p['unrealized_pnl_pct']:.1f}% (${p['unrealized_pnl']:+.2f})"
                     + (' [FRACTIONAL]' if p.get('is_fractional') else ''))
    return '\n'.join(lines)

def sse(gen): return Response(stream_with_context(gen()), mimetype='text/event-stream',
                               headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

def stream_claude(system, user, max_tokens=4000, max_searches=12):
    def gen():
        try:
            with claude().messages.stream(
                model='claude-sonnet-4-20250514', max_tokens=max_tokens,
                system=system,
                tools=[{"type":"web_search_20250305","name":"web_search","max_uses":max_searches}],
                messages=[{"role":"user","content":user}]
            ) as s:
                for t in s.text_stream:
                    yield f"data: {json.dumps({'text':t})}\n\n"
            yield 'data: {"done":true}\n\n'
        except Exception as e:
            yield f"data: {json.dumps({'error':str(e)})}\n\n"
    return gen

@app.route('/api/ai/portfolio', methods=['POST'])
def ai_portfolio():
    positions = request.get_json().get('positions', [])
    if not positions: return jsonify({'error':'no positions'}), 400
    system = """You are an expert using Stan Weinstein's Stage Analysis.
Use web_search to get current news and prices before analysing each stock.
For each position:
## [TICKER] — Stage [1-4]: [Basing/Advancing/Topping/Declining]
**News:** 2-3 bullet points (from web search)
**Sentiment:** Bullish/Neutral/Bearish
**Recommendation:** Hold / Buy More / Sell — with reasoning
**Alternatives:** (only if stage 3-4 or losing money) suggest 1-2 better stocks same sector
End with ## PORTFOLIO SUMMARY"""
    user = f"Analyse my portfolio (CASH account, fractional shares OK):\n{fmt_positions(positions)}"
    return sse(stream_claude(system, user, 4000, 15))

@app.route('/api/ai/wishlist', methods=['POST'])
def ai_wishlist():
    tickers = request.get_json().get('tickers', [])
    if not tickers: return jsonify({'error':'no tickers'}), 400
    system = """You are an expert using Stan Weinstein's Stage Analysis.
Use web_search to research each ticker. For each:
## [TICKER] — Priority [1-10]/10 | Stage [1-4]
- News: 2 bullets
- Sentiment: Bullish/Neutral/Bearish
- Why buy/avoid now
- Priority score reasoning
End with ## RANKED LIST (highest to lowest priority)"""
    user = f"Analyse and rank these watchlist stocks for milestone growth ($10k→$100k):\n{', '.join(t['ticker'] for t in tickers)}\nCash account, fractional OK."
    return sse(stream_claude(system, user, 4000, 15))

@app.route('/api/ai/next-move', methods=['POST'])
def ai_next_move():
    d = request.get_json()
    cash_val = d.get('settled_cash', 0)
    netliq = d.get('net_liquidation', 0)
    positions = d.get('positions', [])
    system = """You are a pragmatic portfolio manager using Weinstein Stage Analysis.
Use web_search to find current Stage 2 breakout candidates.
Show all maths in plain English.
Structure:
## CASH ANALYSIS
- Cash available: $X
- Keep as dry powder: $X (why)
- Deploy: $X

## BUY SUGGESTIONS (2-3)
### BUY [TICKER] — $[amount]
- Why now (plain English, mention stage)
- Current price: $X (from web search)
- Maths: $[amount] ÷ $[price] = [N] shares
- Target: $X (+X%) by [timeframe]
- Stop loss: $X (-X%)

## PLAIN ENGLISH SUMMARY"""
    user = f"I have ${cash_val:,.2f} settled cash. Portfolio value: ${netliq:,.2f}.\nHoldings:\n{fmt_positions(positions)}\nCash account only. Fractional shares OK."
    return sse(stream_claude(system, user, 3000, 10))

@app.route('/api/ai/chat', methods=['POST'])
def ai_chat():
    d = request.get_json()
    messages = d.get('messages', [])
    positions = d.get('positions', [])
    system = f"""You are Weinstein Trader AI — a helpful trading assistant using Stan Weinstein's Stage Analysis.
Use web_search for current prices and news when asked.
LIVE PORTFOLIO:\n{fmt_positions(positions)}
Cash-only account. Be concise and direct."""
    def gen():
        try:
            with claude().messages.stream(
                model='claude-sonnet-4-20250514', max_tokens=2048,
                system=system,
                tools=[{"type":"web_search_20250305","name":"web_search","max_uses":8}],
                messages=messages
            ) as s:
                for t in s.text_stream:
                    yield f"data: {json.dumps({'text':t})}\n\n"
            yield 'data: {"done":true}\n\n'
        except Exception as e:
            yield f"data: {json.dumps({'error':str(e)})}\n\n"
    return sse(gen)

# ── Main page ─────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template_string(HTML)

# ── HTML (all-in-one) ─────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weinstein Trader</title>
<style>
:root{--bg:#1e1e1e;--bg2:#252525;--bg3:#2d2d2d;--bg4:#333;--border:#3a3a3a;--gold:#c9a84c;--gold-dim:#8a6f2f;--green:#4ade80;--red:#f87171;--blue:#60a5fa;--text:#e8e8e8;--dim:#888;--muted:#555}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,'Segoe UI',sans-serif;font-size:13px;overflow:hidden}
/* Header */
.header{background:var(--bg2);border-bottom:1px solid var(--border);padding:10px 16px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.logo{color:var(--gold);font-weight:700;font-size:15px;margin-right:6px}
.stat-card{background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:5px 12px;min-width:110px}
.stat-val{font-size:14px;font-weight:700;font-family:Consolas,monospace;color:var(--blue)}
.stat-lab{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.4px}
.milestone-card{min-width:190px}
.mile-row{display:flex;justify-content:space-between;margin-bottom:3px}
.prog-bg{height:5px;background:var(--bg4);border-radius:3px;overflow:hidden}
.prog-fill{height:100%;background:linear-gradient(90deg,var(--gold-dim),var(--gold));border-radius:3px;transition:width .5s}
.conn{display:flex;align-items:center;gap:5px;margin-left:auto}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot.on{background:var(--green);box-shadow:0 0 6px var(--green)}
.dot.trying{background:var(--gold);box-shadow:0 0 6px var(--gold);animation:pulse 1s infinite}
.dot.off{background:var(--red);box-shadow:0 0 6px var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.conn-lab{font-size:11px;color:var(--dim)}
.refresh-btn{background:transparent;border:1px solid var(--border);color:var(--dim);border-radius:6px;padding:4px 12px;cursor:pointer;font-size:12px}
.refresh-btn:hover{border-color:var(--gold);color:var(--gold)}
/* Tabs */
.tabs{display:flex;background:var(--bg2);border-bottom:1px solid var(--border);padding:0 16px}
.tab{background:transparent;border:none;border-bottom:2px solid transparent;color:var(--dim);padding:8px 18px;cursor:pointer;font-size:13px;font-family:inherit;font-weight:500}
.tab:hover{color:var(--text)}
.tab.active{color:var(--gold);border-bottom-color:var(--gold)}
/* Content */
.tab-content{height:calc(100vh - 96px);overflow:hidden}
.pane{display:none;height:100%}
.pane.active{display:flex;flex-direction:column;height:100%}
/* Two-panel layout */
.split{display:flex;gap:12px;padding:12px 16px;height:100%;overflow:hidden}
.left-panel{flex:1;display:flex;flex-direction:column;background:var(--bg2);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.right-panel{width:44%;display:flex;flex-direction:column;background:var(--bg2);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.panel-head{display:flex;align-items:center;justify-content:space-between;padding:9px 14px;border-bottom:1px solid var(--border);background:var(--bg3);flex-shrink:0}
.panel-head h2{font-size:12px;font-weight:600;color:var(--gold);text-transform:uppercase;letter-spacing:.5px}
.panel-sub{font-size:11px;color:var(--muted)}
.tbl-wrap{overflow-y:auto;flex:1}
table{width:100%;border-collapse:collapse;font-size:12px}
thead th{background:var(--bg3);color:var(--dim);font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;padding:6px 10px;text-align:left;border-bottom:1px solid var(--border);position:sticky;top:0;z-index:1}
th.r,td.r{text-align:right}
tbody tr{border-bottom:1px solid var(--border)}
tbody tr:hover{background:var(--bg3)}
td{padding:7px 10px;font-family:Consolas,monospace}
.sym{color:var(--blue);font-weight:600;font-size:13px}
.gain{color:var(--green)}
.loss{color:var(--red)}
.frac{display:inline-block;background:var(--gold-dim);color:var(--gold);font-size:9px;padding:1px 5px;border-radius:3px;font-weight:600}
.empty{text-align:center;color:var(--muted);padding:24px!important;font-family:inherit}
/* AI output */
.ai-out{flex:1;overflow-y:auto;padding:14px;font-size:13px;line-height:1.7}
.ai-out h2{color:var(--gold);font-size:14px;margin:14px 0 5px;border-bottom:1px solid var(--border);padding-bottom:3px}
.ai-out h3{color:var(--blue);font-size:13px;margin:10px 0 4px}
.ai-out strong{color:var(--text);font-weight:600}
.ai-out em{color:var(--dim)}
.ai-out ul{padding-left:18px;margin:4px 0}
.ai-out li{margin:2px 0}
.ai-out p{margin:5px 0}
.ai-out code{font-family:Consolas,monospace;background:var(--bg3);padding:1px 5px;border-radius:3px;font-size:11px}
.placeholder{color:var(--muted);font-style:italic}
.cursor::after{content:'▋';color:var(--gold);animation:blink .8s step-end infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
/* Buttons */
.gold-btn{background:linear-gradient(135deg,#8a6f2f,var(--gold));color:#1a1200;border:none;border-radius:6px;padding:6px 14px;font-size:12px;font-weight:700;cursor:pointer;font-family:inherit;white-space:nowrap}
.gold-btn:hover{filter:brightness(1.1)}
.gold-btn:disabled{opacity:.5;cursor:not-allowed;filter:none}
.gold-btn.sm{padding:4px 10px;font-size:11px}
.gold-btn.wide{width:100%;margin-top:10px;padding:10px;font-size:13px}
/* Add ticker row */
.add-row{display:flex;gap:6px;align-items:center}
.add-row input{background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 10px;font-size:12px;font-family:Consolas,monospace;width:110px;text-transform:uppercase}
.add-row input:focus{outline:none;border-color:var(--gold)}
.del-btn{background:transparent;border:none;color:var(--muted);cursor:pointer;font-size:14px;padding:2px 6px;border-radius:3px}
.del-btn:hover{color:var(--red)}
/* Next Move */
.nm-wrap{display:flex;flex-direction:column;gap:10px;padding:12px 16px;height:100%;overflow:hidden}
.cash-card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:14px 18px;flex-shrink:0}
.cash-row{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border)}
.cash-row:last-of-type{border-bottom:none}
.cash-lab{color:var(--dim);font-size:12px}
.cash-val{font-family:Consolas,monospace;font-weight:700;color:var(--blue);font-size:15px}
.ai-tall{flex:1;overflow-y:auto;padding:14px;font-size:13px;line-height:1.7;background:var(--bg2);border:1px solid var(--border);border-radius:10px}
.ai-tall h2{color:var(--gold);font-size:14px;margin:14px 0 5px;border-bottom:1px solid var(--border);padding-bottom:3px}
.ai-tall h3{color:var(--blue);font-size:13px;margin:10px 0 4px}
.ai-tall strong{color:var(--text);font-weight:600}
.ai-tall ul{padding-left:18px;margin:4px 0}
.ai-tall li{margin:2px 0}
.ai-tall p{margin:5px 0}
/* Chat */
.chat-wrap{display:flex;flex-direction:column;height:100%}
.chat-msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px}
.msg{display:flex}
.msg.user{justify-content:flex-end}
.bubble{max-width:75%;padding:10px 14px;border-radius:10px;line-height:1.6;font-size:13px}
.msg.user .bubble{background:var(--gold-dim);color:#fef3c7;border-bottom-right-radius:2px}
.msg.assistant .bubble{background:var(--bg3);border:1px solid var(--border);border-bottom-left-radius:2px}
.msg.assistant .bubble h2{color:var(--gold);font-size:13px;margin:8px 0 4px}
.msg.assistant .bubble h3{color:var(--blue);font-size:12px;margin:6px 0 3px}
.msg.assistant .bubble strong{color:var(--text)}
.chat-in{display:flex;gap:8px;padding:10px 16px;border-top:1px solid var(--border);background:var(--bg2);flex-shrink:0}
.chat-in textarea{flex:1;background:var(--bg3);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:8px 12px;font-size:13px;font-family:inherit;resize:none;line-height:1.5}
.chat-in textarea:focus{outline:none;border-color:var(--gold)}
/* Scrollbar */
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--gold-dim)}
/* Toast */
.toast{position:fixed;bottom:20px;right:20px;background:#3a1a1a;border:1px solid var(--red);color:var(--red);padding:10px 16px;border-radius:6px;font-size:12px;z-index:9999;animation:slidein .2s ease}
@keyframes slidein{from{transform:translateY(20px);opacity:0}to{transform:translateY(0);opacity:1}}
</style>
</head>
<body>

<!-- HEADER -->
<div class="header">
  <span class="logo">⬡ Weinstein Trader</span>
  <div class="stat-card"><div class="stat-val" id="s-netliq">—</div><div class="stat-lab">Net Liquidation</div></div>
  <div class="stat-card"><div class="stat-val" id="s-cash">—</div><div class="stat-lab">Settled Cash</div></div>
  <div class="stat-card"><div class="stat-val" id="s-pnl">—</div><div class="stat-lab">Unrealised P&L</div></div>
  <div class="stat-card milestone-card">
    <div class="mile-row"><span class="stat-lab">Milestone</span><span class="stat-val" id="s-mile" style="font-size:12px">—</span></div>
    <div class="prog-bg"><div class="prog-fill" id="mile-bar" style="width:0%"></div></div>
  </div>
  <div class="conn"><span class="dot off" id="conn-dot"></span><span class="conn-lab" id="conn-lab">Connecting…</span></div>
  <button class="refresh-btn" onclick="refresh()">⟳ Refresh</button>
</div>

<!-- TABS -->
<div class="tabs">
  <button class="tab active" onclick="tab('portfolio')">Portfolio</button>
  <button class="tab" onclick="tab('wishlist')">Wishlist</button>
  <button class="tab" onclick="tab('nextmove')">Next Move</button>
  <button class="tab" onclick="tab('chat')">Chat</button>
</div>

<!-- CONTENT -->
<div class="tab-content">

  <!-- PORTFOLIO -->
  <div id="pane-portfolio" class="pane active">
    <div class="split">
      <div class="left-panel">
        <div class="panel-head"><h2>Live Positions</h2><span class="panel-sub" id="pos-updated">Not loaded</span></div>
        <div class="tbl-wrap">
          <table><thead><tr><th>Ticker</th><th class="r">Qty</th><th class="r">Avg Cost</th><th class="r">Price</th><th class="r">Mkt Value</th><th class="r">P&L</th><th class="r">%</th><th>Frac</th></tr></thead>
          <tbody id="pos-body"><tr><td colspan="8" class="empty">Loading…</td></tr></tbody></table>
        </div>
      </div>
      <div class="right-panel">
        <div class="panel-head"><h2>AI Analysis</h2>
          <button class="gold-btn" id="btn-port" onclick="analysePortfolio()">✦ Analyse All</button>
        </div>
        <div class="ai-out" id="out-port"><p class="placeholder">Click "Analyse All" to get Claude's Stage analysis, news & recommendations for every position.</p></div>
      </div>
    </div>
  </div>

  <!-- WISHLIST -->
  <div id="pane-wishlist" class="pane">
    <div class="split">
      <div class="left-panel">
        <div class="panel-head"><h2>Watchlist</h2>
          <div class="add-row">
            <input id="wl-input" placeholder="e.g. NVDA" maxlength="10" onkeydown="if(event.key==='Enter')addTicker()">
            <button class="gold-btn sm" onclick="addTicker()">+ Add</button>
          </div>
        </div>
        <div class="tbl-wrap">
          <table><thead><tr><th>Ticker</th><th>Added</th><th></th></tr></thead>
          <tbody id="wl-body"><tr><td colspan="3" class="empty">No tickers yet — add some above.</td></tr></tbody></table>
        </div>
      </div>
      <div class="right-panel">
        <div class="panel-head"><h2>AI Analysis</h2>
          <button class="gold-btn" id="btn-wl" onclick="analyseWishlist()">✦ Rank by Priority</button>
        </div>
        <div class="ai-out" id="out-wl"><p class="placeholder">Add tickers then click "Rank by Priority" for Claude's ranked buying list.</p></div>
      </div>
    </div>
  </div>

  <!-- NEXT MOVE -->
  <div id="pane-nextmove" class="pane">
    <div class="nm-wrap">
      <div class="cash-card">
        <div class="cash-row"><span class="cash-lab">Settled Cash Available</span><span class="cash-val" id="nm-cash">—</span></div>
        <div class="cash-row"><span class="cash-lab">Portfolio Value</span><span class="cash-val" id="nm-liq">—</span></div>
        <button class="gold-btn wide" id="btn-nm" onclick="getNextMove()">✦ Get Claude's Recommendation</button>
      </div>
      <div class="ai-tall" id="out-nm"><p class="placeholder">Click the button — Claude will search for Stage 2 breakout candidates and show you exactly how to deploy your cash, with the maths in plain English.</p></div>
    </div>
  </div>

  <!-- CHAT -->
  <div id="pane-chat" class="pane">
    <div class="chat-wrap">
      <div class="chat-msgs" id="chat-msgs">
        <div class="msg assistant"><div class="bubble">Hi! I'm your Weinstein Trader AI. I have your live portfolio as context and can search the web for current data. Ask me anything about your positions or the market.</div></div>
      </div>
      <div class="chat-in">
        <textarea id="chat-txt" rows="3" placeholder="Ask about a stock, your portfolio, or trading strategy… (Enter to send)" onkeydown="chatKey(event)"></textarea>
        <button class="gold-btn" id="btn-chat" onclick="sendChat()">Send ↵</button>
      </div>
    </div>
  </div>

</div>

<script>
const API = '';
let portfolio = {positions:[], account:{}};
let cashData = {settled_cash:0, net_liquidation:0};
let chatHistory = [];
let streaming = false;

// ── Init ──────────────────────────────────────────────────────────────────────
window.onload = () => { checkConn(); refresh(); loadWishlist(); setInterval(checkConn, 10000); };

// ── Tabs ──────────────────────────────────────────────────────────────────────
function tab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', ['portfolio','wishlist','nextmove','chat'][i]===name));
  document.querySelectorAll('.pane').forEach(p => p.classList.toggle('active', p.id===`pane-${name}`));
  if(name==='nextmove') fillNextMove();
}

// ── Connection ────────────────────────────────────────────────────────────────
async function checkConn() {
  try {
    const d = await (await fetch('/api/status')).json();
    const dot = document.getElementById('conn-dot');
    const lab = document.getElementById('conn-lab');
    if(d.connected){ dot.className='dot on'; lab.textContent='TWS Connected'; }
    else { dot.className='dot trying'; lab.textContent='TWS Connecting…'; }
  } catch { document.getElementById('conn-dot').className='dot off'; document.getElementById('conn-lab').textContent='No connection'; }
}

// ── Refresh ───────────────────────────────────────────────────────────────────
async function refresh() {
  const btn = document.querySelector('.refresh-btn');
  btn.textContent='⟳ Loading…'; btn.disabled=true;
  try {
    [portfolio, cashData] = await Promise.all([
      fetch('/api/portfolio').then(r=>r.json()),
      fetch('/api/cash').then(r=>r.json())
    ]);
  } catch(e){ toast('Refresh failed: '+e.message); }
  renderTable(); renderStats(); renderMilestone();
  document.getElementById('pos-updated').textContent = 'Updated '+new Date().toLocaleTimeString();
  btn.textContent='⟳ Refresh'; btn.disabled=false;
}

// ── Stats ─────────────────────────────────────────────────────────────────────
function renderStats() {
  const acc = portfolio.account||{};
  const nl = parseFloat(acc.NetLiquidation||cashData.net_liquidation||0);
  const ca = parseFloat(cashData.settled_cash||acc.AvailableFunds||0);
  const pnl = (portfolio.positions||[]).reduce((s,p)=>s+p.unrealized_pnl,0);
  setText('s-netliq', fmt$(nl));
  setText('s-cash', fmt$(ca));
  const pe = document.getElementById('s-pnl');
  pe.textContent = (pnl>=0?'+':'')+fmt$(pnl);
  pe.style.color = pnl>=0?'var(--green)':'var(--red)';
}

function renderMilestone() {
  const nl = parseFloat((portfolio.account||{}).NetLiquidation||cashData.net_liquidation||0);
  const ms = [10000,20000,30000,40000,50000,75000,100000];
  let prev=0, next=ms[0];
  for(const m of ms){ if(nl<m){next=m;break;} prev=m; }
  const pct = Math.min(100,((nl-prev)/(next-prev))*100||0);
  document.getElementById('mile-bar').style.width = pct.toFixed(1)+'%';
  document.getElementById('s-mile').textContent = nl>=100000 ? '$'+Math.round(nl/1000)+'k' : '$'+fmtK(nl)+' → $'+fmtK(next);
}

// ── Portfolio table ───────────────────────────────────────────────────────────
function renderTable() {
  const tb = document.getElementById('pos-body');
  const pos = portfolio.positions||[];
  if(!pos.length){ tb.innerHTML=`<tr><td colspan="8" class="empty">${portfolio.error?'⚠ '+portfolio.error:'No positions — is TWS connected?'}</td></tr>`; return; }
  tb.innerHTML = pos.map(p=>{
    const gc = p.unrealized_pnl>=0?'gain':'loss';
    const sg = p.unrealized_pnl>=0?'+':'';
    return `<tr>
      <td class="sym">${p.symbol}</td>
      <td class="r">${fmtQty(p.qty)}</td>
      <td class="r">$${p.avg_cost.toFixed(4)}</td>
      <td class="r">$${p.current_price.toFixed(4)}</td>
      <td class="r">$${p.market_value.toFixed(2)}</td>
      <td class="r ${gc}">${sg}$${p.unrealized_pnl.toFixed(2)}</td>
      <td class="r ${gc}">${sg}${p.unrealized_pnl_pct.toFixed(2)}%</td>
      <td>${p.is_fractional?'<span class="frac">FRAC</span>':'—'}</td>
    </tr>`;
  }).join('');
}

// ── Portfolio AI ──────────────────────────────────────────────────────────────
async function analysePortfolio() {
  if(streaming) return;
  if(!(portfolio.positions||[]).length){ toast('No positions to analyse.'); return; }
  const btn=document.getElementById('btn-port'), out=document.getElementById('out-port');
  btn.disabled=true; btn.textContent='⏳ Analysing…';
  out.innerHTML='<p class="placeholder cursor">Claude is searching for news and analysing…</p>';
  streaming=true;
  await stream('/api/ai/portfolio',{positions:portfolio.positions},out);
  btn.disabled=false; btn.textContent='✦ Analyse All'; streaming=false;
}

// ── Wishlist ──────────────────────────────────────────────────────────────────
async function loadWishlist() {
  const items = await fetch('/api/wishlist').then(r=>r.json()).catch(()=>[]);
  renderWL(items);
}
function renderWL(items) {
  const tb=document.getElementById('wl-body');
  if(!items.length){ tb.innerHTML='<tr><td colspan="3" class="empty">No tickers yet — add some above.</td></tr>'; return; }
  tb.innerHTML=items.map(w=>`<tr><td class="sym">${w.ticker}</td><td>${w.added||''}</td><td><button class="del-btn" onclick="delTicker('${w.ticker}')">✕</button></td></tr>`).join('');
}
async function addTicker() {
  const inp=document.getElementById('wl-input'), ticker=inp.value.trim().toUpperCase();
  if(!ticker) return;
  const r=await fetch('/api/wishlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker})}).then(r=>r.json());
  if(r.error){ toast(r.error); return; }
  inp.value=''; loadWishlist();
}
async function delTicker(t) {
  await fetch(`/api/wishlist/${t}`,{method:'DELETE'}); loadWishlist();
}
async function analyseWishlist() {
  if(streaming) return;
  const items=await fetch('/api/wishlist').then(r=>r.json()).catch(()=>[]);
  if(!items.length){ toast('Add some tickers first.'); return; }
  const btn=document.getElementById('btn-wl'), out=document.getElementById('out-wl');
  btn.disabled=true; btn.textContent='⏳ Analysing…';
  out.innerHTML='<p class="placeholder cursor">Searching and ranking tickers…</p>';
  streaming=true;
  await stream('/api/ai/wishlist',{tickers:items},out);
  btn.disabled=false; btn.textContent='✦ Rank by Priority'; streaming=false;
}

// ── Next Move ─────────────────────────────────────────────────────────────────
function fillNextMove() {
  setText('nm-cash', fmt$(cashData.settled_cash||0));
  setText('nm-liq', fmt$(cashData.net_liquidation||0));
}
async function getNextMove() {
  if(streaming) return;
  const btn=document.getElementById('btn-nm'), out=document.getElementById('out-nm');
  btn.disabled=true; btn.textContent='⏳ Claude is thinking…';
  out.innerHTML='<p class="placeholder cursor">Searching for Stage 2 breakout candidates…</p>';
  streaming=true;
  await stream('/api/ai/next-move',{settled_cash:cashData.settled_cash||0,net_liquidation:cashData.net_liquidation||0,positions:portfolio.positions||[]},out);
  btn.disabled=false; btn.textContent="✦ Get Claude's Recommendation"; streaming=false;
}

// ── Chat ──────────────────────────────────────────────────────────────────────
function chatKey(e){ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();} }
async function sendChat() {
  if(streaming) return;
  const inp=document.getElementById('chat-txt'), text=inp.value.trim();
  if(!text) return;
  inp.value='';
  chatHistory.push({role:'user',content:text});
  addMsg('user',text);
  const aDiv=addMsg('assistant','');
  const bubble=aDiv.querySelector('.bubble');
  bubble.classList.add('cursor');
  document.getElementById('btn-chat').disabled=true;
  streaming=true;
  let full='';
  try {
    const res=await fetch('/api/ai/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages:chatHistory,positions:portfolio.positions||[]})});
    const reader=res.body.getReader(), dec=new TextDecoder();
    let buf='';
    while(true){
      const {done,value}=await reader.read(); if(done) break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n'); buf=lines.pop();
      for(const ln of lines){
        if(!ln.startsWith('data: ')) continue;
        try{ const d=JSON.parse(ln.slice(6)); if(d.text){full+=d.text;bubble.innerHTML=md(full);} } catch{}
      }
      scrollChat();
    }
  } catch(e){ full='⚠ '+e.message; bubble.textContent=full; }
  bubble.classList.remove('cursor');
  chatHistory.push({role:'assistant',content:full});
  streaming=false; document.getElementById('btn-chat').disabled=false; scrollChat();
}
function addMsg(role,text){
  const c=document.getElementById('chat-msgs'), d=document.createElement('div');
  d.className='msg '+role;
  d.innerHTML=`<div class="bubble">${role==='user'?esc(text):md(text)}</div>`;
  c.appendChild(d); scrollChat(); return d;
}
function scrollChat(){ const c=document.getElementById('chat-msgs'); c.scrollTop=c.scrollHeight; }

// ── SSE stream helper ─────────────────────────────────────────────────────────
async function stream(path, body, el) {
  let full=''; el.innerHTML='<div class="cursor"></div>';
  try {
    const res=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const reader=res.body.getReader(), dec=new TextDecoder(); let buf='';
    while(true){
      const {done,value}=await reader.read(); if(done) break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n'); buf=lines.pop();
      for(const ln of lines){
        if(!ln.startsWith('data: ')) continue;
        try{ const d=JSON.parse(ln.slice(6)); if(d.text){full+=d.text;el.innerHTML=md(full);el.scrollTop=el.scrollHeight;} if(d.error){full+='\n\n⚠ '+d.error;el.innerHTML=md(full);} }catch{}
      }
    }
  } catch(e){ el.innerHTML=`<p style="color:var(--red)">⚠ ${e.message}</p>`; }
}

// ── Markdown ──────────────────────────────────────────────────────────────────
function md(t){
  if(!t) return '';
  let h=esc(t);
  h=h.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  h=h.replace(/^## (.+)$/gm,'<h2>$1</h2>');
  h=h.replace(/^# (.+)$/gm,'<h2>$1</h2>');
  h=h.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
  h=h.replace(/\*(.+?)\*/g,'<em>$1</em>');
  h=h.replace(/`(.+?)`/g,'<code>$1</code>');
  h=h.replace(/^[-*] (.+)$/gm,'<li>$1</li>');
  h=h.replace(/(<li>[\s\S]*?<\/li>)/g,'<ul>$1</ul>');
  h=h.split(/\n{2,}/).map(b=>b.startsWith('<h')||b.startsWith('<ul')||b.startsWith('<li')?b:`<p>${b.replace(/\n/g,'<br>')}</p>`).join('');
  return h;
}

// ── Utils ─────────────────────────────────────────────────────────────────────
function fmt$(n){ return n||n===0?'$'+Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'—'; }
function fmtK(n){ return n>=1000?(n/1000).toFixed(0)+'k':n.toString(); }
function fmtQty(n){ return n===Math.floor(n)?n.toString():n.toFixed(4); }
function setText(id,t){ const e=document.getElementById(id); if(e) e.textContent=t; }
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function toast(msg){ const t=document.createElement('div'); t.className='toast'; t.textContent=msg; document.body.appendChild(t); setTimeout(()=>t.remove(),4000); }
</script>
</body>
</html>
"""

# ── Launch ────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import socket
    port = 5050
    # find a free port if 5050 is taken
    for p in [5050, 5051, 5052, 5053]:
        try:
            s = socket.socket(); s.bind(('127.0.0.1', p)); s.close(); port = p; break
        except: pass
    url = f'http://localhost:{port}'
    print(f'\n  Weinstein Trader starting...\n  Opening {url}\n')
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
