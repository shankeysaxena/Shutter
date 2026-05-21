"""
Phase 5B tests — fast-iteration allocator, compression breakout,
weekly report, disabled-strategy enforcement.
"""
from collections import deque
from datetime import date, datetime
from typing import Optional

import pytest

from src.core.enums import RejectionReason
from src.core.models import (
    BarEvent, Candle, EngineState, FeatureSnapshot, StrategyContext
)
from src.strategies.allocator import (
    NAMED_POLICIES,
    STATE_LOW_VOL_COMP,
    MarketStateDetector,
    StrategyEligibilityPolicy,
    wrap_strategies,
    REGIME_BAD,
    REGIME_NEUTRAL,
    REGIME_GOOD,
    STATE_EXHAUSTION,
)
from src.strategies.compression_breakout import CompressionBreakoutStrategy

_DATE = date(2025, 3, 13)


# ─── helpers ─────────────────────────────────────────────────────────────────

def _bar(ts_str, close, vwap=22000, atr=15.0, vwap_dist=0.0,
          sh=22100, sl=21900, or_w=0.003, or_ready=True,
          high=None, low=None):
    ts = datetime.strptime(f'2025-03-13 {ts_str}', '%Y-%m-%d %H:%M')
    candle = Candle(ts, 'NIFTY', close, high or close+5, low or close-5, close, 1000)
    feat = FeatureSnapshot(
        session_date=_DATE, minute_index=0, prior_close=vwap,
        vwap=vwap, vwap_distance=vwap_dist,
        above_vwap=close > vwap, below_vwap=close < vwap,
        or_high=vwap+50, or_low=vwap-50, or_width=or_w, or_ready=or_ready,
        gap_pct=0.005, gap_direction='UP',
        session_high_so_far=sh, session_low_so_far=sl,
        intraday_atr=atr, vwap_atr_distance=(close-vwap)/atr if atr else 0,
    )
    state = EngineState(
        'NIFTY', _DATE, [], [], [],
        {'COMPRESSION_BREAKOUT': 0, 'VWAP_PULLBACK': 0},
    )
    return BarEvent(candle, feat, True, 'backtest'), state


def _cfg(**overrides):
    base = {
        'compression_breakout': {
            'enabled': True,
            'entry_start': '10:00',
            'no_entry_after': '15:00',
            'min_compression_bars': 5,    # small for tests
            'max_range_atr_ratio': 0.4,
            'max_vwap_distance_pct': 0.003,
            'stop_buffer_pct': 0.0003,
            'atr_target_multiplier': 1.5,
            'max_trades_per_day': 2,
        }
    }
    base['compression_breakout'].update(overrides)
    return base


# ─── CompressionBreakoutStrategy ─────────────────────────────────────────────

class TestCompressionBreakout:
    def test_no_signal_before_10am(self):
        s = CompressionBreakoutStrategy()
        be, st = _bar('09:30', 22000)
        ctx = StrategyContext(be, st, _cfg())
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.OUTSIDE_ENTRY_TIME

    def test_no_signal_after_3pm(self):
        s = CompressionBreakoutStrategy()
        be, st = _bar('15:00', 22000)
        ctx = StrategyContext(be, st, _cfg())
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.OUTSIDE_ENTRY_TIME

    def test_no_signal_when_atr_not_warm(self):
        s = CompressionBreakoutStrategy()
        be, st = _bar('10:30', 22000, atr=None)
        be.features.intraday_atr = None
        ctx = StrategyContext(be, st, _cfg())
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.ATR_NOT_WARM

    def test_idle_before_compression_window_full(self):
        s = CompressionBreakoutStrategy()
        # Feed only 3 bars (< min_compression_bars=5)
        for i in range(3):
            be, st = _bar(f'10:0{i}', 22000)
            ctx = StrategyContext(be, st, _cfg())
            s.generate_signal(ctx)
        st2 = s._state('NIFTY', _DATE)
        assert st2.phase == 'IDLE'

    def test_transitions_to_compressed(self):
        """5 narrow bars (H-L=2pts) around VWAP → COMPRESSED state."""
        s = CompressionBreakoutStrategy()
        cfg = _cfg()
        # Tight bars: high=close+1, low=close-1 → window range = 22001-21999 = 2pts
        # 2pts / ATR 15 = 0.13 < max_range_atr_ratio 0.4 → compressed
        for i in range(5):
            be, st = _bar(f'10:0{i}', 22000,
                           high=22001, low=21999,
                           sh=22001, sl=21999, atr=15.0, vwap_dist=0.0)
            ctx = StrategyContext(be, st, cfg)
            s.generate_signal(ctx)
        state = s._state('NIFTY', _DATE)
        assert state.phase == 'COMPRESSED'

    def test_long_signal_on_upside_breakout(self):
        """After compression (window range=2pts), bar closes above comp_high → LONG."""
        s = CompressionBreakoutStrategy()
        cfg = _cfg()
        for i in range(5):
            be, st = _bar(f'10:0{i}', 22000,
                           high=22001, low=21999,
                           sh=22001, sl=21999, atr=15.0, vwap_dist=0.0)
            s.generate_signal(StrategyContext(be, st, cfg))
        # comp_high ≈ 22001; breakout bar closes above it
        be_b, st_b = _bar('10:05', 22020, high=22025, low=22000,
                            sh=22025, sl=21999, atr=15.0)
        sig = s.generate_signal(StrategyContext(be_b, st_b, cfg))
        assert sig is not None
        assert sig.direction == 'LONG'
        assert sig.stop_price < 21999   # below comp_low
        assert sig.target_price > 22020

    def test_short_signal_on_downside_breakout(self):
        s = CompressionBreakoutStrategy()
        cfg = _cfg()
        for i in range(5):
            be, st = _bar(f'10:0{i}', 22000,
                           high=22001, low=21999,
                           sh=22001, sl=21999, atr=15.0, vwap_dist=0.0)
            s.generate_signal(StrategyContext(be, st, cfg))
        # comp_low ≈ 21999; breakout bar closes below it
        be_b, st_b = _bar('10:05', 21980, high=21999, low=21975,
                            sh=22001, sl=21975, atr=15.0)
        sig = s.generate_signal(StrategyContext(be_b, st_b, cfg))
        assert sig is not None
        assert sig.direction == 'SHORT'

    def test_reset_clears_state(self):
        s = CompressionBreakoutStrategy()
        s._state('NIFTY', _DATE).phase = 'COMPRESSED'
        s.reset()
        assert len(s._states) == 0


