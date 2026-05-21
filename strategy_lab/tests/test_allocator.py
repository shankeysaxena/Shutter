"""Tests for MarketStateDetector, StrategyEligibilityPolicy, AllocationGatedStrategy."""
from datetime import date, datetime

import pytest

from src.core.models import BarEvent, Candle, EngineState, FeatureSnapshot, StrategyContext
from src.strategies.allocator import (
    STATE_CHOPPY_BAD,
    STATE_EXHAUSTION,
    AllocationGatedStrategy,
    MarketStateDetector,
    StrategyEligibilityPolicy,
    wrap_strategies,
)
from src.analytics.regime import REGIME_BAD, REGIME_GOOD, REGIME_NEUTRAL

_DATE = date(2025, 3, 13)


def _ctx(or_width=0.003, gap_pct=0.007, or_ready=True, session_high=22100.0,
          session_low=21900.0, prior_close=22000.0):
    ts = datetime(2025, 3, 13, 9, 31)
    candle   = Candle(ts, 'NIFTY', 22000, session_high, session_low, 22000, 1000)
    features = FeatureSnapshot(
        session_date=_DATE, minute_index=16, prior_close=prior_close,
        vwap=22000, vwap_distance=0.0, above_vwap=False, below_vwap=False,
        or_high=22000 + (22000*or_width/2), or_low=22000 - (22000*or_width/2),
        or_width=or_width, or_ready=or_ready,
        gap_pct=gap_pct, gap_direction='UP',
        session_high_so_far=session_high, session_low_so_far=session_low,
    )
    state = EngineState(
        instrument='NIFTY', session_date=_DATE,
        open_trades=[], closed_trades=[], queued_signals=[],
        per_strategy_day_trade_count={},
    )
    return StrategyContext(bar_event=BarEvent(candle, features, True, 'backtest'),
                           engine_state=state, strategy_config={})


class TestMarketStateDetector:
    def test_none_before_or_ready(self):
        d = MarketStateDetector()
        ctx = _ctx(or_ready=False)
        assert d.detect(ctx) is None

    def test_good_orb_requires_prior_day_vol(self):
        d = MarketStateDetector()
        # Normal OR + medium gap — but no prior day data → NEUTRAL (not GOOD_ORB)
        ctx = _ctx(or_width=0.003, gap_pct=0.007,
                    session_high=22220, session_low=22000, prior_close=22000)
        state = d.detect(ctx)
        assert state is not None
        assert state.regime == REGIME_NEUTRAL   # no prior day → prior_vol_ok=False

    def test_good_orb_with_prior_day_vol(self):
        from datetime import timedelta
        d = MarketStateDetector()
        # Seed prior-day stats with volatile session
        prior = date(2025, 3, 12)
        today = date(2025, 3, 13)
        # Simulate 10 bars of a volatile prior day (range > 0.8%)
        d._update_daily_stats('NIFTY', prior, high=22220, low=21780, open_=22000)
        # Now classify today
        ctx = _ctx(or_width=0.003, gap_pct=0.007,
                    session_high=22050, session_low=21990, prior_close=22000)
        state = d.detect(ctx)
        assert state is not None
        assert state.regime == REGIME_GOOD

    def test_bad_orb_flat_open(self):
        d = MarketStateDetector()
        # flat open → CHOPPY_BAD (sub-type of BAD)
        ctx = _ctx(or_width=0.003, gap_pct=0.001)
        state = d.detect(ctx)
        assert state.regime == STATE_CHOPPY_BAD

    def test_bad_orb_wide_or(self):
        d = MarketStateDetector()
        ctx = _ctx(or_width=0.007, gap_pct=0.005)
        state = d.detect(ctx)
        assert state.regime == REGIME_BAD

    def test_exhaustion_wide_or_large_gap(self):
        d = MarketStateDetector()
        ctx = _ctx(or_width=0.008, gap_pct=0.015)
        state = d.detect(ctx)
        assert state.regime == STATE_EXHAUSTION

    def test_neutral_default(self):
        d = MarketStateDetector()
        ctx = _ctx(or_width=0.005, gap_pct=0.004)
        state = d.detect(ctx)
        assert state.regime == REGIME_NEUTRAL

    def test_result_cached_per_session(self):
        d = MarketStateDetector()
        ctx = _ctx(or_width=0.003, gap_pct=0.007,
                    session_high=22220, session_low=22000, prior_close=22000)
        s1 = d.detect(ctx)
        s2 = d.detect(ctx)
        assert s1 is s2   # same object returned from cache

    def test_reset_clears_cache(self):
        d = MarketStateDetector()
        ctx = _ctx(or_width=0.003, gap_pct=0.007,
                    session_high=22220, session_low=22000, prior_close=22000)
        d.detect(ctx)
        d.reset()
        assert len(d._session_cache) == 0
        assert len(d._daily_stats) == 0

    def test_prior_day_range_accumulated_across_bars(self):
        """Stats accumulate per-bar; prior day lookup returns correct range."""
        d = MarketStateDetector()
        # Simulate a prior session with high=22300, low=21700 → range = 600/22000 ≈ 2.7%
        d._update_daily_stats('NIFTY', date(2025, 3, 12), 22300, 21700, 22000)
        prior = d._prior_day_range('NIFTY', date(2025, 3, 13))
        assert prior is not None
        assert abs(prior - 600/22000) < 0.0001


