"""
Tests for GapBehaviorStrategy signal generation.

Key invariants:
- Only trade if abs(gap_pct) >= gap_threshold_pct
- OR must be ready before any signal
- Gap up + break above OR_high → LONG (continuation)
- Gap up + break below OR_low  → SHORT (fill)
- Gap down + break below OR_low  → SHORT (continuation)
- Gap down + break above OR_high → LONG (fill)
- Max 1 trade per session (any direction)
- No signal when close is inside OR band
"""
import math
import pytest
from datetime import datetime, date
from typing import Optional

from src.core.models import (
    Candle, FeatureSnapshot, BarEvent, Signal, Trade,
    EngineState, StrategyContext,
)
from src.core.enums import RejectionReason
from src.strategies.gap_behavior import GapBehaviorStrategy


# --- Helpers ---

def _make_ctx(
    bar_time_str: str = "10:00",
    close: float = 100.0,
    or_high: float = 102.0,
    or_low: float = 98.0,
    gap_pct: float = 0.01,   # 1% gap up by default
    or_ready: bool = True,
    session_date: date = date(2024, 1, 2),
    closed_trades=None,
    open_trades=None,
    queued_signals=None,
    config: dict = None,
):
    ts = datetime.strptime(f"2024-01-02 {bar_time_str}", "%Y-%m-%d %H:%M")
    candle = Candle(ts, 'NIFTY', close - 1, close + 1, close - 1, close, 1000)

    or_width = (or_high - or_low) if (or_high is not None and or_low is not None
                                       and not (isinstance(or_high, float) and math.isnan(or_high))
                                       and not (isinstance(or_low, float) and math.isnan(or_low))) else None
    features = FeatureSnapshot(
        session_date=session_date,
        minute_index=45,
        prior_close=close * (1 - gap_pct) if gap_pct is not None else close,
        vwap=close,
        vwap_distance=0.0,
        above_vwap=True,
        below_vwap=False,
        or_high=or_high,
        or_low=or_low,
        or_width=or_width,
        or_ready=or_ready,
        gap_pct=gap_pct,
        gap_direction='up' if gap_pct and gap_pct > 0 else ('down' if gap_pct and gap_pct < 0 else None),
        session_high_so_far=close + 2,
        session_low_so_far=close - 2,
    )
    bar_event = BarEvent(candle=candle, features=features, is_bar_closed=True, runtime_mode='backtest')

    state = EngineState(
        instrument='NIFTY',
        session_date=session_date,
        open_trades=open_trades or [],
        closed_trades=closed_trades or [],
        queued_signals=queued_signals or [],
        per_strategy_day_trade_count={},
    )

    if config is None:
        config = {
            'gap_behavior': {
                'enabled': True,
                'gap_threshold_pct': 0.005,
                'target_r': 2.0,
            }
        }

    return StrategyContext(
        bar_event=bar_event,
        engine_state=state,
        strategy_config=config,
    )


def _make_trade(direction: str, strategy_name: str = 'GAP_BEHAVIOR') -> Trade:
    ts = datetime.strptime("2024-01-02 09:30", "%Y-%m-%d %H:%M")
    return Trade(
        trade_id='t1',
        strategy_name=strategy_name,
        instrument='NIFTY',
        direction=direction,
        entry_time=ts,
        entry_price=100.0,
        stop_price=98.0,
        target_price=104.0,
        qty=25,
        exit_time=None,
        exit_price=None,
        exit_reason=None,
        gross_pnl=0.0,
        net_pnl=0.0,
        r_multiple=None,
        runtime_mode='backtest',
    )


def _make_queued_signal(direction: str = 'LONG') -> Signal:
    ts = datetime.strptime("2024-01-02 09:45", "%Y-%m-%d %H:%M")
    return Signal(
        strategy_name='GAP_BEHAVIOR',
        instrument='NIFTY',
        timestamp=ts,
        direction=direction,
        entry_type='MARKET',
        stop_price=98.0,
        target_price=0.0,
        metadata={'target_r': 2.0},
    )


