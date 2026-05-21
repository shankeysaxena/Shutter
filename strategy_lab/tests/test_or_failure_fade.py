"""Tests for OR Failure Fade strategy."""
from datetime import date, datetime

import pytest

from src.core.enums import RejectionReason
from src.core.models import (
    BarEvent, Candle, EngineState, FeatureSnapshot, StrategyContext
)
from src.strategies.or_failure_fade import ORFailureFadeStrategy


_DATE  = date(2025, 3, 13)
_CONFIG = {
    'or_failure_fade': {
        'enabled': True,
        'max_failure_bars': 5,
        'stop_buffer_pct': 0.0003,
        'target_type': 'vwap',
        'no_entry_after': '13:30',
        'max_trades_per_day': 2,
    }
}


def _bar(time_str, close, or_high=22100.0, or_low=21900.0, or_ready=True,
          vwap=22000.0, high=None, low=None):
    ts = datetime.strptime(f'2025-03-13 {time_str}', '%Y-%m-%d %H:%M')
    high = high if high is not None else close + 10
    low  = low  if low  is not None else close - 10
    candle   = Candle(ts, 'NIFTY', close, high, low, close, 1000)
    features = FeatureSnapshot(
        session_date=_DATE, minute_index=0, prior_close=22000.0,
        vwap=vwap, vwap_distance=(close-vwap)/vwap,
        above_vwap=close > vwap, below_vwap=close < vwap,
        or_high=or_high, or_low=or_low, or_width=(or_high-or_low)/or_low,
        or_ready=or_ready, gap_pct=0.005, gap_direction='UP',
        session_high_so_far=close+20, session_low_so_far=close-20,
    )
    return BarEvent(candle=candle, features=features, is_bar_closed=True, runtime_mode='backtest')


def _ctx(bar_event, cfg=None):
    state = EngineState(
        instrument='NIFTY', session_date=_DATE,
        open_trades=[], closed_trades=[], queued_signals=[],
        per_strategy_day_trade_count={'OR_FAILURE_FADE': 0},
    )
    return StrategyContext(bar_event=bar_event, engine_state=state,
                            strategy_config=cfg or _CONFIG)


