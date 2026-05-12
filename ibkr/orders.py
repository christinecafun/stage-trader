import logging
from ib_insync import Stock, Order, LimitOrder, StopOrder

logger = logging.getLogger(__name__)

# IBKR error codes that are informational only (not real failures)
_BENIGN_CODES = {2104, 2106, 2107, 2108, 2119, 2158, 10167}


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
    contract = Stock(symbol, exchange, currency)
    ib.qualifyContracts(contract)

    qty = calc_quantity(limit_price, usd_amount, use_fractional)
    if qty <= 0:
        raise ValueError(f"Calculated zero quantity for {symbol} at ${limit_price:.2f}")

    tp_price = round(limit_price * (1 + profit_target_pct), 2)
    sl_price = round(limit_price * (1 - stop_loss_pct), 2)

    # Capture IBKR error events during placement
    errors = []
    def _on_error(reqId, errorCode, errorString, contract, advancedOrderRejectJson=''):
        if errorCode not in _BENIGN_CODES:
            errors.append(f"[{errorCode}] {errorString}")
    ib.errorEvent += _on_error

    try:
        # Use ib_insync's bracketOrder — calls getReqId() correctly for each leg
        bracket = ib.bracketOrder(
            'BUY', qty,
            limitPrice=limit_price,
            takeProfitPrice=tp_price,
            stopLossPrice=sl_price,
        )
        # Override TIF on each leg
        bracket.parent.tif = 'DAY'
        bracket.takeProfit.tif = 'GTC'
        bracket.stopLoss.tif = 'GTC'

        trades = []
        for order in bracket:
            trade = ib.placeOrder(contract, order)
            trades.append(trade)

        # Wait up to 3 seconds for IBKR to acknowledge or reject
        ib.sleep(3)

    finally:
        ib.errorEvent -= _on_error

    if errors:
        raise RuntimeError(f"IBKR rejected order: {'; '.join(errors)}")

    # Check order status of the parent trade
    parent_trade = trades[0]
    status = parent_trade.orderStatus.status
    if status in ('Inactive', 'ApiCancelled', 'Cancelled'):
        raise RuntimeError(f"Order {status} — check TWS for details")

    logger.info(f"Bracket BUY {symbol}: qty={qty} @ ${limit_price} TP=${tp_price} SL=${sl_price} status={status}")

    return {
        'symbol': symbol,
        'action': 'BUY',
        'quantity': qty,
        'entry': limit_price,
        'take_profit': tp_price,
        'stop_loss': sl_price,
        'usd_value': round(qty * limit_price, 2),
        'ibkr_status': status,
    }


def place_sell_market(
    ib,
    symbol: str,
    quantity: float,
    exchange: str = 'SMART',
    currency: str = 'USD',
) -> dict:
    contract = Stock(symbol, exchange, currency)
    ib.qualifyContracts(contract)

    errors = []
    def _on_error(reqId, errorCode, errorString, contract, advancedOrderRejectJson=''):
        if errorCode not in _BENIGN_CODES:
            errors.append(f"[{errorCode}] {errorString}")
    ib.errorEvent += _on_error

    try:
        order = Order()
        order.action = 'SELL'
        order.orderType = 'MKT'
        order.totalQuantity = abs(quantity)
        order.transmit = True
        order.tif = 'DAY'
        trade = ib.placeOrder(contract, order)
        ib.sleep(2)
    finally:
        ib.errorEvent -= _on_error

    if errors:
        raise RuntimeError(f"IBKR rejected sell: {'; '.join(errors)}")

    logger.info(f"Market SELL {symbol}: qty={quantity} status={trade.orderStatus.status}")

    return {
        'symbol': symbol,
        'action': 'SELL',
        'quantity': abs(quantity),
        'entry': trade.orderStatus.avgFillPrice or 0,
        'ibkr_status': trade.orderStatus.status,
    }


def get_live_price(ib, contract) -> float | None:
    try:
        ticker = ib.reqMktData(contract, '', False, False)
        ib.sleep(2)
        ib.cancelMktData(contract)
        price = ticker.midpoint() or ticker.last or ticker.close
        return float(price) if price and price > 0 else None
    except Exception as e:
        logger.warning(f"Could not get live price: {e}")
        return None
