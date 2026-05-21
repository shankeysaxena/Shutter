"""
ReplayFeed — wraps historical session data in a bar-by-bar iterator.

The replay feed emits BarEvents one at a time, identical to how a live feed
would deliver finalized bars. This means the replay runtime can never see
future bars, which is the key correctness guarantee of the replay design.
"""
import pandas as pd
from typing import Dict, Iterator, Tuple
from datetime import date

from src.core.models import BarEvent
from src.core.utils import row_to_bar_event


class ReplayFeed:
    """
    Iterates over pre-processed historical sessions and yields BarEvents
    one bar at a time, in chronological order.

    Sessions must already have features computed (same as for backtest).
    The only difference from backtest is that bars are emitted through an
    iterator rather than accessed via a full dataframe at once.
    """

    def __init__(self, sessions: Dict[date, pd.DataFrame], instrument: str):
        self.sessions = sessions
        self.instrument = instrument

    def iter_sessions(self) -> Iterator[Tuple[date, Iterator[BarEvent]]]:
        """
        Yields (session_date, bar_iterator) for each session in date order.
        The bar_iterator emits one BarEvent per bar and is exhausted once the
        session ends — there is no way to look ahead.
        """
        for session_date in sorted(self.sessions.keys()):
            yield session_date, self._iter_bars(self.sessions[session_date])

    def _iter_bars(self, session_df: pd.DataFrame) -> Iterator[BarEvent]:
        for _, row in session_df.iterrows():
            yield row_to_bar_event(row, self.instrument, 'replay')