# ─── fast_iter_allocator eligibility ─────────────────────────────────────────

class TestFastIterAllocator:
    def _state(self, regime):
        from src.strategies.allocator import SessionState
        return SessionState(
            date=_DATE, instrument='NIFTY', regime=regime,
            or_width_pct=0.003, gap_abs_pct=0.007,
            prior_day_range_pct=0.012,
            is_wide_or=False, is_large_gap=False,
        )

    def test_vwap_pullback_enabled_in_all_regimes(self):
        p = StrategyEligibilityPolicy.from_name('fast_iter_allocator')
        for regime in [REGIME_GOOD, REGIME_NEUTRAL, REGIME_BAD,
                        STATE_EXHAUSTION, STATE_LOW_VOL_COMP]:
            assert p.is_eligible('VWAP_PULLBACK', self._state(regime))

    def test_orb_never_enabled(self):
        p = StrategyEligibilityPolicy.from_name('fast_iter_allocator')
        for regime in [REGIME_GOOD, REGIME_NEUTRAL, REGIME_BAD,
                        STATE_EXHAUSTION, STATE_LOW_VOL_COMP]:
            assert not p.is_eligible('ORB', self._state(regime))

    def test_gap_behavior_never_enabled(self):
        p = StrategyEligibilityPolicy.from_name('fast_iter_allocator')
        for regime in [REGIME_GOOD, REGIME_NEUTRAL, REGIME_BAD]:
            assert not p.is_eligible('GAP_BEHAVIOR', self._state(regime))

    def test_iron_fly_never_enabled(self):
        p = StrategyEligibilityPolicy.from_name('fast_iter_allocator')
        for regime in [REGIME_GOOD, REGIME_BAD]:
            assert not p.is_eligible('IRON_FLY', self._state(regime))

    def test_vwap_reversion_only_bad_orb(self):
        p = StrategyEligibilityPolicy.from_name('fast_iter_allocator')
        assert p.is_eligible('VWAP_REVERSION', self._state(REGIME_BAD))
        assert not p.is_eligible('VWAP_REVERSION', self._state(REGIME_NEUTRAL))
        assert not p.is_eligible('VWAP_REVERSION', self._state(REGIME_GOOD))

    def test_or_failure_fade_only_exhaustion(self):
        p = StrategyEligibilityPolicy.from_name('fast_iter_allocator')
        assert p.is_eligible('OR_FAILURE_FADE', self._state(STATE_EXHAUSTION))
        assert not p.is_eligible('OR_FAILURE_FADE', self._state(REGIME_BAD))

    def test_compression_breakout_only_when_compression_detected(self):
        """
        COMPRESSION_BREAKOUT requires compression_detected=True.
        state.regime is the BASE regime and never becomes LOW_VOL_COMP directly —
        compression_detected is an additive flag checked by is_eligible.
        """
        p = StrategyEligibilityPolicy.from_name('fast_iter_allocator')

        # Without compression — COMPRESSION_BREAKOUT blocked in all base regimes
        assert not p.is_eligible('COMPRESSION_BREAKOUT', self._state(REGIME_NEUTRAL))
        assert not p.is_eligible('COMPRESSION_BREAKOUT', self._state(REGIME_GOOD))
        assert not p.is_eligible('COMPRESSION_BREAKOUT', self._state(REGIME_BAD))

        # With compression_detected=True — COMPRESSION_BREAKOUT unlocked via LOW_VOL_COMP bucket
        from src.strategies.allocator import SessionState
        bad_orb_with_compression = SessionState(
            date=_DATE, instrument='NIFTY', regime=REGIME_BAD,
            or_width_pct=0.007, gap_abs_pct=0.003,
            prior_day_range_pct=0.012,
            is_wide_or=True, is_large_gap=False,
            compression_detected=True,   # mid-session coil on a bad-OR day
        )
        assert p.is_eligible('COMPRESSION_BREAKOUT', bad_orb_with_compression)

    def test_bad_orb_plus_compression_grants_both_strategies(self):
        """
        BAD_ORB + compression_detected=True should allow BOTH:
          - VWAP_REVERSION  (from BAD_ORB base bucket)
          - COMPRESSION_BREAKOUT (from LOW_VOL_COMP compression bucket)
        Regime replacement would have broken VWAP_REVERSION; additive model fixes this.
        """
        from src.strategies.allocator import SessionState
        p = StrategyEligibilityPolicy.from_name('fast_iter_allocator')

        state = SessionState(
            date=_DATE, instrument='NIFTY', regime=REGIME_BAD,
            or_width_pct=0.007, gap_abs_pct=0.001,
            prior_day_range_pct=0.015,
            is_wide_or=True, is_large_gap=False,
            compression_detected=True,
        )
        assert p.is_eligible('VWAP_PULLBACK',        state)   # always
        assert p.is_eligible('VWAP_REVERSION',       state)   # from BAD_ORB bucket
        assert p.is_eligible('COMPRESSION_BREAKOUT', state)   # from LOW_VOL_COMP bucket
        assert not p.is_eligible('ORB',              state)   # never
        assert not p.is_eligible('GAP_BEHAVIOR',     state)   # never


