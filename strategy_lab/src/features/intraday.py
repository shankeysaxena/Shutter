import pandas as pd
from src.features.base import Feature


class IntradaySessionFeature(Feature):
    """
    Computes rolling intraday session statistics that track what is known
    up to and including each bar — no lookahead.

    Fills in FeatureSnapshot fields:
    - session_high_so_far: cumulative max of bar highs
    - session_low_so_far:  cumulative min of bar lows
    """

    def calculate(self, session_df: pd.DataFrame) -> pd.DataFrame:
        if session_df.empty:
            return session_df

        session_df['session_high_so_far'] = session_df['high'].cummax()
        session_df['session_low_so_far'] = session_df['low'].cummin()

        return session_df
