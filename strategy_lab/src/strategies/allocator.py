"""
Phase 5A — Adaptive Strategy Allocator (backtest-only, deterministic).

Architecture
────────────
  MarketStateDetector   — classifies each session at 09:30 using only info
                          available at or before the first trade decision:
                          OR width, opening gap, session range so far.
                          Produces: GOOD_ORB | NEUTRAL | BAD_ORB | CHOPPY_BAD

  StrategyEligibilityPolicy
                       — deterministic mapping of market state → which
                          strategies are allowed to generate signals.
                          No ML, no lookahead.

  AllocationGatedStrategy
                       — wraps any BaseStrategy; intercepts generate_signal
                          and returns None when the strategy is not eligible
                          in the current session state. The underlying
                          strategy's own logic is untouched.

Why this is correct for Phase 5A
────────────────────────────────
  We already know from research:
    VWAP_PULLBACK   → profitable in BAD_ORB and NEUTRAL
    ORB             → profitable only in NEUTRAL / GOOD_ORB
    VWAP_REVERSION  → signal exists but below breakeven; gated to BAD_ORB
    OR_FAILURE_FADE → signal exists but below breakeven; gated to exhaustion days
    GAP_BEHAVIOR    → no confirmed edge; disabled

  The allocator does NOT invent strategy logic. It selects which strategy
  is allowed to participate given the current market state.

Decision timing
───────────────
  Classification happens at the FIRST bar where OR is ready (≥09:30).
  Until that bar, all strategies are blocked by the 09:30 gate anyway.
  After that bar, the state is cached for the whole session.

  Inputs used:
    features.or_width     — set after 09:30 (post-OR info)
    features.gap_pct      — set at 09:15 (pre-session info)
    session_range_pct     — (session_high_so_far - session_low_so_far) / open,
                           known in rolling form from first bar
  No lookahead into today's close or tomorrow's data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import Dict, Optional, Set, Tuple

from src.analytics.regime import (
    BAD_ORB_GAP_MAX,
    BAD_ORB_OR_WIDTH_MIN,
    GOOD_ORB_GAP_MAX,
    GOOD_ORB_GAP_MIN,
    GOOD_ORB_OR_WIDTH_MAX,
    GOOD_ORB_OR_WIDTH_MIN,
    GOOD_ORB_VOL_MIN,
    REGIME_BAD,
    REGIME_GOOD,
    REGIME_NEUTRAL,
)
from src.core.models import Signal, StrategyContext
from src.core.option_models import MultiLegSignal, MultiLegTrade
from src.strategies.base import BaseStrategy

# Additional state beyond the three base regimes
STATE_CHOPPY_BAD    = 'CHOPPY_BAD'       # BAD_ORB + very flat open
STATE_EXHAUSTION    = 'EXHAUSTION'       # Wide OR + large gap — OR Failure Fade territory
STATE_LOW_VOL_COMP  = 'LOW_VOL_COMP'    # Intraday compression around VWAP — Compression Breakout territory
ALLOC_BLOCKED       = 'allocator_blocked'


# ─────────────────────────────────────────────────────────────────────────────
# Market State Detector
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SessionState:
    date: date
    instrument: str
    regime: str                       # GOOD_ORB | NEUTRAL | BAD_ORB | CHOPPY_BAD | EXHAUSTION | LOW_VOL_COMP
    or_width_pct: float
    gap_abs_pct: float
    prior_day_range_pct: Optional[float]
    is_wide_or: bool
    is_large_gap: bool
    # Compression state — set mid-session when rolling window detects coiling
    compression_detected: bool  = False
    comp_high: Optional[float]  = None   # highest high during compression window
    comp_low:  Optional[float]  = None   # lowest low during compression window


# Minimum prior-day range to qualify as "structured enough for ORB"
# Slightly below the oracle's 1% full-session threshold because prior day
# is a proxy, not the current session's actual expansion.
PRIOR_DAY_VOL_MIN  = 0.008           # 0.8%
_COMPRESSION_START = time(10, 0)     # don't detect compression before 10:00


class MarketStateDetector:
    """
    Classifies market state once per session at the first OR-ready bar.
    Uses only time-valid features:

      Pre-open (09:15):  gap_pct
      Post-OR  (09:30):  or_width
      Prior-day proxy:   accumulated prior session's (high - low) / open

    REMOVED (v2 fix): current-day session_range_pct — at 09:30 this equals
    the OR width (only 15 bars elapsed), not the full-day range. Using it
    for the GOOD_ORB "high volatility" check caused systematic
    misclassification: sessions that the oracle labels NEUTRAL were classified
    GOOD_ORB (or vice versa) because the full-day expansion wasn't yet known.
    """

    def __init__(self):
        self._session_cache: Dict[Tuple[str, date], SessionState] = {}
        self._daily_stats:   Dict[Tuple[str, date], dict] = {}
        # Rolling bar window for mid-session compression detection.
        # Using a recent window avoids false negatives from opening volatility.
        from collections import deque as _deque
        self._bar_windows: Dict[Tuple[str, date], object] = {}
        self._COMP_WINDOW  = 20
        # FIX: idempotency guard — track unique bar timestamps already processed.
        # detect() is called multiple times per bar (from generate_signal AND
        # explain_no_signal on each wrapped strategy). Without this guard, the
        # same bar gets appended to rolling windows N times (N = strategy count),
        # corrupting compression detection.
        self._seen_bar_keys: set = set()

    def detect(self, ctx: StrategyContext) -> Optional[SessionState]:
        """
        Return the session's market state, or None if OR not yet ready.

        Rolling state (daily_stats, bar_windows) is updated only ONCE per unique
        (instrument, session_date, timestamp) — idempotent on repeated calls.
        """
        bar      = ctx.bar_event.candle
        features = ctx.bar_event.features

        # Idempotency: only update mutable state once per unique bar
        bar_key = (bar.instrument, features.session_date, bar.timestamp)
        if bar_key not in self._seen_bar_keys:
            self._seen_bar_keys.add(bar_key)
            self._update_daily_stats(bar.instrument, features.session_date,
                                      bar.high, bar.low, bar.open)
            self._update_bar_window(bar.instrument, features.session_date, bar.high, bar.low)

        if not features.or_ready or features.or_width is None:
            return None

        key = (bar.instrument, features.session_date)

        # Initial OR-based classification (cached per session)
        if key not in self._session_cache:
            or_w    = float(features.or_width)
            gap_abs = abs(float(features.gap_pct)) if features.gap_pct is not None else 0.0
            prior   = self._prior_day_range(bar.instrument, features.session_date)
            regime  = self._classify(or_w, gap_abs, prior)
            self._session_cache[key] = SessionState(
                date=features.session_date, instrument=bar.instrument,
                regime=regime, or_width_pct=or_w, gap_abs_pct=gap_abs,
                prior_day_range_pct=prior,
                is_wide_or=(or_w > BAD_ORB_OR_WIDTH_MIN),
                is_large_gap=(gap_abs > 0.010),
            )

        state = self._session_cache[key]

        # FIX: compression_detected is an ADDITIVE flag — state.regime stays as
        # the base regime (BAD_ORB, NEUTRAL, etc.) and is never overwritten.
        # This lets the allocator grant BOTH base-regime strategies AND
        # compression strategies simultaneously (e.g. BAD_ORB day that coils
        # mid-session still allows VWAP_REVERSION from the BAD_ORB bucket).
        # EXHAUSTION and CHOPPY_BAD are excluded — those days are not coiling days.
        if (not state.compression_detected
                and bar.timestamp.time() >= _COMPRESSION_START
                and state.regime not in (STATE_EXHAUSTION, STATE_CHOPPY_BAD)
                and self._is_compressed(bar.instrument, features.session_date, features)):
            state.compression_detected = True
            # Store the compression range so strategies can consume it directly
            # instead of re-detecting compression independently.
            window = self._bar_windows.get((bar.instrument, features.session_date))
            if window:
                state.comp_high = max(h for h, _ in window)
                state.comp_low  = min(l for _, l in window)

        return state

    def reset(self) -> None:
        self._session_cache.clear()
        self._daily_stats.clear()
        self._bar_windows.clear()
        self._seen_bar_keys.clear()

    def _update_bar_window(self, instrument: str, session_date, high: float, low: float) -> None:
        from collections import deque
        key = (instrument, session_date)
        if key not in self._bar_windows:
            self._bar_windows[key] = deque(maxlen=self._COMP_WINDOW)
        self._bar_windows[key].append((high, low))

    def _is_compressed(self, instrument: str, session_date, features) -> bool:
        """
        Rolling-window compression check (post-10:00 only).
        Uses the last N bars' high/low range — NOT the full-session range,
        which includes opening volatility and would suppress valid mid-session setups.

        True when:
          - ATR is warm
          - Last N bars' range / ATR < threshold  (narrow recent bars)
          - Price is near VWAP                    (centered, not trending)
        """
        atr  = features.intraday_atr
        vwap = features.vwap
        if atr is None or atr <= 0 or vwap is None:
            return False

        key    = (instrument, session_date)
        window = self._bar_windows.get(key)
        if not window or len(window) < self._COMP_WINDOW:
            return False   # not enough bars yet for a reliable reading

        highs = [h for h, _ in window]
        lows  = [l for _, l in window]
        window_range_atr = (max(highs) - min(lows)) / atr
        vwap_dist = abs(features.vwap_distance) if features.vwap_distance is not None else 1.0
        # intraday_atr is a per-bar (1-min) ATR, not daily ATR.
        # A 20-bar window spanning < 2.2× a single bar's ATR is genuinely compressed
        # (~20% of sessions on NIFTY 2025 data). Original threshold 1.5 was miscalibrated
        # for daily ATR and mathematically never fired (observed min ratio: 1.754).
        return window_range_atr < 2.2 and vwap_dist < 0.004

    # ----------------------------------------------------------------

    def _update_daily_stats(self, instrument: str, session_date, high: float,
                              low: float, open_: float) -> None:
        key = (instrument, session_date)
        if key not in self._daily_stats:
            self._daily_stats[key] = {'high': high, 'low': low, 'open': open_}
        else:
            s = self._daily_stats[key]
            s['high'] = max(s['high'], high)
            s['low']  = min(s['low'],  low)

    def _prior_day_range(self, instrument: str, today: date) -> Optional[float]:
        """Full-session (high-low)/open of the most recent prior session."""
        best_date, best_stats = None, None
        for (inst, d), stats in self._daily_stats.items():
            if inst == instrument and d < today:
                if best_date is None or d > best_date:
                    best_date, best_stats = d, stats
        if best_stats is None:
            return None
        open_ = best_stats.get('open', 0)
        return (best_stats['high'] - best_stats['low']) / open_ if open_ > 0 else None

    @staticmethod
    def _classify(or_w: float, gap_abs: float,
                  prior_day_range: Optional[float]) -> str:
        """
        Regime classification using only features valid at 09:30.

        Inputs:
          or_w             — OR width (set at 09:30)
          gap_abs          — |opening gap| (set at 09:15)
          prior_day_range  — yesterday's (high-low)/open (known pre-open)

        GOOD_ORB requires normal OR + medium gap + prior day was volatile.
        The prior-day volatility check replaces the invalid current-day range
        that was accidentally filtering out nearly all sessions at 09:30.
        """
        is_bad_or  = or_w > BAD_ORB_OR_WIDTH_MIN        # OR > 0.6%
        is_flat    = gap_abs < BAD_ORB_GAP_MAX           # gap < 0.2%
        is_wide_or = or_w > BAD_ORB_OR_WIDTH_MIN
        is_lg_gap  = gap_abs > 0.010                     # gap > 1%
        is_good_or = GOOD_ORB_OR_WIDTH_MIN <= or_w <= GOOD_ORB_OR_WIDTH_MAX
        is_good_gap= GOOD_ORB_GAP_MIN <= gap_abs <= GOOD_ORB_GAP_MAX
        # Use prior-day range as volatility proxy — NOT current-day (invalid at 09:30)
        prior_vol_ok = (prior_day_range is not None and
                        prior_day_range >= PRIOR_DAY_VOL_MIN)

        # EXHAUSTION: wide OR + large gap → OR Failure Fade territory
        if is_wide_or and is_lg_gap:
            return STATE_EXHAUSTION

        # BAD_ORB sub-states (checked before GOOD to ensure BAD wins)
        if is_bad_or or is_flat:
            return STATE_CHOPPY_BAD if is_flat else REGIME_BAD

        # GOOD_ORB: normal OR + medium gap + prior day was volatile enough
        if is_good_or and is_good_gap and prior_vol_ok:
            return REGIME_GOOD

        return REGIME_NEUTRAL


# ─────────────────────────────────────────────────────────────────────────────
# Eligibility policies
# ─────────────────────────────────────────────────────────────────────────────

# State → set of eligible strategy names ('*' = all)
_POLICY_ALL_ON: Dict[str, Set[str]] = {
    REGIME_GOOD:      {'*'},
    REGIME_NEUTRAL:   {'*'},
    REGIME_BAD:       {'*'},
    STATE_CHOPPY_BAD: {'*'},
    STATE_EXHAUSTION: {'*'},
}

_POLICY_VWAP_PULLBACK_ONLY: Dict[str, Set[str]] = {
    REGIME_GOOD:      {'VWAP_PULLBACK'},
    REGIME_NEUTRAL:   {'VWAP_PULLBACK'},
    REGIME_BAD:       {'VWAP_PULLBACK'},
    STATE_CHOPPY_BAD: {'VWAP_PULLBACK'},
    STATE_EXHAUSTION: {'VWAP_PULLBACK'},
}

_POLICY_CONSERVATIVE: Dict[str, Set[str]] = {
    # VWAP_PULLBACK always; ORB only on structured days; everything else disabled
    REGIME_GOOD:      {'VWAP_PULLBACK', 'ORB'},
    REGIME_NEUTRAL:   {'VWAP_PULLBACK', 'ORB'},
    REGIME_BAD:       {'VWAP_PULLBACK'},
    STATE_CHOPPY_BAD: {'VWAP_PULLBACK'},
    STATE_EXHAUSTION: {'VWAP_PULLBACK'},
}

_POLICY_DETERMINISTIC: Dict[str, Set[str]] = {
    # Full regime-aware allocation (empirically motivated):
    # GOOD_ORB / NEUTRAL  → breakout strategies work
    # BAD_ORB             → mean-reversion strategies (signal confirmed, not yet profitable)
    # CHOPPY_BAD          → VWAP Pullback only — quietest signal in chop
    # EXHAUSTION          → OR Failure Fade + VWAP Pullback
    REGIME_GOOD:      {'VWAP_PULLBACK', 'ORB'},
    REGIME_NEUTRAL:   {'VWAP_PULLBACK', 'ORB'},
    REGIME_BAD:       {'VWAP_PULLBACK', 'VWAP_REVERSION'},
    STATE_CHOPPY_BAD: {'VWAP_PULLBACK'},
    STATE_EXHAUSTION: {'VWAP_PULLBACK', 'OR_FAILURE_FADE'},
}

_POLICY_DETERMINISTIC_NO_ORB: Dict[str, Set[str]] = {
    # ORB removed: it cannot be reliably gated without full-session range,
    # which is unavailable at 09:30 decision time.
    # Evidence: even gated to NEUTRAL/GOOD_ORB, ORB loses -₹70k in 2025.
    REGIME_GOOD:      {'VWAP_PULLBACK'},
    REGIME_NEUTRAL:   {'VWAP_PULLBACK'},
    REGIME_BAD:       {'VWAP_PULLBACK', 'VWAP_REVERSION'},
    STATE_CHOPPY_BAD: {'VWAP_PULLBACK'},
    STATE_EXHAUSTION: {'VWAP_PULLBACK', 'OR_FAILURE_FADE'},
}

_POLICY_FAST_ITER: Dict[str, Set[str]] = {
    # Fast-iteration live-paper policy (Phase 5B)
    # VWAP_PULLBACK — anchor, always active
    # VWAP_REVERSION — BAD_ORB only (confirmed signal, gated)
    # OR_FAILURE_FADE — EXHAUSTION only (confirmed direction, gated)
    # COMPRESSION_BREAKOUT — LOW_VOL_COMP ONLY (research; requires compression state)
    # ORB / GAP_BEHAVIOR — never enabled
    REGIME_GOOD:         {'VWAP_PULLBACK'},
    REGIME_NEUTRAL:      {'VWAP_PULLBACK'},
    REGIME_BAD:          {'VWAP_PULLBACK', 'VWAP_REVERSION'},
    STATE_CHOPPY_BAD:    {'VWAP_PULLBACK'},
    STATE_EXHAUSTION:    {'VWAP_PULLBACK', 'OR_FAILURE_FADE'},
    STATE_LOW_VOL_COMP:  {'VWAP_PULLBACK', 'COMPRESSION_BREAKOUT'},
}

NAMED_POLICIES = {
    'all_on':                  _POLICY_ALL_ON,
    'vwap_pullback_only':      _POLICY_VWAP_PULLBACK_ONLY,
    'conservative':            _POLICY_CONSERVATIVE,
    'deterministic':           _POLICY_DETERMINISTIC,
    'deterministic_no_orb':    _POLICY_DETERMINISTIC_NO_ORB,
    'fast_iter_allocator':     _POLICY_FAST_ITER,
}


class StrategyEligibilityPolicy:
    def __init__(self, name: str, eligibility: Dict[str, Set[str]]):
        self.name = name
        self._eligibility = eligibility

    def is_eligible(self, strategy_name: str, state: Optional[SessionState]) -> bool:
        if state is None:
            return False   # OR not ready yet — block all

        # Check base regime eligibility
        base_allowed = self._eligibility.get(state.regime, set())
        if '*' in base_allowed or strategy_name in base_allowed:
            return True

        # FIX: also check LOW_VOL_COMP bucket when compression is detected,
        # independently of base regime. A BAD_ORB day with compression should
        # allow COMPRESSION_BREAKOUT while still allowing VWAP_REVERSION from
        # the BAD_ORB bucket — both can be active simultaneously.
        if state.compression_detected:
            comp_allowed = self._eligibility.get(STATE_LOW_VOL_COMP, set())
            if '*' in comp_allowed or strategy_name in comp_allowed:
                return True

        return False

    @classmethod
    def from_name(cls, name: str) -> 'StrategyEligibilityPolicy':
        if name not in NAMED_POLICIES:
            raise ValueError(f"Unknown policy: {name!r}. Available: {list(NAMED_POLICIES)}")
        return cls(name, NAMED_POLICIES[name])


# ─────────────────────────────────────────────────────────────────────────────
# Allocation-gated strategy wrapper
# ─────────────────────────────────────────────────────────────────────────────

class AllocationGatedStrategy(BaseStrategy):
    """
    Wraps any BaseStrategy and gates generate_signal using a
    MarketStateDetector + StrategyEligibilityPolicy.

    The underlying strategy's logic is completely unchanged — the allocator
    is a transparent layer that either passes through or blocks each call.
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        detector: MarketStateDetector,
        policy: StrategyEligibilityPolicy,
    ):
        self._strategy = strategy
        self._detector = detector
        self._policy   = policy
        # name forwarded so the engine can identify this strategy
        self.name = strategy.name

    def generate_signal(self, ctx: StrategyContext) -> Optional[Signal | MultiLegSignal]:
        state = self._detector.detect(ctx)
        if not self._policy.is_eligible(self.name, state):
            return None

        strat_cfg = (ctx.strategy_config or {}).get(self.name.lower(), {})
        if strat_cfg.get('research_only', False):
            if state is None or not state.compression_detected:
                return None

        # Inject market state so strategies can read compression range directly
        import dataclasses as _dc
        enriched_ctx = _dc.replace(ctx, market_state=state)
        return self._strategy.generate_signal(enriched_ctx)

    def explain_no_signal(self, ctx: StrategyContext) -> str:
        state = self._detector.detect(ctx)
        if not self._policy.is_eligible(self.name, state):
            regime = state.regime if state else 'unknown'
            comp   = f'+compression' if (state and state.compression_detected) else ''
            return f'{ALLOC_BLOCKED}:{regime}{comp}'
        # research_only check mirrors generate_signal
        strat_cfg = (ctx.strategy_config or {}).get(self.name.lower(), {})
        if strat_cfg.get('research_only', False):
            if state is None or not state.compression_detected:
                return f'{ALLOC_BLOCKED}:research_only_no_compression'
        return self._strategy.explain_no_signal(ctx)

    def evaluate_multi_leg_exits(self, ctx: StrategyContext):
        return self._strategy.evaluate_multi_leg_exits(ctx)

    def reset(self) -> None:
        self._strategy.reset()


def wrap_strategies(
    strategies: list,
    policy_name: str,
) -> list:
    """
    Wrap a list of strategies with allocation gating under `policy_name`.
    All wrapped strategies share one MarketStateDetector (caches per session).
    """
    detector = MarketStateDetector()
    policy   = StrategyEligibilityPolicy.from_name(policy_name)
    return [AllocationGatedStrategy(s, detector, policy) for s in strategies]