class TestORFailureFadeEntry:
    def test_no_signal_when_disabled(self):
        s = ORFailureFadeStrategy()
        cfg = {'or_failure_fade': {'enabled': False}}
        assert s.generate_signal(_ctx(_bar('10:00', 22150), cfg=cfg)) is None

    def test_no_signal_before_or_ready(self):
        s = ORFailureFadeStrategy()
        ctx = _ctx(_bar('09:20', 22200, or_ready=False))
        assert s.generate_signal(ctx) is None

    def test_no_signal_after_cutoff(self):
        s = ORFailureFadeStrategy()
        assert s.generate_signal(_ctx(_bar('13:30', 22200))) is None

    def test_no_signal_when_price_inside_or(self):
        s = ORFailureFadeStrategy()
        ctx = _ctx(_bar('10:00', 22000))   # inside OR (21900–22100)
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.OR_NOT_BROKEN

    def test_no_signal_on_break_bar_itself(self):
        """Bar that breaks OR does not emit signal — strategy watches for failure."""
        s = ORFailureFadeStrategy()
        sig = s.generate_signal(_ctx(_bar('10:00', 22150)))   # break above
        assert sig is None
        sess = s._session('NIFTY', _DATE)
        assert sess.above.phase == 'BROKEN'

    def test_short_signal_on_failure_reclaim(self):
        """Break above OR_high then reclaim within N bars → SHORT."""
        s = ORFailureFadeStrategy()
        # Bar 1: break above (close=22150 > OR_high=22100)
        s.generate_signal(_ctx(_bar('10:00', 22150, high=22160)))
        # Bar 2: failure — close back below OR_high (22090 < 22100)
        sig = s.generate_signal(_ctx(_bar('10:01', 22090, high=22095)))
        assert sig is not None
        assert sig.direction == 'SHORT'
        assert sig.stop_price > 22150      # above the spike extreme
        assert sig.target_price < 22100   # below OR_high (targeting VWAP=22000)

    def test_long_signal_on_failure_reclaim_below(self):
        """Break below OR_low then reclaim → LONG."""
        s = ORFailureFadeStrategy()
        s.generate_signal(_ctx(_bar('10:00', 21850, low=21840)))   # break below
        sig = s.generate_signal(_ctx(_bar('10:01', 21920, low=21910)))  # reclaim
        assert sig is not None
        assert sig.direction == 'LONG'
        assert sig.stop_price < 21840     # below spike extreme
        assert sig.target_price > 21900  # above OR_low

    def test_stop_includes_buffer(self):
        s = ORFailureFadeStrategy()
        s.generate_signal(_ctx(_bar('10:00', 22200, high=22210)))  # spike to 22210
        sig = s.generate_signal(_ctx(_bar('10:01', 22080)))        # failure
        assert sig is not None
        # stop = 22210 + 22210*0.0003 ≈ 22210 + 6.7 ≈ 22217
        assert abs(sig.stop_price - (22210 + 22210 * 0.0003)) < 1.0

    def test_gives_up_if_breakout_holds(self):
        """Break holds for > max_failure_bars without reclaim → back to IDLE."""
        s = ORFailureFadeStrategy()
        for i in range(6):   # 6 bars above OR_high (> max_failure_bars=5)
            s.generate_signal(_ctx(_bar(f'10:0{i}', 22150, high=22160)))
        sess = s._session('NIFTY', _DATE)
        assert sess.above.phase == 'IDLE'   # gave up, it's a real break

    def test_tracks_highest_extreme_during_break(self):
        """Extreme should be the maximum high seen while above OR, not just first bar."""
        s = ORFailureFadeStrategy()
        s.generate_signal(_ctx(_bar('10:00', 22150, high=22160)))
        s.generate_signal(_ctx(_bar('10:01', 22180, high=22200)))  # higher spike
        sig = s.generate_signal(_ctx(_bar('10:02', 22090)))        # failure
        assert sig.metadata['extreme'] == pytest.approx(22200, abs=1)

    def test_used_blocks_repeat_same_direction(self):
        s = ORFailureFadeStrategy()
        s.generate_signal(_ctx(_bar('10:00', 22150, high=22160)))
        s.generate_signal(_ctx(_bar('10:01', 22090)))   # signal fires → USED

        # Try another break + failure — same direction should not fire again
        s.generate_signal(_ctx(_bar('10:05', 22200, high=22210)))
        sig = s.generate_signal(_ctx(_bar('10:06', 22050)))
        assert sig is None

    def test_both_directions_can_fire_independently(self):
        """Long and short fade are independent — both can fire in same session."""
        s = ORFailureFadeStrategy()
        # First: failed OR_high break → SHORT
        s.generate_signal(_ctx(_bar('10:00', 22150, high=22160)))
        sig1 = s.generate_signal(_ctx(_bar('10:01', 22090)))
        assert sig1 is not None and sig1.direction == 'SHORT'

        # Then: failed OR_low break → LONG
        s.generate_signal(_ctx(_bar('10:10', 21850, low=21840)))
        sig2 = s.generate_signal(_ctx(_bar('10:11', 21920)))
        assert sig2 is not None and sig2.direction == 'LONG'

    def test_target_falls_back_to_or_midpoint_if_vwap_invalid(self):
        """If VWAP target is on wrong side, fall back to OR midpoint."""
        s = ORFailureFadeStrategy()
        # VWAP above OR_high — invalid as SHORT target
        s.generate_signal(_ctx(_bar('10:00', 22150, vwap=22300, high=22160)))
        sig = s.generate_signal(_ctx(_bar('10:01', 22090, vwap=22300)))
        assert sig is not None
        # Should use OR midpoint (22000) not VWAP (22300)
        assert sig.target_price == pytest.approx(22000.0, abs=1)

    def test_reset_clears_all_state(self):
        s = ORFailureFadeStrategy()
        s.generate_signal(_ctx(_bar('10:00', 22150, high=22160)))
        s.generate_signal(_ctx(_bar('10:01', 22090)))
        s.reset()
        assert len(s._states) == 0

    def test_metadata_keys(self):
        s = ORFailureFadeStrategy()
        s.generate_signal(_ctx(_bar('10:00', 22150, high=22160)))
        sig = s.generate_signal(_ctx(_bar('10:01', 22090)))
        for key in ('boundary', 'extreme', 'breakout_size', 'break_bars', 'or_mid'):
            assert key in sig.metadata
        assert sig.metadata['breakout_size'] == pytest.approx(22160 - 22100, abs=1)