# --- Tests ---

class TestGapContinuation:
    """Gap up → break above OR_high = LONG continuation."""

    def test_gap_up_continuation_long(self):
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.01)
        sig = strategy.generate_signal(ctx)
        assert sig is not None
        assert sig.direction == 'LONG'
        assert sig.metadata['setup'] == 'gap_continuation'
        assert sig.stop_price == 98.0

    def test_gap_down_continuation_short(self):
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=97.0, or_high=102.0, or_low=98.0, gap_pct=-0.01)
        sig = strategy.generate_signal(ctx)
        assert sig is not None
        assert sig.direction == 'SHORT'
        assert sig.metadata['setup'] == 'gap_continuation'
        assert sig.stop_price == 102.0

    def test_gap_up_fill_short(self):
        """Gap up but price breaks below OR_low → SHORT (gap fill)."""
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=97.0, or_high=102.0, or_low=98.0, gap_pct=0.01)
        sig = strategy.generate_signal(ctx)
        assert sig is not None
        assert sig.direction == 'SHORT'
        assert sig.metadata['setup'] == 'gap_fill'
        assert sig.stop_price == 102.0

    def test_gap_down_fill_long(self):
        """Gap down but price breaks above OR_high → LONG (gap fill)."""
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=-0.01)
        sig = strategy.generate_signal(ctx)
        assert sig is not None
        assert sig.direction == 'LONG'
        assert sig.metadata['setup'] == 'gap_fill'
        assert sig.stop_price == 98.0

    def test_target_r_in_metadata(self):
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.01)
        sig = strategy.generate_signal(ctx)
        assert sig.metadata['target_r'] == 2.0

    def test_gap_pct_in_metadata(self):
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.01)
        sig = strategy.generate_signal(ctx)
        assert sig.metadata['gap_pct'] == pytest.approx(0.01)


class TestGapFilters:
    """Filters: threshold, OR ready, inside band."""

    def test_no_signal_inside_or_band(self):
        """Close between OR_low and OR_high → no signal."""
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=100.0, or_high=102.0, or_low=98.0, gap_pct=0.01)
        assert strategy.generate_signal(ctx) is None

    def test_no_signal_gap_below_threshold(self):
        """gap_pct < gap_threshold_pct → filtered out."""
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.002)
        assert strategy.generate_signal(ctx) is None

    def test_no_signal_gap_none(self):
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=None)
        assert strategy.generate_signal(ctx) is None

    def test_no_signal_or_not_ready(self):
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.01, or_ready=False)
        assert strategy.generate_signal(ctx) is None

    def test_no_signal_or_high_nan(self):
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=float('nan'), or_low=98.0, gap_pct=0.01)
        assert strategy.generate_signal(ctx) is None

    def test_no_signal_or_low_nan(self):
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=float('nan'), gap_pct=0.01)
        assert strategy.generate_signal(ctx) is None

    def test_no_signal_or_none(self):
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=None, or_low=None, gap_pct=0.01)
        assert strategy.generate_signal(ctx) is None

    def test_gap_exactly_at_threshold_fires(self):
        """gap_pct == gap_threshold_pct: abs(0.005) < 0.005 is False → filter passes → signal fires."""
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.005)
        sig = strategy.generate_signal(ctx)
        assert sig is not None  # at-threshold is NOT filtered (strict < check)

    def test_gap_just_above_threshold_fires(self):
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.0051)
        sig = strategy.generate_signal(ctx)
        assert sig is not None

    def test_disabled_strategy(self):
        strategy = GapBehaviorStrategy()
        config = {'gap_behavior': {'enabled': False}}
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.01, config=config)
        assert strategy.generate_signal(ctx) is None

    def test_no_signal_after_cutoff(self):
        strategy = GapBehaviorStrategy()
        config = {'gap_behavior': {'enabled': True, 'gap_threshold_pct': 0.005,
                                   'no_entry_after': '12:30', 'target_r': 2.0}}
        ctx = _make_ctx(bar_time_str='12:30', close=103.0, or_high=102.0, or_low=98.0,
                        gap_pct=0.01, config=config)
        assert strategy.generate_signal(ctx) is None

    def test_signal_before_cutoff(self):
        strategy = GapBehaviorStrategy()
        config = {'gap_behavior': {'enabled': True, 'gap_threshold_pct': 0.005,
                                   'no_entry_after': '12:30', 'target_r': 2.0}}
        ctx = _make_ctx(bar_time_str='12:29', close=103.0, or_high=102.0, or_low=98.0,
                        gap_pct=0.01, config=config)
        assert strategy.generate_signal(ctx) is not None

    def test_explain_after_cutoff(self):
        strategy = GapBehaviorStrategy()
        config = {'gap_behavior': {'enabled': True, 'gap_threshold_pct': 0.005,
                                   'no_entry_after': '12:30', 'target_r': 2.0}}
        ctx = _make_ctx(bar_time_str='13:00', close=103.0, or_high=102.0, or_low=98.0,
                        gap_pct=0.01, config=config)
        assert strategy.explain_no_signal(ctx) == RejectionReason.AFTER_CUTOFF


