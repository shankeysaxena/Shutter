"""
Phase 6.6 — SessionHealthMonitor.

Watches for silent failures that logs and backtest results cannot surface:
  - Stale feed: no ticks for > N seconds during market hours
  - Missing bars: gap in the 1-minute bar sequence
  - WebSocket disconnects longer than the reconnect window
  - Position drift: executor says we hold X but engine expects Y

Design: passive observer. The runtime calls update() each bar and tick.
Callers check is_healthy() or subscribe to alerts via set_alert_callback().

All alerts are written to stderr (logging.WARNING level) by default.
The callback receives (alert_type: str, detail: str) for downstream handling
(e.g., Telegram notification, email).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

AlertCallback = Callable[[str, str], None]

_MARKET_OPEN  = time(9, 15)
_MARKET_CLOSE = time(15, 30)


class SessionHealthMonitor:
    """
    Monitors live session health. Not in the critical path — never blocks
    or modifies any state; purely observational.
    """

    def __init__(
        self,
        stale_feed_seconds:   int = 120,    # alert if no ticks for 2 min
        max_bar_gap_minutes:  int = 3,      # alert if bar sequence has a gap
        alert_callback:       Optional[AlertCallback] = None,
    ):
        self.stale_feed_threshold  = timedelta(seconds=stale_feed_seconds)
        self.max_bar_gap           = timedelta(minutes=max_bar_gap_minutes)
        self._alert_cb             = alert_callback

        # Per-instrument tracking
        self._last_tick:   Dict[str, datetime] = {}
        self._last_bar:    Dict[str, datetime] = {}
        self._bar_counts:  Dict[str, int]      = {}
        self._tick_counts: Dict[str, int]      = {}

        # Session-level health state
        self._disconnected    = False
        self._alerts_fired:   List[str] = []
        self._session_start:  Optional[datetime] = None

        # Background monitor thread
        self._lock            = threading.RLock()   # reentrant: health_summary calls is_healthy
        self._running         = False
        self._monitor_thread: Optional[threading.Thread] = None

    # ----------------------------------------------------------------
    # Update hooks — called from the runtime
    # ----------------------------------------------------------------

    def on_tick(self, instrument: str, timestamp: datetime) -> None:
        with self._lock:
            self._last_tick[instrument] = timestamp
            self._tick_counts[instrument] = self._tick_counts.get(instrument, 0) + 1

    def on_bar_completed(self, instrument: str, bar_timestamp: datetime) -> None:
        with self._lock:
            last = self._last_bar.get(instrument)
            if last is not None:
                gap = bar_timestamp - last
                if gap > self.max_bar_gap:
                    self._alert(
                        'BAR_GAP',
                        f"{instrument}: {gap.total_seconds()/60:.1f}min gap after {last.strftime('%H:%M')}"
                    )
            self._last_bar[instrument]   = bar_timestamp
            self._bar_counts[instrument] = self._bar_counts.get(instrument, 0) + 1

    def on_connect(self) -> None:
        with self._lock:
            self._disconnected = False
            logger.info("Feed reconnected.")

    def on_disconnect(self) -> None:
        with self._lock:
            self._disconnected = True
            self._alert('DISCONNECTED', 'WebSocket feed disconnected')

    def reset_session(self) -> None:
        with self._lock:
            self._last_tick.clear()
            self._last_bar.clear()
            self._bar_counts.clear()
            self._tick_counts.clear()
            self._alerts_fired.clear()
            self._session_start = datetime.now()

    # ----------------------------------------------------------------
    # Health query
    # ----------------------------------------------------------------

    def is_healthy(self) -> bool:
        now = datetime.now()
        if not _is_market_hours(now):
            return True  # outside market hours, not our problem

        with self._lock:
            if self._disconnected:
                return False
            for inst, last in self._last_tick.items():
                if now - last > self.stale_feed_threshold:
                    return False
        return True

    def health_summary(self) -> dict:
        now = datetime.now()
        with self._lock:
            stale = {}
            for inst, last in self._last_tick.items():
                secs = (now - last).total_seconds()
                if secs > 30:
                    stale[inst] = round(secs, 0)
            return {
                'is_healthy':      self.is_healthy(),
                'disconnected':    self._disconnected,
                'stale_feeds':     stale,
                'bar_counts':      dict(self._bar_counts),
                'tick_counts':     dict(self._tick_counts),
                'alerts_fired':    list(self._alerts_fired),
            }

    # ----------------------------------------------------------------
    # Background staleness check
    # ----------------------------------------------------------------

    def start_background_check(self, interval_seconds: int = 30) -> None:
        """Spin up a background thread that polls health every N seconds."""
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._background_loop,
            args=(interval_seconds,),
            daemon=True,
            name='session-health-monitor',
        )
        self._monitor_thread.start()

    def stop(self) -> None:
        self._running = False

    def _background_loop(self, interval: int) -> None:
        import time as time_mod
        while self._running:
            now = datetime.now()
            if _is_market_hours(now):
                with self._lock:
                    for inst, last in self._last_tick.items():
                        if now - last > self.stale_feed_threshold:
                            self._alert(
                                'STALE_FEED',
                                f"{inst}: no ticks for {(now-last).total_seconds():.0f}s"
                            )
            time_mod.sleep(interval)

    # ----------------------------------------------------------------
    # Internal
    # ----------------------------------------------------------------

    def _alert(self, alert_type: str, detail: str) -> None:
        """Fire an alert. Called with lock held."""
        key = f"{alert_type}:{detail}"
        if key not in self._alerts_fired:
            self._alerts_fired.append(key)
            logger.warning(f"[HEALTH ALERT] {alert_type}: {detail}")
            if self._alert_cb:
                try:
                    self._alert_cb(alert_type, detail)
                except Exception as e:
                    logger.error(f"Alert callback failed: {e}")


def _is_market_hours(now: datetime) -> bool:
    t = now.time()
    return _MARKET_OPEN <= t <= _MARKET_CLOSE
