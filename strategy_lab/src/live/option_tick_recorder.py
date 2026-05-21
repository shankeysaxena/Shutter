"""
Real-time option chain recorder — Phase 2.

Subscribes to option strikes via Kite WebSocket in FULL mode during market
hours. Collects real bid/ask (from depth), LTP, OI, and volume every minute.
Writes a Parquet archive at session end matching the HistoricalOptionChainFeed
schema (+ volume and oi columns added in Phase 2).

Architecture:
    KiteWebSocketFeed  →  option_tick callback
                              ↓
                        OptionTickAggregator  →  1-min snapshots in memory
                              ↓ (session end)
                        write_session_parquet  →  data/option_chain_snapshots/

Why WebSocket over end-of-session batch:
    The batch approach uses open-as-bid / close-as-ask which are poor proxies.
    WebSocket FULL mode provides real depth[0].bid / depth[0].ask every tick.

IV calculation:
    BSM back-calculation from LTP (mid of best bid/ask if available, else LTP).
    Uses the same Black-Scholes helpers as SyntheticOptionChainFeed.
    IV is set to 0.0 when back-calculation fails (deep OTM, very short T, etc.)

Usage (inside LivePaperRuntime or CLI):
    recorder = OptionTickRecorder(feed, resolver, config)
    recorder.start(session_date='2025-06-05')   # subscribes to tokens
    # ... market runs ...
    recorder.stop()                              # writes archive, unsubscribes
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# ─── BSM helpers (replicated from option_chain_snapshot.py to avoid circular import) ─

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(S: float, K: float, T: float, sigma: float, r: float, opt: str) -> float:
    if T <= 0 or sigma <= 0:
        intrinsic = max(S - K, 0.0) if opt == 'CE' else max(K - S, 0.0)
        return intrinsic
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt == 'CE':
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _implied_vol(S: float, K: float, T: float, r: float, price: float, opt: str) -> float:
    """Bisection-based IV. Returns 0.0 if no solution found."""
    if T <= 0 or price <= 0:
        return 0.0
    lo, hi = 0.001, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        p   = _bs_price(S, K, T, mid, r, opt)
        if abs(p - price) < 0.01:
            return mid
        if p > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0


# ─── 1-minute bucket ─────────────────────────────────────────────────────────

@dataclass
class _MinuteBucket:
    """Accumulates raw ticks for one (token, 1-min bar)."""
    bids:    List[float] = field(default_factory=list)
    asks:    List[float] = field(default_factory=list)
    ltps:    List[float] = field(default_factory=list)
    volumes: List[int]   = field(default_factory=list)
    ois:     List[int]   = field(default_factory=list)

    def last_bid(self) -> float:
        return self.bids[-1] if self.bids else 0.0

    def last_ask(self) -> float:
        return self.asks[-1] if self.asks else 0.0

    def last_ltp(self) -> float:
        return self.ltps[-1] if self.ltps else 0.0

    def last_volume(self) -> int:
        return self.volumes[-1] if self.volumes else 0

    def last_oi(self) -> int:
        return self.ois[-1] if self.ois else 0


# ─── Aggregator ──────────────────────────────────────────────────────────────

class OptionTickAggregator:
    """
    Accumulates raw Kite FULL-mode ticks for option tokens.
    Produces 1-minute snapshot rows on demand.

    Token metadata (strike, expiry, option_type) is injected at init via
    the token_meta dict from OptionInstrumentResolver.tokens_for_session().
    """

    def __init__(self, token_meta: Dict[int, dict], risk_free_rate: float = 0.07):
        self._meta  = token_meta        # {token: {underlying, expiry, strike, option_type}}
        self._rfr   = risk_free_rate
        # {(token, minute_ts): _MinuteBucket}
        self._buckets: Dict[Tuple[int, datetime], _MinuteBucket] = defaultdict(_MinuteBucket)
        self._spot_series: Dict[datetime, float] = {}   # minute_ts → spot

    def on_tick(self, token: int, tick: dict, spot: Optional[float] = None) -> None:
        """
        Called for each incoming WebSocket tick.

        tick fields expected (Kite FULL mode):
            last_price       : float
            volume           : int   (cumulative for day)
            oi               : int
            depth.buy[0].price  : float  (best bid)
            depth.sell[0].price : float  (best ask)
            timestamp        : datetime
        """
        if token not in self._meta:
            return

        ts      = tick.get('timestamp') or tick.get('last_trade_time') or datetime.now()
        if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        minute_ts = ts.replace(second=0, microsecond=0)

        bucket = self._buckets[(token, minute_ts)]
        bucket.ltps.append(float(tick.get('last_price', 0)))
        bucket.volumes.append(int(tick.get('volume_traded', tick.get('volume', 0))))
        bucket.ois.append(int(tick.get('oi', 0)))

        # Real bid/ask from FULL-mode depth
        depth = tick.get('depth', {})
        buy_levels  = depth.get('buy', [])
        sell_levels = depth.get('sell', [])
        bid = float(buy_levels[0]['price'])  if buy_levels  and buy_levels[0].get('price')  else 0.0
        ask = float(sell_levels[0]['price']) if sell_levels and sell_levels[0].get('price') else 0.0
        bucket.bids.append(bid)
        bucket.asks.append(ask)

        if spot is not None:
            self._spot_series[minute_ts] = spot

    def snapshot_rows(self) -> List[dict]:
        """
        Convert accumulated buckets → list of row dicts (one per token per minute).
        Called at session end before writing Parquet.
        """
        rows = []
        for (token, minute_ts), bucket in sorted(self._buckets.items()):
            meta  = self._meta[token]
            bid   = bucket.last_bid()
            ask   = bucket.last_ask()
            ltp   = bucket.last_ltp()
            spot  = self._spot_series.get(minute_ts, 0.0)
            price = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else ltp
            iv    = 0.0
            if spot > 0 and price > 0:
                expiry_date = meta['expiry']
                T = max((expiry_date - minute_ts.date()).days / 365.0, 1 / (365 * 24 * 60))
                iv = _implied_vol(spot, meta['strike'], T, self._rfr, price, meta['option_type'])
            rows.append({
                'timestamp':   minute_ts,
                'spot':        spot,
                'expiry':      meta['expiry'].isoformat(),
                'strike':      meta['strike'],
                'option_type': meta['option_type'],
                'bid':         bid,
                'ask':         ask,
                'last':        ltp,
                'iv':          iv,
                'volume':      bucket.last_volume(),
                'oi':          bucket.last_oi(),
            })
        return rows

    def clear(self) -> None:
        self._buckets.clear()
        self._spot_series.clear()


# ─── Recorder ────────────────────────────────────────────────────────────────

@dataclass
class OptionRecorderConfig:
    underlyings:        List[str]        = field(default_factory=lambda: ['NIFTY'])
    strikes_each_side:  int              = 10
    strike_interval:    Dict[str, float] = field(default_factory=lambda: {'NIFTY': 50.0, 'BANKNIFTY': 100.0})
    snapshot_dir:       str              = 'data/option_chain_snapshots'
    risk_free_rate:     float            = 0.07
    atm_spot_estimate:  Dict[str, float] = field(default_factory=dict)


class OptionTickRecorder:
    """
    Real-time option chain recorder.

    Plugs into the live session:
      1. On session start: resolve tokens, subscribe to WebSocket, start aggregating.
      2. Each tick: route to OptionTickAggregator (called from feed's tick callback).
      3. On session end: flush aggregated rows to Parquet archive.

    Spot prices (for IV calculation and the 'spot' column) are provided by the
    caller via update_spot() — typically from the underlying index bar closes.
    """

    def __init__(
        self,
        resolver,           # OptionInstrumentResolver
        config: OptionRecorderConfig,
    ):
        self._resolver   = resolver
        self._config     = config
        self._aggregators: Dict[str, OptionTickAggregator] = {}  # underlying → aggregator
        self._token_to_underlying: Dict[int, str] = {}
        self._session_date: Optional[date] = None
        self._all_tokens:   List[int] = []
        self._spot:         Dict[str, float] = {}   # underlying → latest spot

    def start(self, session_date: date, spot_estimates: Dict[str, float]) -> List[int]:
        """
        Resolve tokens and prepare aggregators for the session.
        Returns the list of tokens to subscribe to via WebSocket.
        """
        self._session_date = session_date
        self._spot = dict(spot_estimates)
        self._all_tokens.clear()
        self._aggregators.clear()
        self._token_to_underlying.clear()

        for underlying in self._config.underlyings:
            spot = spot_estimates.get(underlying)
            if not spot:
                logger.warning(f"No spot estimate for {underlying} — skipping option subscription")
                continue

            expiry   = self._resolver.nearest_weekly_expiry(session_date, underlying)
            interval = self._config.strike_interval.get(underlying, 50.0)
            atm      = round(spot / interval) * interval
            strikes  = [atm + i * interval
                        for i in range(-self._config.strikes_each_side,
                                        self._config.strikes_each_side + 1)]

            token_meta = self._resolver.tokens_for_session(underlying, expiry, strikes)
            if not token_meta:
                logger.warning(f"No tokens resolved for {underlying} expiry={expiry}")
                continue

            self._aggregators[underlying] = OptionTickAggregator(
                token_meta, risk_free_rate=self._config.risk_free_rate
            )
            for token in token_meta:
                self._token_to_underlying[token] = underlying
                self._all_tokens.append(token)

        logger.info(
            f"OptionTickRecorder ready: {len(self._all_tokens)} tokens "
            f"across {list(self._aggregators)} for {session_date}"
        )
        return self._all_tokens

    def on_tick(self, token: int, tick: dict) -> None:
        """Route a raw Kite tick to the correct underlying's aggregator."""
        underlying = self._token_to_underlying.get(token)
        if underlying is None:
            return
        agg = self._aggregators.get(underlying)
        if agg is None:
            return
        agg.on_tick(token, tick, spot=self._spot.get(underlying))

    def update_spot(self, underlying: str, spot: float) -> None:
        """Called each time a new underlying bar closes — keeps spot current for IV."""
        self._spot[underlying] = spot

    def stop(self) -> Dict[str, Path]:
        """
        Flush aggregated data to Parquet and return {underlying: path}.
        Call at end of session (15:30 or on forced shutdown).
        """
        if not self._session_date:
            return {}

        out_dir = Path(self._config.snapshot_dir)
        written = {}

        for underlying, agg in self._aggregators.items():
            rows = agg.snapshot_rows()
            if not rows:
                logger.warning(f"No option ticks recorded for {underlying} on {self._session_date}")
                continue
            path = self._write_parquet(underlying, rows, out_dir)
            written[underlying] = path
            agg.clear()
            logger.info(
                f"Option chain archive written: {underlying} {self._session_date} "
                f"({len(rows)} rows) → {path}"
            )

        self._all_tokens.clear()
        self._token_to_underlying.clear()
        return written

    def _write_parquet(self, underlying: str, rows: List[dict], out_dir: Path) -> Path:
        from src.feeds.chain_archive_schema import archive_file_path, SCHEMA_VERSION, ORIGIN_RECORDED
        from src.feeds.chain_archive import write_manifest
        from src.feeds.chain_archive_schema import ArchiveManifest
        from datetime import datetime as _dt

        manifest_file = out_dir / '_meta.yaml'
        if not manifest_file.exists():
            write_manifest(out_dir, ArchiveManifest(
                schema_version=SCHEMA_VERSION,
                data_origin=ORIGIN_RECORDED,
                generated_at=_dt.now(),
                notes='Recorded from Kite WebSocket real-time ticks (bid/ask from depth).',
            ))

        df   = pd.DataFrame(rows)
        path = archive_file_path(out_dir, underlying, self._session_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        df   = df.sort_values(['timestamp', 'strike', 'option_type']).reset_index(drop=True)
        df.to_parquet(path, index=False)
        return path
