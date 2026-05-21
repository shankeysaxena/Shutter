import pandas as pd
import numpy as np
from datetime import time
from typing import Optional
from src.features.base import Feature

class OpeningRangeFeature(Feature):
    """Calculates Opening Range based on time."""
    
    def __init__(self, start_time: time = time(9, 15), end_time: time = time(9, 30)):
        self.start_time = start_time
        self.end_time = end_time
        
    def calculate(self, session_df: pd.DataFrame) -> pd.DataFrame:
        if session_df.empty:
            return session_df
            
        session_df['or_high'] = np.nan
        session_df['or_low'] = np.nan
        session_df['or_width'] = np.nan
        session_df['or_ready'] = False
        
        # Extract time component
        time_series = session_df['timestamp'].dt.time
        
        # Identify the opening range block
        or_mask = (time_series >= self.start_time) & (time_series < self.end_time)
        or_df = session_df[or_mask]
        
        if not or_df.empty:
            or_high = or_df['high'].max()
            or_low = or_df['low'].min()
            or_width = (or_high - or_low) / or_low if or_low > 0 else 0
            
            # Broadcast to the rest of the session after the end time
            after_or_mask = time_series >= self.end_time
            
            session_df.loc[after_or_mask, 'or_high'] = or_high
            session_df.loc[after_or_mask, 'or_low'] = or_low
            session_df.loc[after_or_mask, 'or_width'] = or_width
            session_df.loc[after_or_mask, 'or_ready'] = True
            
        return session_df