class TestMaxOneTrade:
    """Max 1 trade per session."""

    def test_no_second_signal_after_closed_trade(self):
        strategy = GapBehaviorStrategy()
        trade = _make_trade('LONG')
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.01,
                        closed_trades=[trade])
        assert strategy.generate_signal(ctx) is None

    def test_no_second_signal_after_open_trade(self):
        strategy = GapBehaviorStrategy()
        trade = _make_trade('LONG')
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.01,
                        open_trades=[trade])
        assert strategy.generate_signal(ctx) is None

    def test_no_second_signal_after_queued_signal(self):
        strategy = GapBehaviorStrategy()
        qs = _make_queued_signal('LONG')
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.01,
                        queued_signals=[qs])
        assert strategy.generate_signal(ctx) is None

    def test_other_strategy_trade_does_not_block(self):
        """A trade from a different strategy should not block GAP_BEHAVIOR."""
        strategy = GapBehaviorStrategy()
        trade = _make_trade('LONG', strategy_name='ORB')
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.01,
                        closed_trades=[trade])
        sig = strategy.generate_signal(ctx)
        assert sig is not None


class TestExplainNoSignal:
    """explain_no_signal returns correct RejectionReason."""

    def test_disabled(self):
        strategy = GapBehaviorStrategy()
        config = {'gap_behavior': {'enabled': False}}
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.01, config=config)
        assert strategy.explain_no_signal(ctx) == RejectionReason.DISABLED

    def test_gap_not_available(self):
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=None)
        assert strategy.explain_no_signal(ctx) == RejectionReason.GAP_NOT_AVAILABLE

    def test_gap_below_threshold(self):
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.001)
        assert strategy.explain_no_signal(ctx) == RejectionReason.GAP_BELOW_THRESHOLD

    def test_or_not_ready(self):
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=103.0, or_high=102.0, or_low=98.0, gap_pct=0.01, or_ready=False)
        assert strategy.explain_no_signal(ctx) == RejectionReason.OR_NOT_READY

    def test_already_traded(self):
        strategy = GapBehaviorStrategy()
        trade = _make_trade('LONG')
        ctx = _make_ctx(close=100.0, or_high=102.0, or_low=98.0, gap_pct=0.01,
                        closed_trades=[trade])
        assert strategy.explain_no_signal(ctx) == RejectionReason.ALREADY_TRADED

    def test_no_breakout(self):
        """Price inside OR band with valid gap → NO_BREAKOUT."""
        strategy = GapBehaviorStrategy()
        ctx = _make_ctx(close=100.0, or_high=102.0, or_low=98.0, gap_pct=0.01)
        assert strategy.explain_no_signal(ctx) == RejectionReason.NO_BREAKOUT
