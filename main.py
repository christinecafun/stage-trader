"""
Stage Trader — entry point.

Start TWS / IB Gateway first, then run:
    python main.py

Open http://127.0.0.1:8050 in your browser.
"""
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
    datefmt='%H:%M:%S',
)

import config
from store import DataStore
from ibkr.worker import IBWorker
from dashboard.app import create_app


def main():
    print(f"""
╔══════════════════════════════════════════════╗
║           Stage Trader  —  Starting          ║
╠══════════════════════════════════════════════╣
║  TWS:       {config.TWS_HOST}:{config.TWS_PORT:<30}║
║  Dashboard: http://{config.DASH_HOST}:{config.DASH_PORT:<22}║
║  Goal:      ${config.GOAL_AMOUNT:,}                          ║
╚══════════════════════════════════════════════╝
""")

    store = DataStore()

    worker = IBWorker(store)
    worker.start()
    print(f'IB worker thread started — connecting to TWS {config.TWS_HOST}:{config.TWS_PORT}')

    app = create_app(store, worker)
    app.run(
        host=config.DASH_HOST,
        port=config.DASH_PORT,
        debug=False,
    )


if __name__ == '__main__':
    main()
