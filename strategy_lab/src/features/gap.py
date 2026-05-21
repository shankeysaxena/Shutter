import pandas as pd
import numpy as np
from src.features.base import Feature

class GapFeature(Feature):
    """Calculates inter-session gap features."""
    
    def calculate(self, session_df: pd.DataFrame) -> pd.DataFrame:
        if session_df.empty:
            return session_df
            
        session_df['gap_pct'] = np.nan
        session_df['gap_direction'] = "NONE"
        
        # Need prior_close which should be added by Sessionizer
        if 'prior_close' in session_df.columns and not session_df['prior_close'].isna().all():
            prior_close = session_df['prior_close'].iloc[0]
            first_open = session_df['open'].iloc[0]
            
            if pd.notna(prior_close) and prior_close > 0:
                gap_pct = (first_open - prior_close) / prior_close
                session_df['gap_pct'] = gap_pct
                
                direction = "UP" if gap_pct > 0 else ("DOWN" if gap_pct < 0 else "FLAT")
                session_df['gap_direction'] = direction
                
        return session_df
