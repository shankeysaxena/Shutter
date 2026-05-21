"""Tests for ORB strategy signal generation."""
import pytest
from datetime import datetime, date

from src.core.models import (
    Candle, FeatureSnapshot, BarEvent, EngineState, StrategyContext, Trade, Signal
)
from src.strategies.orb import ORBStrategy


def _make_context(
    bar_close, bar_time_str,
    or_high=100.0, or_low=90.0, or_ready=True,
    existing_trades=None,
    config=None,
):
    ts = datetime.strptime(f"2024-01-01 {bar_time_str}", "%Y-%m-%d %H:%M")
    candle = Candle(ts, 'NIFTY', bar_close - 1, bar_close + 1, bar_close - 2, bar_close, 1000)
    features = FeatureSnapshot(
        session_date=date(2024, 1, 1),
        minute_index=30,
        prior_close=95.0,
        vwap=95.0,
        vwap_distance=0.0,
        above_vwap=True,
        below_vwap=False,
        or_high=or_high,
        or_low=or_low,
        or_width=or_high - or_low if or_high and or_low else None,
        or_ready=or_ready,
        gap_pct=0.005,
        gap_direction='UP',
        session_high_so_far=bar_close + 2,
        session_low_so_far=bar_close - 5,
    )
    bar_event = BarEvent(candle=candle, features=features, is_bar_closed=True, runtime_mode='backtest')
    state = EngineState(
        instrument='NIFTY',
        session_date=date(2024, 1, 1),
        open_trades=[],
        closed_trades=existing_trades or [],
        queued_signals=[],
        per_strategy_day_trade_count={'ORB': 0},
    )
    default_config = {'orb': {'enabled': True, 'no_entry_after': '12:00', 'target_r': 2.0}}
    return StrategyContext(
        bar_event=bar_event,
        engine_state=state,
        strategy_config=config or default_config,
    )


def _make_trade(direction):
    return Trade(
        trade_id='test-id',
        strategy_name='ORB',
        instrument='NIFTY',
        direction=direction,
        entry_time=datetime(2024, 1, 1, 9, 35),
        entry_price=101.0,
        stop_price=90.0,
        target_price=123.0,
        exit_time=None,
        exit_price=None,
        exit_reason=None,
        qty=1,
        gross_pnl=0.0,
        net_pnl=0.0,
        r_multiple=None,
        runtime_mode='backtest',
    )


class TestORBStrategy:
    def test_long_signal_on_breakout_above_or_high(self):
        # close > or_high → LONG signal
        ctx = _make_context(bar_close=105.0, bar_time_str='09:35')
        signal = ORBStrategy().generate_signal(ctx)
        assert signal is not None
        assert signal.direction == 'LONG'
        assert signal.stop_price == 90.0

    def test_short_signal_on_breakout_below_or_low(self):
        # close < or_low → SHORT signal
        ctx = _make_context(bar_close=85.0, bar_time_str='09:35')
        signal = ORBStrategy().generate_signal(ctx)
        assert signal is not None
        assert signal.direction == 'SHORT'
        assert signal.stop_price == 100.0

    def test_no_signal_inside_or(self):
        ctx = _make_context(bar_close=95.0, bar_time_str='09:35')
        assert ORBStrategy().generate_signal(ctx) is None

    def test_no_signal_after_cutoff_time(self):
        ctx = _make_context(bar_close=105.0, bar_time_str='12:00')
        assert ORBStrategy().generate_signal(ctx) is None

    def test_no_signal_before_cutoff_but_just_at(self):
        # 11:59 should still be valid
        ctx = _make_context(bar_close=105.0, bar_time_str='11:59')
        assert ORBStrategy().generate_signal(ctx) is not None

    def test_no_signal_when_or_not_ready(self):
        ctx = _make_context(bar_close=105.0, bar_time_str='09:25', or_ready=False)
        assert ORBStrategy().generate_signal(ctx) is None

    def test_no_signal_when_or_high_is_nan(self):
        import math
        ctx = _make_context(bar_close=105.0, bar_time_str='09:35', or_high=float('nan'))
        assert ORBStrategy().generate_signal(ctx) is None

    def test_no_signal_when_or_low_is_none(self):
        ctx = _make_context(bar_close=105.0, bar_time_str='09:35', or_low=None)
        assert ORBStrategy().generate_signal(ctx) is None

    def test_no_long_signal_if_long_already_exists(self):
        existing = [_make_trade('LONG')]
        ctx = _make_context(bar_close=105.0, bar_time_str='09:35', existing_trades=existing)
        assert ORBStrategy().generate_signal(ctx) is None

    def test_short_signal_still_valid_if_only_long_exists(self):
        # 1 trade per direction: existing LONG should NOT block SHORT
        existing = [_make_trade('LONG')]
        ctx = _make_context(bar_close=85.0, bar_time_str='09:35', existing_trades=existing)
        signal = ORBStrategy().generate_signal(ctx)
        assert signal is not None
        assert signal.direction == 'SHORT'

    def test_no_short_signal_if_short_already_exists(self):
        existing = [_make_trade('SHORT')]
        ctx = _make_context(bar_close=85.0, bar_time_str='09:35', existing_trades=existing)
        assert ORBStrategy().generate_signal(ctx) is None

    def test_signal_carries_target_r_in_metadata(self):
        ctx = _make_context(bar_close=105.0, bar_time_str='09:35')
        signal = ORBStrategy().generate_signal(ctx)
        assert signal.metadata.get('target_r') == 2.0

    def test_no_signal_when_disabled(self):
        config = {'orb': {'enabled': False}}
        ctx = _make_context(bar_close=105.0, bar_time_str='09:35', config=config)
        assert ORBStrategy().generate_signal(ctx) is None
