"""
Intraday ATR feature.

Computes a rolling true-range ATR within the session and uses it to
normalise the VWAP deviation — answering "how many volatility units is
price away from VWAP right now?"

Columns added to session DataFrame:
    intraday_atr        rolling 14-bar exponential ATR
    vwap_atr_distance   (close - vwap) / intraday_atr  (signed)
                        +2.0 means price is 2 ATR units above VWAP

Both are NaN for the first bars until the ATR warms up.
"""
import numpy as np
import pandas as pd

from src.features.base import Feature


class IntradayATRFeature(Feature):
    """Rolling intraday ATR with VWAP-deviation normalisation."""

    def __init__(self, period: int = 14):
        self.period = period

    def calculate(self, session_df: pd.DataFrame) -> pd.DataFrame:
        if session_df.empty:
            return session_df

        # True range: for intraday use prior bar's close as prev_close.
        # First bar: prev_close = open of that bar (no cross-session contamination).
        prev_close = session_df['close'].shift(1).fillna(session_df['open'])

        tr = pd.concat([
            session_df['high'] - session_df['low'],
            (session_df['high'] - prev_close).abs(),
            (session_df['low']  - prev_close).abs(),
        ], axis=1).max(axis=1)

        # Exponential smoothing — quicker warm-up than simple rolling mean
        session_df['intraday_atr'] = (
            tr.ewm(span=self.period, min_periods=self.period, adjust=False).mean()
        )

        # Signed VWAP deviation in ATR units
        if 'vwap' in session_df.columns:
            session_df['vwap_atr_distance'] = np.where(
                session_df['intraday_atr'].notna() & (session_df['intraday_atr'] > 0)
                & session_df['vwap'].notna(),
                (session_df['close'] - session_df['vwap']) / session_df['intraday_atr'],
                np.nan,
            )
        else:
            session_df['vwap_atr_distance'] = np.nan

        return session_df
