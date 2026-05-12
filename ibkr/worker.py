"""
Background thread that owns the IB connection.
- Reconnects automatically on disconnect.
- Refreshes portfolio + stage data every REFRESH_SECONDS.
- Processes order commands from a thread-safe queue.
"""
import asyncio
import logging
import queue
import threading
import time
from datetime import datetime

from ib_insync import IB, Stock, util

import config
from analysis.indicators import compute_indicators
from analysis.stages import classify_stage, generate_recommendations
from ibkr.orders import place_bracket_buy, place_sell_market, get_live_price

logger = logging.getLogger(__name__)


class IBWorker(threading.Thread):
    def __init__(self, store):
        super().__init__(daemon=True, name='IBWorker')
        self.store = store
        self._order_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._order_results: dict = {}
        self.ib: IB | None = None
        self._bm_bars: list | None = None   # cached benchmark bars

    # ------------------------------------------------------------------
    # Public API (called from Dash thread)
    # ------------------------------------------------------------------

    def request_order(self, params: dict) -> str:
        """Submit an order request and return a tracking id."""
        import uuid
        order_id = str(uuid.uuid4())[:8]
        self._order_queue.put({'id': order_id, 'params': params})
        return order_id

    def get_order_result(self, order_id: str) -> dict | None:
        return self._order_results.get(order_id)

    # ------------------------------------------------------------------
    # Thread entry point
    # ------------------------------------------------------------------

    def run(self):
        # Each thread needs its own asyncio event loop for ib_insync.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        self.ib = IB()
        last_refresh = 0
        backoff = 2

        while True:
            if not self.ib.isConnected():
                self.store.update(connected=False, error=f'Connecting to TWS {config.TWS_HOST}:{config.TWS_PORT}...')
                try:
                    self.ib.connect(
                        config.TWS_HOST,
                        config.TWS_PORT,
                        clientId=config.TWS_CLIENT_ID,
                        timeout=20,
                        readonly=False,
                    )
                    self.store.update(connected=True, error=None)
                    backoff = 2
                    last_refresh = 0  # force immediate refresh after reconnect
                    logger.info('Connected to TWS')
                except Exception as exc:
                    self.store.update(connected=False, error=str(exc))
                    logger.warning(f'Connection failed: {exc} — retrying in {backoff}s')
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue

            # Process pending orders
            self._drain_order_queue()

            # Periodic data refresh
            now = time.time()
            if now - last_refresh >= config.REFRESH_SECONDS:
                try:
                    self._refresh_data()
                    last_refresh = time.time()
                except Exception as exc:
                    logger.error(f'Refresh error: {exc}', exc_info=True)
                    self.store.update(error=str(exc))

            # Let ib_insync process events for 1 second
            try:
                self.ib.sleep(1)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _drain_order_queue(self):
        while not self._order_queue.empty():
            item = self._order_queue.get_nowait()
            oid = item['id']
            try:
                result = self._execute_order(item['params'])
                self._order_results[oid] = {'status': 'ok', **result}
                self.store.append_order({'id': oid, 'status': 'ok', **result, 'ts': datetime.now().strftime('%H:%M:%S')})
            except Exception as exc:
                logger.error(f'Order error: {exc}', exc_info=True)
                self._order_results[oid] = {'status': 'error', 'error': str(exc)}
                self.store.append_order({'id': oid, 'status': 'error', 'error': str(exc), 'ts': datetime.now().strftime('%H:%M:%S')})

    def _execute_order(self, params: dict) -> dict:
        action = params['action']
        if action == 'BUY':
            return place_bracket_buy(
                self.ib,
                symbol=params['symbol'],
                usd_amount=params['usd_amount'],
                limit_price=params['limit_price'],
                profit_target_pct=params['profit_target_pct'],
                stop_loss_pct=params['stop_loss_pct'],
                use_fractional=params.get('use_fractional', config.USE_FRACTIONAL),
            )
        elif action == 'SELL':
            return place_sell_market(
                self.ib,
                symbol=params['symbol'],
                quantity=params['quantity'],
            )
        else:
            raise ValueError(f"Unknown order action: {action}")

    def _refresh_data(self):
        logger.info('Refreshing data...')

        # ---- Account summary ------------------------------------------
        account_vals = {}
        for item in self.ib.accountSummary():
            account_vals[item.tag] = item.value

        # ---- Benchmark bars (once per session) ------------------------
        if self._bm_bars is None:
            bm_contract = Stock(config.RS_BENCHMARK, 'SMART', 'USD')
            self.ib.qualifyContracts(bm_contract)
            bars = self.ib.reqHistoricalData(
                bm_contract,
                endDateTime='',
                durationStr=config.HISTORY_DURATION,
                barSizeSetting=config.HISTORY_BAR_SIZE,
                whatToShow='TRADES',
                useRTH=True,
            )
            self.ib.sleep(0.5)
            self._bm_bars = _bars_to_dicts(bars)

        # ---- Positions ------------------------------------------------
        raw_positions = self.ib.positions()
        positions = []
        stage_data = {}
        chart_bars = {}

        for pos in raw_positions:
            if pos.position == 0:
                continue

            sym = pos.contract.symbol
            contract = pos.contract

            try:
                bars = self.ib.reqHistoricalData(
                    contract,
                    endDateTime='',
                    durationStr=config.HISTORY_DURATION,
                    barSizeSetting=config.HISTORY_BAR_SIZE,
                    whatToShow='TRADES',
                    useRTH=True,
                )
                self.ib.sleep(0.4)  # stay within IBKR rate limits
            except Exception as exc:
                logger.warning(f'Historical data failed for {sym}: {exc}')
                bars = []

            bar_dicts = _bars_to_dicts(bars)
            chart_bars[sym] = bar_dicts

            if bar_dicts:
                df = compute_indicators(bar_dicts, self._bm_bars)
                sd = classify_stage(df)
                current_price = bar_dicts[-1]['close']
            else:
                sd = {'stage': 0, 'label': 'Unknown', 'color': '#6c757d', 'action': 'WATCH',
                      'ma150': None, 'ma50': None}
                # Fall back to live quote
                current_price = get_live_price(self.ib, contract) or pos.avgCost

            stage_data[sym] = sd

            mkt_value = pos.position * current_price
            avg_cost = pos.avgCost
            cost_basis = pos.position * avg_cost
            upnl = mkt_value - cost_basis

            positions.append({
                'symbol': sym,
                'position': pos.position,
                'avg_cost': round(avg_cost, 4),
                'current_price': round(current_price, 4),
                'market_value': round(mkt_value, 2),
                'unrealized_pnl': round(upnl, 2),
                'unrealized_pnl_pct': round((upnl / cost_basis * 100) if cost_basis else 0, 2),
                'exchange': getattr(contract, 'primaryExch', 'SMART') or 'SMART',
                'currency': contract.currency or 'USD',
            })

        recommendations = generate_recommendations(positions, stage_data)

        self.store.update(
            account=account_vals,
            positions=positions,
            stage_data=stage_data,
            chart_bars=chart_bars,
            recommendations=recommendations,
        )
        logger.info(f'Refreshed {len(positions)} positions')


def _bars_to_dicts(bars) -> list:
    """Convert ib_insync bar objects to plain dicts."""
    result = []
    for b in bars:
        result.append({
            'date': str(b.date),
            'open': b.open,
            'high': b.high,
            'low': b.low,
            'close': b.close,
            'volume': b.volume,
        })
    return result
