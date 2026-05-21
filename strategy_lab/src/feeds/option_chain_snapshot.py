"""
Option chain feed.

Provides ChainSnapshot for a given (timestamp, underlying, spot). The
backtest runtime calls this per bar and passes the result into
StrategyContext.chain_snapshot.

Two implementations:
- SyntheticOptionChainFeed: Black-Scholes + configurable smile. Used for
  initial Iron Fly validation before real chain data is available.
- HistoricalOptionChainFeed: stub for stored snapshots from a broker
  archive. Populated once real data is wired in.
"""
from abc import ABC, abstractmethod
import math
from datetime import datetime, date, time, timedelta
from typing import Optional, Callable, Dict, List

from src.core.option_models import ChainSnapshot, ChainQuote


# Sinusoidal IV variation period in days (14 = two-week cycle, gives clear signal
# for percentile filters without producing implausible day-over-day vol jumps).
_DAILY_IV_PERIOD_DAYS = 14
_DAILY_IV_AMPLITUDE = 0.05


class OptionChainFeed(ABC):
    """Interface contract for option-chain providers."""

    @abstractmethod
    def snapshot_at(
        self, timestamp: datetime, underlying: str, spot: float
    ) -> Optional[ChainSnapshot]:
        """Return chain snapshot for (timestamp, underlying), or None if unavailable."""
        raise NotImplementedError

    @property
    def data_origin(self) -> str:
        """One of 'synthetic' | 'recorded' | 'broker' | 'unknown'.

        Used by the experiment runner to decide whether the synthetic-chain
        `data_source_warning` should clear. Subclasses override.
        Defensive default: anything other than 'recorded'/'broker' keeps the
        warning on.
        """
        return 'unknown'


# ---------------------------------------------------------------------
# Black-Scholes pricing helpers
# ---------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(
    S: float, K: float, T: float, sigma: float, r: float, option_type: str
) -> float:
    """European option price under Black-Scholes. T in years, sigma annualized."""
    if T <= 0:
        # At/after expiry — intrinsic only
        return max(S - K, 0.0) if option_type == "CE" else max(K - S, 0.0)
    if sigma <= 0:
        forward = S - K * math.exp(-r * T)
        return max(forward, 0.0) if option_type == "CE" else max(-forward, 0.0)

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    if option_type == "CE":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


# ---------------------------------------------------------------------
# Expiry providers
# ---------------------------------------------------------------------

class WeeklyExpiryProvider:
    """Returns the next session-date on/after `session_date` that falls on `weekday`."""

    def __init__(self, weekday: int):
        # 0 = Monday ... 6 = Sunday
        self.weekday = weekday

    def __call__(self, session_date: date, underlying: str) -> date:
        days_ahead = (self.weekday - session_date.weekday()) % 7
        return session_date + timedelta(days=days_ahead)


# ---------------------------------------------------------------------
# Synthetic chain feed
# ---------------------------------------------------------------------

# Time of expiry-day close used to compute fractional T for intraday pricing.
_EXPIRY_CLOSE = time(15, 30)


