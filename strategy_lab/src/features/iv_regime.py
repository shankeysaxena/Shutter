"""
IV regime feature.

Maintains per-underlying rolling buffer of ATM IV observations and answers
percentile queries against the trailing window. Owned and updated by the
strategy that needs it (not by BarEngine), mirroring how VWAPPullback owns
its own session state. Strategy.reset() should clear this state.
"""
from collections import deque
from datetime import datetime, timedelta
from typing import Deque, Dict, Optional, Tuple


class IVRegimeFeature:
    """
    Rolling N-day buffer of ATM IV per underlying with percentile lookup.

    Use:
        feat = IVRegimeFeature(lookback_days=60, min_observations=500)
        feat.update(instrument, timestamp, atm_iv)
        pct = feat.percentile(instrument, atm_iv)   # None if not warm
    """

    def __init__(self, lookback_days: int = 60, min_observations: int = 500):
        self.lookback_days = lookback_days
        self.min_observations = min_observations
        self._buffer: Dict[str, Deque[Tuple[datetime, float]]] = {}

    def update(self, instrument: str, timestamp: datetime, atm_iv: float) -> None:
        if atm_iv is None or atm_iv <= 0:
            return
        buf = self._buffer.setdefault(instrument, deque())
        buf.append((timestamp, atm_iv))
        cutoff = timestamp - timedelta(days=self.lookback_days)
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def percentile(self, instrument: str, atm_iv: float) -> Optional[float]:
        """Fraction of buffered observations strictly less than `atm_iv`. None if not warm."""
        buf = self._buffer.get(instrument)
        if not buf or len(buf) < self.min_observations:
            return None
        below = sum(1 for _, v in buf if v < atm_iv)
        return below / len(buf)

    def buffer_size(self, instrument: str) -> int:
        buf = self._buffer.get(instrument)
        return len(buf) if buf else 0

    def reset(self) -> None:
        self._buffer.clear()