class TestStrategyEligibilityPolicy:
    def _state(self, regime):
        from src.strategies.allocator import SessionState
        return SessionState(
            date=_DATE, instrument='NIFTY', regime=regime,
            or_width_pct=0.003, gap_abs_pct=0.007, prior_day_range_pct=0.012,
            is_wide_or=False, is_large_gap=False,
        )

    def test_all_on_allows_everything(self):
        p = StrategyEligibilityPolicy.from_name('all_on')
        for regime in [REGIME_GOOD, REGIME_NEUTRAL, REGIME_BAD, STATE_CHOPPY_BAD]:
            for strat in ['ORB', 'VWAP_PULLBACK', 'GAP_BEHAVIOR', 'ANYTHING']:
                assert p.is_eligible(strat, self._state(regime))

    def test_pullback_only_blocks_orb(self):
        p = StrategyEligibilityPolicy.from_name('vwap_pullback_only')
        for regime in [REGIME_GOOD, REGIME_BAD]:
            assert p.is_eligible('VWAP_PULLBACK', self._state(regime))
            assert not p.is_eligible('ORB', self._state(regime))

    def test_conservative_enables_orb_on_structured_days(self):
        p = StrategyEligibilityPolicy.from_name('conservative')
        assert p.is_eligible('ORB', self._state(REGIME_GOOD))
        assert p.is_eligible('ORB', self._state(REGIME_NEUTRAL))
        assert not p.is_eligible('ORB', self._state(REGIME_BAD))
        assert not p.is_eligible('GAP_BEHAVIOR', self._state(REGIME_GOOD))

    def test_deterministic_enables_reversion_on_bad_orb(self):
        p = StrategyEligibilityPolicy.from_name('deterministic')
        assert p.is_eligible('VWAP_REVERSION', self._state(REGIME_BAD))
        assert not p.is_eligible('VWAP_REVERSION', self._state(REGIME_GOOD))

    def test_deterministic_enables_failure_fade_on_exhaustion(self):
        p = StrategyEligibilityPolicy.from_name('deterministic')
        assert p.is_eligible('OR_FAILURE_FADE', self._state(STATE_EXHAUSTION))
        assert not p.is_eligible('OR_FAILURE_FADE', self._state(REGIME_BAD))

    def test_none_state_blocks_all(self):
        p = StrategyEligibilityPolicy.from_name('all_on')
        assert not p.is_eligible('VWAP_PULLBACK', None)

    def test_unknown_policy_raises(self):
        with pytest.raises(ValueError, match='Unknown policy'):
            StrategyEligibilityPolicy.from_name('made_up')


class TestAllocationGatedStrategy:
    def _make_strategy(self, name='TEST'):
        """Minimal BaseStrategy implementation."""
        from src.strategies.base import BaseStrategy
        from src.core.models import Signal

        class _Stub(BaseStrategy):
            def __init__(self): self.name = name
            def generate_signal(self, ctx):
                return Signal(name, 'NIFTY', datetime(2025,3,13,10,0), 'LONG',
                               'MARKET', 21800.0, 22200.0)
            def explain_no_signal(self, ctx): return 'no_signal'
            def reset(self): pass
        return _Stub()

    def test_passes_through_when_eligible(self):
        strat = self._make_strategy()
        detector = MarketStateDetector()
        policy   = StrategyEligibilityPolicy.from_name('all_on')
        gated    = AllocationGatedStrategy(strat, detector, policy)
        ctx = _ctx(or_width=0.003, gap_pct=0.007,
                    session_high=22220, session_low=22000, prior_close=22000)
        assert gated.generate_signal(ctx) is not None

    def test_blocks_when_not_eligible(self):
        strat    = self._make_strategy('ORB')
        detector = MarketStateDetector()
        policy   = StrategyEligibilityPolicy.from_name('vwap_pullback_only')
        gated    = AllocationGatedStrategy(strat, detector, policy)
        ctx = _ctx(or_width=0.003, gap_pct=0.007,
                    session_high=22220, session_low=22000, prior_close=22000)
        assert gated.generate_signal(ctx) is None

    def test_explain_no_signal_includes_regime(self):
        strat    = self._make_strategy('ORB')
        detector = MarketStateDetector()
        policy   = StrategyEligibilityPolicy.from_name('vwap_pullback_only')
        gated    = AllocationGatedStrategy(strat, detector, policy)
        ctx = _ctx(or_width=0.003, gap_pct=0.007,
                    session_high=22220, session_low=22000, prior_close=22000)
        reason = gated.explain_no_signal(ctx)
        assert 'allocator_blocked' in reason

    def test_blocks_before_or_ready(self):
        strat    = self._make_strategy()
        detector = MarketStateDetector()
        policy   = StrategyEligibilityPolicy.from_name('all_on')
        gated    = AllocationGatedStrategy(strat, detector, policy)
        ctx = _ctx(or_ready=False)   # OR not ready yet
        assert gated.generate_signal(ctx) is None   # blocked regardless of policy

    def test_name_forwarded(self):
        strat = self._make_strategy('VWAP_PULLBACK')
        gated = AllocationGatedStrategy(
            strat, MarketStateDetector(),
            StrategyEligibilityPolicy.from_name('all_on')
        )
        assert gated.name == 'VWAP_PULLBACK'

    def test_wrap_strategies(self):
        strats = [self._make_strategy('ORB'), self._make_strategy('VWAP_PULLBACK')]
        gated  = wrap_strategies(strats, 'conservative')
        assert len(gated) == 2
        assert all(isinstance(s, AllocationGatedStrategy) for s in gated)
        # All share the same detector (cached state per session)
        assert gated[0]._detector is gated[1]._detector