class SyntheticOptionChainFeed(OptionChainFeed):
    """
    Generates a synthetic chain from Black-Scholes with a parametric smile.

    Smile model: IV(K) = atm_iv + skew * m + smile * m^2  where m = (K - S) / S
      - skew < 0 makes OTM puts more expensive than OTM calls (typical equity skew)
      - smile > 0 produces a U-shape (wings priced above ATM)

    Time-to-expiry is computed in calendar years to the expiry-day 15:30 close,
    with a small floor so sigma*sqrt(T) stays well-defined intra-expiry-day.
    """

    def __init__(
        self,
        atm_iv: float = 0.15,
        skew: float = -0.02,
        smile: float = 0.30,
        risk_free_rate: float = 0.07,
        strike_interval: Optional[Dict[str, float]] = None,
        num_strikes_each_side: int = 20,
        spread_pct: float = 0.01,
        min_spread: float = 0.5,
        expiry_provider: Optional[Callable[[date, str], date]] = None,
        atm_iv_provider: Optional[Callable[[datetime, str], float]] = None,
        daily_iv_variation: bool = False,
        daily_iv_reference_date: Optional[date] = None,
    ):
        self.atm_iv = atm_iv
        self.skew = skew
        self.smile = smile
        self.r = risk_free_rate
        self.strike_interval = strike_interval or {"NIFTY": 50.0, "BANKNIFTY": 100.0}
        self.num_strikes = num_strikes_each_side
        self.spread_pct = spread_pct
        self.min_spread = min_spread
        self.expiry_provider = expiry_provider or WeeklyExpiryProvider(weekday=3)
        # v2.1 #4: daily_iv_variation enables a sinusoidal day-to-day shift in
        # ATM IV (period 14 days, amplitude 0.05). Useful for plumbing tests
        # that need the IV-regime percentile filter to actually receive signal.
        # Explicit atm_iv_provider takes precedence if both are set.
        self.atm_iv_provider = atm_iv_provider
        self.daily_iv_variation = daily_iv_variation
        self.daily_iv_reference_date = daily_iv_reference_date or date(2024, 1, 1)

    @property
    def data_origin(self) -> str:
        return 'synthetic'

    def snapshot_at(
        self, timestamp: datetime, underlying: str, spot: float
    ) -> Optional[ChainSnapshot]:
        if underlying not in self.strike_interval:
            return None
        if spot <= 0:
            return None

        interval = self.strike_interval[underlying]
        atm = round(spot / interval) * interval

        expiry = self.expiry_provider(timestamp.date(), underlying)
        T = _time_to_expiry_years(timestamp, expiry)

        if self.atm_iv_provider is not None:
            base_iv = self.atm_iv_provider(timestamp, underlying)
        elif self.daily_iv_variation:
            day_offset = (timestamp.date() - self.daily_iv_reference_date).days
            base_iv = self.atm_iv + _DAILY_IV_AMPLITUDE * math.sin(
                day_offset * 2.0 * math.pi / _DAILY_IV_PERIOD_DAYS
            )
            base_iv = max(base_iv, 0.01)
        else:
            base_iv = self.atm_iv

        quotes: List[ChainQuote] = []
        for i in range(-self.num_strikes, self.num_strikes + 1):
            K = atm + i * interval
            if K <= 0:
                continue
            moneyness = (K - spot) / spot
            iv = max(base_iv + self.skew * moneyness + self.smile * moneyness * moneyness, 0.01)

            for option_type in ("CE", "PE"):
                price = _bs_price(spot, K, T, iv, self.r, option_type)
                spread = max(price * self.spread_pct, self.min_spread)
                bid = max(price - spread / 2.0, 0.05)
                ask = price + spread / 2.0
                quotes.append(ChainQuote(
                    strike=K,
                    option_type=option_type,
                    bid=round(bid, 2),
                    ask=round(ask, 2),
                    last=round(price, 2),
                    iv=round(iv, 4),
                ))

        return ChainSnapshot(
            timestamp=timestamp,
            underlying=underlying,
            spot=spot,
            expiry=expiry,
            quotes=quotes,
            atm_iv=round(base_iv, 4),
        )


def _time_to_expiry_years(now: datetime, expiry: date) -> float:
    """Calendar years from `now` to expiry-day 15:30. Floored to avoid zero."""
    expiry_dt = datetime.combine(expiry, _EXPIRY_CLOSE)
    seconds = (expiry_dt - now).total_seconds()
    # Floor at 1 minute to keep sigma*sqrt(T) numerically sane
    seconds = max(seconds, 60.0)
    return seconds / (365.25 * 24.0 * 3600.0)


# ---------------------------------------------------------------------
# Historical chain feed
# ---------------------------------------------------------------------

