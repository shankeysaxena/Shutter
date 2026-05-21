"""
Phase 6.3 — KiteWebSocketFeed.

Connects to Kite Connect's WebSocket ticker and converts incoming ticks
to the lab's Tick dataclass. Routes each tick to the registered callback
for LiveBarBuilder to aggregate into 1-minute bars.

Kite-specific details:
  - Uses `KiteTicker` from the kiteconnect package
  - Subscribes in FULL mode (includes last_price, volume, etc.)
  - Instrument tokens are resolved from instruments.py before connecting
  - Handles reconnection automatically via KiteTicker's built-in retry

IST handling:
  - Kite timestamps arrive as timezone-aware IST datetimes
  - Stripped to naive IST here to stay consistent with the rest of the lab
    (see `historical_loader.py` which does the same for historical data)

Token → symbol map:
  - The feed maintains a {token: symbol} lookup so ticks can be labelled
    with human-readable symbols (NIFTY / BANKNIFTY) rather than raw tokens
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from src.live.bar_builder import Tick
from src.live.feeds import MarketDataFeed
from src.integrations.zerodha.instruments import _FALLBACK_TOKENS

logger = logging.getLogger(__name__)

# Well-known index tokens → symbol (same as instruments.py fallback table, inverted)
_TOKEN_TO_SYMBOL: Dict[int, str] = {v: k for k, v in {
    'NIFTY':     256265,
    'BANKNIFTY': 260105,
}.items()}


class KiteWebSocketFeed(MarketDataFeed):
    """
    Live tick feed via Kite Connect WebSocket (KiteTicker).

    Usage:
        feed = KiteWebSocketFeed(api_key=..., access_token=...)
        feed.subscribe(['NIFTY', 'BANKNIFTY'])
        feed.set_tick_callback(bar_builder.on_tick)
        feed.start()
        ...
        feed.stop()
    """

    def __init__(self, api_key: str, access_token: str,
                 reconnect_max_tries: int = 10):
        super().__init__()
        self._api_key          = api_key
        self._access_token     = access_token
        self._reconnect_tries  = reconnect_max_tries
        self._subscribed_tokens: List[int] = []
        self._token_map:        Dict[int, str] = dict(_TOKEN_TO_SYMBOL)
        self._ticker           = None
        self._connected        = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def subscribe(self, instruments: List[str]) -> None:
        """
        Register instruments by symbol. Resolves to Kite tokens using the
        fallback table (covers NIFTY/BANKNIFTY without a full instruments dump).
        For other symbols, run `zerodha instruments refresh` first.
        """
        from src.integrations.zerodha.instruments import resolve_instrument_token
        tokens = []
        for symbol in instruments:
            try:
                token = resolve_instrument_token(symbol, 'NSE')
                tokens.append(token)
                self._token_map[token] = symbol.upper()
            except LookupError as e:
                logger.warning(f"Could not resolve token for {symbol}: {e}")
        self._subscribed_tokens = tokens
        logger.info(f"Subscribed tokens: {self._token_map}")

    def start(self) -> None:
        """Connect to Kite WebSocket. Non-blocking — runs in a daemon thread."""
        try:
            from kiteconnect import KiteTicker
        except ImportError as e:
            raise ImportError(
                "kiteconnect required. Install: pip install -r requirements.txt"
            ) from e

        ticker = KiteTicker(self._api_key, self._access_token,
                             reconnect_max_tries=self._reconnect_tries)

        ticker.on_ticks   = self._on_ticks
        ticker.on_connect = self._on_connect_handler
        ticker.on_close   = self._on_close_handler
        ticker.on_error   = self._on_error_handler

        self._ticker = ticker
        ticker.connect(threaded=True)   # non-blocking daemon thread
        logger.info("KiteWebSocketFeed started.")

    def stop(self) -> None:
        if self._ticker is not None:
            try:
                self._ticker.close()
            except Exception:
                pass
            self._ticker    = None
            self._connected = False
        logger.info("KiteWebSocketFeed stopped.")

    def reconnect(self) -> None:
        """
        Force-close the current (possibly zombie) connection and re-establish.
        Called when the health monitor detects a silent TCP hang:
          - disconnected=False (no close event received)
          - stale feed (no ticks for N minutes)
        This is not the same as KiteTicker's built-in reconnect, which only
        fires on clean disconnects. This handles silent hangs.
        """
        logger.warning("KiteWebSocketFeed: forcing reconnect (zombie connection detected)")
        self.stop()
        import time as _time
        _time.sleep(2)   # brief pause to let the OS release the socket
        self.start()
        logger.info("KiteWebSocketFeed: reconnect complete")

    # ----------------------------------------------------------------
    # KiteTicker callbacks (run on KiteTicker's internal thread)
    # ----------------------------------------------------------------

    def _on_connect_handler(self, ws, response) -> None:
        self._connected = True
        logger.info("Kite WebSocket connected.")
        if self._subscribed_tokens:
            ws.subscribe(self._subscribed_tokens)
            ws.set_mode(ws.MODE_FULL, self._subscribed_tokens)
        if self._on_connect:
            self._on_connect('connected', None)

    def _on_close_handler(self, ws, code, reason) -> None:
        self._connected = False
        logger.warning(f"Kite WebSocket closed: code={code} reason={reason}")
        if self._on_disconnect:
            self._on_disconnect('disconnected', None)

    def _on_error_handler(self, ws, code, reason) -> None:
        logger.error(f"Kite WebSocket error: code={code} reason={reason}")
        # Surface to disconnect callback so runtime + monitor + notifier are informed
        if self._on_disconnect:
            self._on_disconnect('error', Exception(f"code={code} reason={reason}"))

    def _on_ticks(self, ws, ticks: list) -> None:
        """Convert Kite tick dicts to Tick objects and route to callback."""
        if self._on_tick is None:
            return

        for raw in ticks:
            try:
                token       = raw.get('instrument_token', 0)
                symbol      = self._token_map.get(token)
                if symbol is None:
                    continue

                ts = raw.get('timestamp') or raw.get('last_trade_time') or datetime.now()
                if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
                    ts = ts.replace(tzinfo=None)   # strip tz → naive IST

                tick = Tick(
                    instrument   = symbol,
                    timestamp    = ts,
                    last_price   = float(raw.get('last_price', 0)),
                    last_quantity= int(raw.get('last_quantity', 0)),
                    volume       = int(raw.get('volume', raw.get('volume_traded', 0))),
                )
                self._on_tick(tick)
            except Exception as e:
                logger.error(f"Error processing tick: {e} — raw={raw}")