# ─── Weekly report ────────────────────────────────────────────────────────────

class TestWeeklyReport:
    def _make_trades(self, pnls, strategy='VWAP_PULLBACK', instrument='NIFTY'):
        return [
            {
                'strategy': strategy, 'instrument': instrument,
                'net_pnl': p, 'exit_reason': 'TARGET' if p > 0 else 'STOP',
                'entry_time': '2025-03-13 10:30:00',
            }
            for p in pnls
        ]

    def test_aggregates_pnl_correctly(self):
        from src.analytics.weekly_report import _strategy_metrics
        trades = self._make_trades([500, -200, 300, -100, 400])
        m = _strategy_metrics(trades)
        assert m['n_trades'] == 5
        assert m['total_pnl'] == pytest.approx(900)
        assert m['win_rate'] == pytest.approx(0.6)

    def test_pf_calculated(self):
        from src.analytics.weekly_report import _strategy_metrics
        trades = self._make_trades([500, 300, -200])
        m = _strategy_metrics(trades)
        # gross wins=800, gross losses=200, PF=4.0
        assert m['profit_factor'] == pytest.approx(4.0)

    def test_decision_keep(self):
        from src.analytics.weekly_report import _decision
        assert 'KEEP' in _decision({'n_trades': 12, 'profit_factor': 1.3})

    def test_decision_disable(self):
        from src.analytics.weekly_report import _decision
        assert 'DISABLE' in _decision({'n_trades': 15, 'profit_factor': 0.6})

    def test_decision_observe(self):
        from src.analytics.weekly_report import _decision
        assert 'OBSERVE' in _decision({'n_trades': 10, 'profit_factor': 1.0})

    def test_decision_insufficient_data(self):
        from src.analytics.weekly_report import _decision
        assert 'INSUFFICIENT' in _decision({'n_trades': 3, 'profit_factor': 0.5})

    def test_report_from_empty_dir(self, tmp_path):
        from src.analytics.weekly_report import weekly_report
        r = weekly_report(str(tmp_path), days=7)
        assert r['total_trades'] == 0
        assert r['strategies'] == {}

    def test_report_aggregates_across_days(self, tmp_path):
        import json
        from src.analytics.weekly_report import weekly_report
        from datetime import date, timedelta

        today = date.today()
        for i in range(3):
            d = today - timedelta(days=i)
            path = tmp_path / f"{d}.json"
            path.write_text(json.dumps({
                'session_date': str(d),
                'experiment_name': 'test',
                'instruments': ['NIFTY'],
                'total_trades': 2,
                'total_net_pnl': 500,
                'win_rate': 0.5,
                'halted': False,
                'halt_reason': '',
                'entries_accepted': 2,
                'fills_closed': 2,
                'trades': [
                    {'strategy': 'VWAP_PULLBACK', 'instrument': 'NIFTY',
                      'net_pnl': 300, 'exit_reason': 'TARGET',
                      'entry_time': f'{d} 10:30:00'},
                    {'strategy': 'VWAP_PULLBACK', 'instrument': 'NIFTY',
                      'net_pnl': -200, 'exit_reason': 'STOP',
                      'entry_time': f'{d} 11:00:00'},
                ],
            }))

        r = weekly_report(str(tmp_path), days=7)
        assert r['total_trades'] == 6
        assert r['total_pnl'] == pytest.approx(300)
        assert 'VWAP_PULLBACK' in r['strategies']
