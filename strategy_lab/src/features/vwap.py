import pandas as pd
import numpy as np
from src.features.base import Feature

class VWAPFeature(Feature):
    """
    Calculates session VWAP and related metrics.

    For instruments that report zero volume (indices: NIFTY, BANKNIFTY),
    falls back to time-weighted VWAP (uniform weight per bar = simple
    cumulative average of typical prices). This is the standard approach
    for index data where volume is not meaningful.
    """
    def calculate(self, session_df: pd.DataFrame) -> pd.DataFrame:
        if session_df.empty:
            return session_df

        typical_price = (session_df['high'] + session_df['low'] + session_df['close']) / 3
        vol = pd.to_numeric(session_df['volume'], errors='coerce').fillna(0)

        cum_vol = vol.cumsum()
        cum_vol_price = (typical_price * vol).cumsum()

        # Volume-weighted VWAP where volume > 0; otherwise time-weighted fallback.
        # Zero-volume instruments (index data from Kite) always use the fallback.
        has_volume = cum_vol > 0
        if has_volume.any():
            vwap_values = np.where(has_volume, cum_vol_price / cum_vol, np.nan)
        else:
            # Time-weighted: cumulative mean of typical price (uniform bar weight)
            vwap_values = typical_price.expanding().mean().values

        session_df['vwap'] = vwap_values

        # VWAP distance % = (close - vwap) / vwap
        session_df['vwap_distance'] = np.where(
            session_df['vwap'].notna() & (session_df['vwap'] > 0),
            (session_df['close'] - session_df['vwap']) / session_df['vwap'],
            np.nan
        )

        session_df['above_vwap'] = session_df['close'] > session_df['vwap']
        session_df['below_vwap'] = session_df['close'] < session_df['vwap']

        return session_df
