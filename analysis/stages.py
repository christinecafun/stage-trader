import pandas as pd
import numpy as np
from config import PROFIT_TARGET_PCT, STOP_LOSS_PCT

RISK_PCT = 0.02          # risk 2% of account per trade
MAX_POSITION_PCT = 0.20  # never put more than 20% of account in one position


def recommend_position_size(
    entry: float,
    stop: float | None,
    account_value: float,
    settled_cash: float,
) -> dict:
    """
    Fixed-fractional position sizing using the 2% risk rule.

    Returns a dict with recommended USD amount and a plain-English reason.
    """
    available = min(settled_cash, account_value * MAX_POSITION_PCT)
    available = max(available, 0)

    if not entry or entry <= 0:
        return {'usd': 0, 'qty': 0, 'reason': 'No price available.'}

    risk_dollars = account_value * RISK_PCT

    if stop and stop > 0 and stop < entry:
        stop_distance = entry - stop
        stop_pct = (stop_distance / entry) * 100
        ideal_qty = risk_dollars / stop_distance
        ideal_usd = ideal_qty * entry
    else:
        # No stop available — fall back to 5% assumed stop distance
        stop_pct = STOP_LOSS_PCT * 100
        stop_distance = entry * STOP_LOSS_PCT
        ideal_qty = risk_dollars / stop_distance
        ideal_usd = ideal_qty * entry

    # Apply caps
    usd = round(min(ideal_usd, available), 2)
    qty = round(usd / entry, 4)

    cap_note = ''
    if usd < ideal_usd:
        if settled_cash < account_value * MAX_POSITION_PCT:
            cap_note = ' — limited by settled cash'
        else:
            cap_note = f' — capped at {MAX_POSITION_PCT*100:.0f}% max position'

    reason = (
        f"2% risk rule: risking ${risk_dollars:.2f} "
        f"({RISK_PCT*100:.0f}% of ${account_value:,.2f}) "
        f"with stop {stop_pct:.1f}% below entry{cap_note}"
    )

    return {
        'usd': usd,
        'qty': qty,
        'risk_dollars': round(risk_dollars, 2),
        'stop_pct': round(stop_pct, 1),
        'reason': reason,
    }


STAGE_LABELS = {
    0: 'Unknown',
    1: 'Stage 1 — Basing',
    2: 'Stage 2 — Advancing',
    3: 'Stage 3 — Topping',
    4: 'Stage 4 — Declining',
}

STAGE_COLORS = {
    0: '#6c757d',
    1: '#adb5bd',
    2: '#28a745',
    3: '#fd7e14',
    4: '#dc3545',
}

STAGE_ACTIONS = {
    0: 'WATCH',
    1: 'WATCH',
    2: 'HOLD / BUY',
    3: 'REDUCE',
    4: 'SELL',
}


def classify_stage(df: pd.DataFrame) -> dict:
    """
    Classify the most recent bar's Weinstein stage.
    Returns a dict with stage, label, color, key metrics, and suggested trade levels.
    """
    if df.empty or df['ma150'].isna().all():
        return _empty_result()

    last = df.iloc[-1]
    price = last['close']
    ma150 = last['ma150']
    ma50 = last['ma50']
    vol = last['volume']
    vol_ma = last['vol_ma']
    slope = last['ma150_slope']

    if pd.isna(ma150):
        return _empty_result()

    above_150 = price > ma150
    above_50 = price > ma50
    ma_rising = slope > 0.001
    ma_falling = slope < -0.001
    vol_expanding = (not pd.isna(vol_ma)) and (vol > vol_ma * 1.15)

    if above_150 and ma_rising:
        stage = 2
    elif not above_150 and ma_falling:
        stage = 4
    elif above_150 and not ma_rising and not ma_falling:
        stage = 3
    elif not ma_rising and not ma_falling and price >= ma150 * 0.95:
        stage = 1
    elif not above_150 and not ma_falling:
        stage = 1
    else:
        stage = 4

    # Measured-move profit target: depth of the 60-day base projected from current price
    recent_60_high = df['high'].tail(60).max()
    recent_60_low = df['low'].tail(60).min()
    base_depth = recent_60_high - recent_60_low
    measured_target = round(price + base_depth, 2)

    # Default percentage targets as fallback
    pct_target = round(price * (1 + PROFIT_TARGET_PCT), 2)
    stop = round(ma150 * 0.97, 2)  # 3% below 30-wk MA

    return {
        'stage': stage,
        'label': STAGE_LABELS[stage],
        'color': STAGE_COLORS[stage],
        'action': STAGE_ACTIONS[stage],
        'price': price,
        'ma150': round(ma150, 2),
        'ma50': round(ma50, 2) if not pd.isna(ma50) else None,
        'ma150_slope': round(slope * 100, 3) if not pd.isna(slope) else None,
        'vol_expanding': vol_expanding,
        'high_52w': last['high_52w'] if not pd.isna(last.get('high_52w', float('nan'))) else None,
        'low_52w': last['low_52w'] if not pd.isna(last.get('low_52w', float('nan'))) else None,
        'rs': round(last['rs'], 1) if not pd.isna(last.get('rs', float('nan'))) else None,
        'measured_target': measured_target,
        'pct_target': pct_target,
        'stop': stop,
    }


def generate_recommendations(positions: list, stage_data: dict) -> list:
    """
    Return a list of recommended actions sorted by urgency.
    Urgency order: SELL (4) > REDUCE (3) > BUY/HOLD (2) > WATCH (1).
    """
    recs = []
    urgency = {4: 0, 3: 1, 2: 2, 1: 3, 0: 4}

    for pos in positions:
        sym = pos['symbol']
        sd = stage_data.get(sym)
        if not sd or sd['stage'] == 0:
            continue

        stage = sd['stage']
        price = pos['current_price']

        if stage == 2:
            reason = 'Advancing — hold or add on pullback to MA'
        elif stage == 3:
            reason = 'Topping — trim position, protect profits'
        elif stage == 4:
            reason = 'Declining — exit to stop losses and preserve capital'
        else:
            reason = 'Basing — watch for Stage 2 breakout'

        recs.append({
            'symbol': sym,
            'stage': stage,
            'label': sd['label'],
            'color': sd['color'],
            'action': sd['action'],
            'reason': reason,
            'current_price': price,
            'entry': price,
            'target': sd['measured_target'] if stage == 2 else None,
            'stop': sd['stop'] if stage in (2, 3) else None,
            'ma150': sd['ma150'],
            'position': pos['position'],
            'market_value': pos['market_value'],
        })

    recs.sort(key=lambda r: urgency.get(r['stage'], 99))
    return recs


def _empty_result():
    return {
        'stage': 0,
        'label': STAGE_LABELS[0],
        'color': STAGE_COLORS[0],
        'action': 'WATCH',
        'price': None,
        'ma150': None,
        'ma50': None,
        'ma150_slope': None,
        'vol_expanding': False,
        'high_52w': None,
        'low_52w': None,
        'rs': None,
        'measured_target': None,
        'pct_target': None,
        'stop': None,
    }
