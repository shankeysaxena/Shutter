"""
Phase 6.3 — MarketDataFeed interface.

Defines the contract that any live market data provider must satisfy.
The LivePaperRuntime talks only to this interface — it does not know
whether it's connected to Kite, a replay feed, or a synthetic generator.

Feed implementations push ticks to a registered callback. This is more
natural for WebSocket-based providers than a polling model.

Thread model:
  - start() is non-blocking; spawns its own thread internally
  - on_tick callback is invoked from the feed's internal thread
  - Caller (runtime) must be thread-safe in its on_tick handler
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, List, Optional

from src.live.bar_builder import Tick


TickCallback = Callable[[Tick], None]
ConnectionCallback = Callable[[str, Optional[Exception]], None]


class MarketDataFeed(ABC):
    """
    Abstract live market data feed.

    Contract:
      - subscribe() before start()
      - on_tick callback is invoked for each new tick (from feed thread)
      - on_connect / on_disconnect callbacks are optional but recommended
      - stop() must be safe to call multiple times
    """

    def __init__(self):
        self._on_tick:       Optional[TickCallback]       = None
        self._on_connect:    Optional[ConnectionCallback] = None
        self._on_disconnect: Optional[ConnectionCallback] = None

    def set_tick_callback(self, cb: TickCallback) -> None:
        self._on_tick = cb

    def set_connect_callback(self, cb: ConnectionCallback) -> None:
        self._on_connect = cb

    def set_disconnect_callback(self, cb: ConnectionCallback) -> None:
        self._on_disconnect = cb

    @abstractmethod
    def subscribe(self, instruments: List[str]) -> None:
        """Subscribe to a list of instrument symbols (e.g. ['NIFTY', 'BANKNIFTY'])."""
        raise NotImplementedError

    @abstractmethod
    def start(self) -> None:
        """Start the feed. Non-blocking — spins up internal thread(s)."""
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        """Stop the feed and clean up. Safe to call multiple times."""
        raise NotImplementedError

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        raise NotImplementedError
