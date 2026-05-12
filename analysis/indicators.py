import pandas as pd
import numpy as np
from config import MA_LONG, MA_SHORT, VOL_PERIOD


def compute_indicators(bars: list, benchmark_bars: list | None = None) -> pd.DataFrame:
    """
    Accepts raw IBKR bar dicts, returns a DataFrame enriched with:
      ma150, ma50, vol_ma, ma150_slope, relative strength.
    """
    df = pd.DataFrame(bars)
    if df.empty:
        return df

    df = df.sort_values('date').reset_index(drop=True)

    df['ma150'] = df['close'].rolling(MA_LONG, min_periods=80).mean()
    df['ma50'] = df['close'].rolling(MA_SHORT, min_periods=30).mean()
    df['vol_ma'] = df['volume'].rolling(VOL_PERIOD, min_periods=10).mean()

    # 10-day rate-of-change on 150-day MA → slope signal
    df['ma150_slope'] = df['ma150'].pct_change(10)

    # 52-week range
    df['high_52w'] = df['high'].rolling(252, min_periods=100).max()
    df['low_52w'] = df['low'].rolling(252, min_periods=100).min()

    # Relative strength vs benchmark (simple ratio, normalised to 100 at start)
    if benchmark_bars:
        bm = pd.DataFrame(benchmark_bars).sort_values('date')[['date', 'close']]
        bm = bm.rename(columns={'close': 'bm_close'})
        merged = df.merge(bm, on='date', how='left').ffill()
        first_ratio = (merged['close'].iloc[0] / merged['bm_close'].iloc[0])
        merged['rs'] = (merged['close'] / merged['bm_close']) / first_ratio * 100
        df['rs'] = merged['rs'].values
    else:
        df['rs'] = np.nan

    return df
