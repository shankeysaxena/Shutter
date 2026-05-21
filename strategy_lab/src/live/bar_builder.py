"""
Phase 6.1 — LiveBarBuilder.

Aggregates raw market ticks into 1-minute OHLCV bars. Emits a completed
LiveBar at each minute boundary. Designed for concurrent use: on_tick() is
called from a WebSocket callback thread; get_completed_bars() is polled by
the main processing loop.

Key design decisions:
  - Each instrument has its own in-progress bar state (no cross-contamination)
  - A tick that arrives after the current minute boundary immediately closes
    the prior bar and starts a new one
  - flush() force-closes the in-progress bar (called at session end 15:29)
  - Thread safety: on_tick() acquires a per-instrument lock before mutating
    state; get_completed_bars() drains the completed queue atomically
"""
from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Deque, List, Optional


@dataclass
class Tick:
    """One raw market tick from the data feed."""
    instrument: str
    timestamp: datetime         # IST, timezone-naive (consistent with rest of lab)
    last_price: float
    last_quantity: int
    volume: int                 # cumulative exchange volume for the session


@dataclass
class LiveBar:
    """One completed 1-minute OHLCV bar."""
    timestamp: datetime         # bar's minute boundary (09:15, 09:16, …)
    instrument: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    tick_count: int             # number of ticks that formed this bar
    is_complete: bool = True


@dataclass
class _InProgressBar:
    """Mutable state for the bar currently being built."""
    minute_ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume_start: int           # session volume at bar start
    volume_last: int            # session volume at last tick
    tick_count: int = 0


def _floor_to_minute(ts: datetime) -> datetime:
    """Strip seconds/microseconds to get the minute-boundary timestamp."""
    return ts.replace(second=0, microsecond=0)


class LiveBarBuilder:
    """
    Aggregates ticks per instrument into 1-minute OHLCV bars.

    Usage:
        builder = LiveBarBuilder()
        builder.on_tick(tick)           # call from websocket thread
        bars = builder.get_completed_bars()   # call from main loop
    """

    def __init__(self):
        self._in_progress: Dict[str, _InProgressBar] = {}
        self._completed:   Dict[str, Deque[LiveBar]]  = defaultdict(deque)
        self._locks:       Dict[str, threading.Lock]  = defaultdict(threading.Lock)

    def on_tick(self, tick: Tick) -> None:
        """
        Process one tick. If the tick crosses a minute boundary, the prior
        bar is closed and moved to the completed queue.
        """
        inst  = tick.instrument
        minute_ts = _floor_to_minute(tick.timestamp)

        with self._locks[inst]:
            current = self._in_progress.get(inst)

            # FIX #6: drop late/out-of-order ticks — they belong to a closed bar
            if current is not None and minute_ts < current.minute_ts:
                return   # silently discard; counter can be added here if needed

            if current is None:
                # First tick for this instrument today
                self._in_progress[inst] = _InProgressBar(
                    minute_ts=minute_ts,
                    open=tick.last_price, high=tick.last_price,
                    low=tick.last_price,  close=tick.last_price,
                    volume_start=tick.volume, volume_last=tick.volume,
                    tick_count=1,
                )
                return

            if minute_ts > current.minute_ts:
                # Crossed a boundary — close current bar, start a new one
                self._close_bar(inst, current)
                self._in_progress[inst] = _InProgressBar(
                    minute_ts=minute_ts,
                    open=tick.last_price, high=tick.last_price,
                    low=tick.last_price,  close=tick.last_price,
                    volume_start=current.volume_last,
                    volume_last=tick.volume,
                    tick_count=1,
                )
            else:
                # Same bar — update OHLCV
                current.high  = max(current.high, tick.last_price)
                current.low   = min(current.low,  tick.last_price)
                current.close = tick.last_price
                current.volume_last = tick.volume
                current.tick_count += 1

    def flush(self, instrument: Optional[str] = None) -> List[LiveBar]:
        """
        Force-close in-progress bars (call at session end or on disconnect).
        Returns the list of flushed bars.
        """
        instruments = [instrument] if instrument else list(self._in_progress.keys())
        flushed = []
        for inst in instruments:
            with self._locks[inst]:
                current = self._in_progress.pop(inst, None)
                if current and current.tick_count > 0:
                    bar = self._close_bar(inst, current)
                    flushed.append(bar)
        return flushed

    def get_completed_bars(self, instrument: Optional[str] = None) -> List[LiveBar]:
        """
        Drain and return all completed bars. Non-blocking.
        Call this from the main processing loop.
        """
        instruments = [instrument] if instrument else list(self._completed.keys())
        bars = []
        for inst in instruments:
            with self._locks[inst]:
                q = self._completed[inst]
                while q:
                    bars.append(q.popleft())
        return bars

    def reset(self, instrument: Optional[str] = None) -> None:
        """Clear all state for an instrument (or all instruments) at session start.

        FIX 2: iterate over the union of _in_progress and _completed keys so
        an instrument that has no in-progress bar but still has queued completed
        bars (e.g. from a very late flush) is also cleared.
        """
        if instrument:
            instruments = [instrument]
        else:
            instruments = list(set(self._in_progress.keys()) | set(self._completed.keys()))
        for inst in instruments:
            with self._locks[inst]:
                self._in_progress.pop(inst, None)
                self._completed[inst].clear()

    def _close_bar(self, instrument: str, bar: _InProgressBar) -> LiveBar:
        """Finalise a bar and enqueue it. Caller must hold the instrument lock."""
        bar_volume = max(0, bar.volume_last - bar.volume_start)
        completed  = LiveBar(
            timestamp=bar.minute_ts,
            instrument=instrument,
            open=bar.open, high=bar.high, low=bar.low, close=bar.close,
            volume=bar_volume,
            tick_count=bar.tick_count,
        )
        self._completed[instrument].append(completed)
        return completed
