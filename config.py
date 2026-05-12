# IBKR TWS connection
TWS_HOST = '127.0.0.1'
TWS_PORT = 7496        # 7496 = live, 7497 = paper
TWS_CLIENT_ID = 1

# Dashboard
DASH_HOST = '127.0.0.1'
DASH_PORT = 8050
REFRESH_SECONDS = 30   # how often the background thread refreshes data

# Account goal
GOAL_AMOUNT = 10_000

# Default trade parameters
DEFAULT_TRADE_USD = 500     # position size in dollars
PROFIT_TARGET_PCT = 0.20   # 20% take-profit
STOP_LOSS_PCT = 0.08       # 8% stop-loss
USE_FRACTIONAL = True       # use fractional shares

# Stage analysis
MA_LONG = 150              # 30-week moving average (trading days)
MA_SHORT = 50              # 10-week moving average
VOL_PERIOD = 20            # volume average period
RS_BENCHMARK = 'SPY'       # relative strength benchmark

# History to fetch per symbol
HISTORY_DURATION = '1 Y'
HISTORY_BAR_SIZE = '1 day'
