import logging
from ib_insync import Stock, Order

logger = logging.getLogger(__name__)


def calc_quantity(price: float, usd_amount: float, use_fractional: bool = True) -> float:
    qty = usd_amount / price
    if use_fractional:
        return round(qty, 4)
    return max(1, int(qty))


def place_bracket_buy(
    ib,
    symbol: str,
    usd_amount: float,
    limit_price: float,
    profit_target_pct: float,
    stop_loss_pct: float,
    use_fractional: bool = True,
    exchange: str = 'SMART',
    currency: str = 'USD',
) -> dict:
    """
    Place a bracket BUY with an attached take-profit limit and stop-loss.
    Uses fractional share quantities when enabled.
    Returns a summary dict.
    """
    contract = Stock(symbol, exchange, currency)
    ib.qualifyContracts(contract)

    qty = calc_quantity(limit_price, usd_amount, use_fractional)
    if qty <= 0:
        raise ValueError(f"Calculated zero quantity for {symbol} at ${limit_price:.2f}")

    tp_price = round(limit_price * (1 + profit_target_pct), 2)
    sl_price = round(limit_price * (1 - stop_loss_pct), 2)

    # Build parent + attached orders manually so we can support fractional qty
    next_id = ib.client.getReqId()

    parent = Order()
    parent.orderId = next_id
    parent.action = 'BUY'
    parent.orderType = 'LMT'
    parent.totalQuantity = qty
    parent.lmtPrice = limit_price
    parent.transmit = False
    parent.tif = 'DAY'

    take_profit = Order()
    take_profit.orderId = next_id + 1
    take_profit.parentId = next_id
    take_profit.action = 'SELL'
    take_profit.orderType = 'LMT'
    take_profit.totalQuantity = qty
    take_profit.lmtPrice = tp_price
    take_profit.transmit = False
    take_profit.tif = 'GTC'

    stop_loss = Order()
    stop_loss.orderId = next_id + 2
    stop_loss.parentId = next_id
    stop_loss.action = 'SELL'
    stop_loss.orderType = 'STP'
    stop_loss.totalQuantity = qty
    stop_loss.auxPrice = sl_price
    stop_loss.transmit = True  # transmits all three at once
    stop_loss.tif = 'GTC'

    trades = []
    for order in (parent, take_profit, stop_loss):
        trade = ib.placeOrder(contract, order)
        trades.append(trade)

    logger.info(
        f"Bracket BUY {symbol}: qty={qty} entry=${limit_price} TP=${tp_price} SL=${sl_price}"
    )

    return {
        'symbol': symbol,
        'action': 'BUY',
        'quantity': qty,
        'entry': limit_price,
        'take_profit': tp_price,
        'stop_loss': sl_price,
        'usd_value': round(qty * limit_price, 2),
        'order_ids': [next_id, next_id + 1, next_id + 2],
    }


def place_sell_market(
    ib,
    symbol: str,
    quantity: float,
    exchange: str = 'SMART',
    currency: str = 'USD',
) -> dict:
    """Place a market SELL to close / reduce a position."""
    contract = Stock(symbol, exchange, currency)
    ib.qualifyContracts(contract)

    order = Order()
    order.action = 'SELL'
    order.orderType = 'MKT'
    order.totalQuantity = abs(quantity)
    order.transmit = True
    order.tif = 'DAY'

    trade = ib.placeOrder(contract, order)
    logger.info(f"Market SELL {symbol}: qty={quantity}")

    return {
        'symbol': symbol,
        'action': 'SELL',
        'quantity': abs(quantity),
        'order_type': 'MKT',
    }


def get_live_price(ib, contract) -> float | None:
    """Fetch a live mid-price snapshot. Returns None on failure."""
    try:
        ticker = ib.reqMktData(contract, '', False, False)
        ib.sleep(2)
        ib.cancelMktData(contract)
        price = ticker.midpoint() or ticker.last or ticker.close
        return float(price) if price and price > 0 else None
    except Exception as e:
        logger.warning(f"Could not get live price: {e}")
        return None
