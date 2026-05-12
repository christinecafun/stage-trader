import threading
from datetime import datetime


class DataStore:
    """Thread-safe shared state between the IB worker and the Dash dashboard."""

    def __init__(self):
        self._lock = threading.RLock()
        self.connected = False
        self.error = None
        self.account = {}          # keyed by IBKR account-summary tag
        self.positions = []        # list of position dicts
        self.stage_data = {}       # symbol -> stage-result dict
        self.chart_bars = {}       # symbol -> list of OHLCV dicts
        self.recommendations = []  # list of recommendation dicts
        self.recent_orders = []    # list of order-result dicts
        self.last_update = None

    # ------------------------------------------------------------------
    def update(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)
            self.last_update = datetime.now().strftime('%H:%M:%S')

    def append_order(self, order_dict):
        with self._lock:
            self.recent_orders.insert(0, order_dict)
            self.recent_orders = self.recent_orders[:50]  # keep last 50

    def get_snapshot(self):
        with self._lock:
            return {
                'connected': self.connected,
                'error': self.error,
                'account': dict(self.account),
                'positions': list(self.positions),
                'stage_data': dict(self.stage_data),
                'chart_bars': dict(self.chart_bars),
                'recommendations': list(self.recommendations),
                'recent_orders': list(self.recent_orders),
                'last_update': self.last_update,
            }
