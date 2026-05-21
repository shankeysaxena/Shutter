from abc import ABC, abstractmethod
import pandas as pd

class Feature(ABC):
    """Base interface for computing features on a session dataframe."""
    
    @abstractmethod
    def calculate(self, session_df: pd.DataFrame) -> pd.DataFrame:
        """
        Takes a session dataframe with raw OHLCV.
        Adds calculated feature columns and returns mutated dataframe.
        """
        pass