class HistoricalOptionChainFeed(OptionChainFeed):
    """
    Loads option-chain snapshots from a Parquet archive on disk.

    Directory layout (see src/feeds/chain_archive_schema.py for full spec):
        <snapshot_dir>/
            _meta.yaml
            <UNDERLYING>/
                YYYY-MM-DD.parquet

    First query for a given (underlying, date) loads the whole day's Parquet
    into memory, builds a {timestamp -> ChainSnapshot} dict for O(1) lookup,
    and caches it. Subsequent queries for the same day are cheap.

    `snapshot_at` returns None if:
      - the day's Parquet file does not exist (treated as missing data, not error)
      - the day exists but contains no row at the queried timestamp
    The caller (BarEngine) handles None — it will not silently produce a trade.

    `data_origin` is read from the archive manifest and exposed via the
    `data_origin` property so the experiment runner can decide whether the
    synthetic-chain warning applies.
    """

    def __init__(self, snapshot_dir: str, strict_schema: bool = True):
        # Fail loudly with an actionable message if pyarrow is missing — pandas's
        # own error here is opaque ("Unable to find a usable engine"). This is
        # the first place a user without pyarrow will hit a problem.
        try:
            import pyarrow  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "pyarrow is required for HistoricalOptionChainFeed (Parquet I/O). "
                "Install via: python3 -m pip install -r requirements-dev.txt "
                "(or: python3 -m pip install pyarrow>=10.0.0)"
            ) from e

        from src.feeds.chain_archive_schema import (
            ArchiveManifest, manifest_path, REQUIRED_COLUMNS,
            SCHEMA_VERSION, ORIGIN_UNKNOWN, archive_file_path,
        )
        import os, yaml
        from pathlib import Path

        self.snapshot_dir = Path(snapshot_dir)
        self.strict_schema = strict_schema
        self._cache: Dict[tuple, Dict[datetime, ChainSnapshot]] = {}
        # Lazy import dependency holders
        self._required_columns = REQUIRED_COLUMNS
        self._archive_path_fn = archive_file_path

        # Load manifest (optional — if missing, default to unknown)
        manifest_file = manifest_path(self.snapshot_dir)
        if manifest_file.exists():
            with open(manifest_file) as f:
                meta = yaml.safe_load(f) or {}
            self._manifest = ArchiveManifest(
                schema_version=meta.get('schema_version', SCHEMA_VERSION),
                data_origin=meta.get('data_origin', ORIGIN_UNKNOWN),
                generated_at=meta.get('generated_at'),
                notes=meta.get('notes'),
            )
        else:
            self._manifest = ArchiveManifest(
                schema_version=SCHEMA_VERSION,
                data_origin=ORIGIN_UNKNOWN,
            )

    @property
    def manifest(self):
        return self._manifest

    @property
    def data_origin(self) -> str:
        return self._manifest.data_origin

    def snapshot_at(
        self, timestamp: datetime, underlying: str, spot: float
    ) -> Optional[ChainSnapshot]:
        key = (underlying, timestamp.date())
        if key not in self._cache:
            loaded = self._load_day(underlying, timestamp.date())
            if loaded is None:
                # Cache the negative result so repeated misses don't re-touch disk
                self._cache[key] = {}
                return None
            self._cache[key] = loaded

        return self._cache[key].get(timestamp)

    def _load_day(self, underlying: str, day: date) -> Optional[Dict[datetime, ChainSnapshot]]:
        """Read one Parquet file and bucket by timestamp into ChainSnapshots."""
        import pandas as pd

        path = self._archive_path_fn(self.snapshot_dir, underlying, day)
        if not path.exists():
            return None

        df = pd.read_parquet(path)
        self._validate_schema(df, path)

        # Coerce expiry: stored as ISO string; convert to date.
        # Don't rely on dtype check — pandas 3.0 + pyarrow uses 'string[pyarrow]'
        # not 'object'. Inspect a sample value and convert if it's not already a date.
        if len(df) > 0 and not isinstance(df['expiry'].iloc[0], date):
            df['expiry'] = pd.to_datetime(df['expiry']).dt.date

        snapshots: Dict[datetime, ChainSnapshot] = {}
        for ts, group in df.groupby('timestamp', sort=False):
            ts_py = ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts
            first = group.iloc[0]
            snap_spot = float(first['spot'])
            snap_expiry = first['expiry']
            quotes = [
                ChainQuote(
                    strike=float(r['strike']),
                    option_type=str(r['option_type']),
                    bid=float(r['bid']),
                    ask=float(r['ask']),
                    last=float(r['last']),
                    iv=float(r['iv']),
                )
                for _, r in group.iterrows()
            ]
            atm_iv = _nearest_atm_iv(quotes, snap_spot)
            snapshots[ts_py] = ChainSnapshot(
                timestamp=ts_py,
                underlying=underlying,
                spot=snap_spot,
                expiry=snap_expiry,
                quotes=quotes,
                atm_iv=atm_iv,
            )
        return snapshots

    def _validate_schema(self, df, path) -> None:
        missing = [c for c in self._required_columns if c not in df.columns]
        if missing:
            msg = f"{path}: missing required columns {missing}"
            if self.strict_schema:
                raise ValueError(msg)
            # In non-strict mode just warn via stderr
            import sys
            print(f"WARNING: {msg}", file=sys.stderr)


def _nearest_atm_iv(quotes: List[ChainQuote], spot: float) -> float:
    """IV of the strike closest to spot. Falls back to median if no quotes."""
    if not quotes:
        return 0.0
    by_distance = min(quotes, key=lambda q: abs(q.strike - spot))
    return by_distance.iv
