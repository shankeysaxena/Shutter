"""
Tests for VWAPPullbackStrategy state machine and signal generation.

Key invariants:
- LONG: trend above VWAP → pullback near VWAP → recapture → signal
- SHORT: trend below VWAP → pullback near VWAP → rejected back → signal
- Invalidation: price crosses VWAP decisively
- Max 2 trades per day (across LONG + SHORT)
- State resets each new session_date
"""
import pytest
from datetime import datetime, date, time

from src.core.models import (
    Candle, FeatureSnapshot, BarEvent, EngineState, StrategyContext,
)
from src.core.enums import RejectionReason
from src.strategies.vwap_pullback import VWAPPullbackStrategy, _DirectionalState, _advance


# --- Helpers ---

def _make_ctx(
    bar_time_str: str,
    close: float,
    vwap: float,
    bar_low: float = None,
    bar_high: float = None,
    session_date: date = date(2024, 1, 2),
    existing_trades=None,
    queued_signals=None,
    per_strategy_count: int = 0,
    config: dict = None,
):
    if bar_low is None:
        bar_low = close - 2
    if bar_high is None:
        bar_high = close + 2

    ts = datetime.strptime(f"2024-01-02 {bar_time_str}", "%Y-%m-%d %H:%M")
    candle = Candle(ts, 'NIFTY', close - 1, bar_high, bar_low, close, 1000)

    vwap_dist = (close - vwap) / vwap if vwap > 0 else None
    features = FeatureSnapshot(
        session_date=session_date,
        minute_index=30,
        prior_close=vwap,
        vwap=vwap,
        vwap_distance=vwap_dist,
        above_vwap=close > vwap,
        below_vwap=close < vwap,
        or_high=None, or_low=None, or_width=None, or_ready=False,
        gap_pct=None, gap_direction=None,
        session_high_so_far=None, session_low_so_far=None,
    )
    bar_event = BarEvent(candle=candle, features=features, is_bar_closed=True, runtime_mode='backtest')
    state = EngineState(
        instrument='NIFTY',
        session_date=session_date,
        open_trades=[],
        closed_trades=[],
        queued_signals=queued_signals or [],
        per_strategy_day_trade_count={'VWAP_PULLBACK': per_strategy_count},
    )
    if existing_trades:
        state.closed_trades = existing_trades
    default_cfg = {
        'vwap_pullback': {
            'enabled': True,
            'no_entry_after': '13:30',
            'max_trades_per_day': 2,
            'target_r': 2.0,
            'min_trend_bars': 3,
            'pullback_zone_pct': 0.003,
        }
    }
    return StrategyContext(
        bar_event=bar_event,
        engine_state=state,
        strategy_config=config or default_cfg,
    )


def _feed_bars(strategy: VWAPPullbackStrategy, bars: list) -> list:
    """Feed a sequence of (bar_time, close, vwap, low, high) tuples. Returns list of signals."""
    signals = []
    for bar_time, close, vwap, low, high in bars:
        ctx = _make_ctx(bar_time, close, vwap, bar_low=low, bar_high=high)
        s = strategy.generate_signal(ctx)
        signals.append(s)
    return signals


# --- State machine unit tests ---

class TestDirectionalStateAdvance:
    def _ds(self) -> _DirectionalState:
        return _DirectionalState()

    def _features(self, close, vwap):
        dist = (close - vwap) / vwap
        from types import SimpleNamespace
        return SimpleNamespace(
            above_vwap=close > vwap,
            below_vwap=close < vwap,
            vwap_distance=dist,
        )

    def _candle(self, close, low=None, high=None):
        from types import SimpleNamespace
        return SimpleNamespace(low=low or close - 2, high=high or close + 2)

    def test_idle_to_trend_after_min_bars(self):
        ds = self._ds()
        vwap = 100.0
        for _ in range(3):
            _advance(ds, 'LONG', self._candle(105), self._features(105, vwap), 0.003, 3)
        assert ds.phase == 'TREND'

    def test_idle_resets_on_below_vwap(self):
        ds = self._ds()
        vwap = 100.0
        _advance(ds, 'LONG', self._candle(105), self._features(105, vwap), 0.003, 3)
        _advance(ds, 'LONG', self._candle(95), self._features(95, vwap), 0.003, 3)
        assert ds.phase == 'IDLE'
        assert ds.consecutive_trend_bars == 0

    def test_trend_to_pullback_when_near_vwap(self):
        ds = self._ds()
        vwap = 100.0
        for _ in range(3):
            _advance(ds, 'LONG', self._candle(105), self._features(105, vwap), 0.003, 3)
        assert ds.phase == 'TREND'
        # close = 100.2, dist = 0.002 < 0.003 pullback zone
        _advance(ds, 'LONG', self._candle(100.2, low=99.8), self._features(100.2, vwap), 0.003, 3)
        assert ds.phase == 'PULLBACK'

    def test_pullback_to_signal_ready_on_recapture(self):
        ds = self._ds()
        vwap = 100.0
        for _ in range(3):
            _advance(ds, 'LONG', self._candle(105), self._features(105, vwap), 0.003, 3)
        # Enter pullback
        _advance(ds, 'LONG', self._candle(100.2, low=99.8), self._features(100.2, vwap), 0.003, 3)
        assert ds.phase == 'PULLBACK'
        # Recapture — well above vwap
        _advance(ds, 'LONG', self._candle(105), self._features(105, vwap), 0.003, 3)
        assert ds.phase == 'SIGNAL_READY'

    def test_pullback_tracks_lowest_low(self):
        ds = self._ds()
        vwap = 100.0
        for _ in range(3):
            _advance(ds, 'LONG', self._candle(105), self._features(105, vwap), 0.003, 3)
        _advance(ds, 'LONG', self._candle(100.2, low=99.8), self._features(100.2, vwap), 0.003, 3)
        _advance(ds, 'LONG', self._candle(100.1, low=99.5), self._features(100.1, vwap), 0.003, 3)
        assert abs(ds.pullback_extreme - 99.5) < 0.001

    def test_trend_invalidated_on_close_below_vwap(self):
        ds = self._ds()
        vwap = 100.0
        for _ in range(3):
            _advance(ds, 'LONG', self._candle(105), self._features(105, vwap), 0.003, 3)
        _advance(ds, 'LONG', self._candle(98), self._features(98, vwap), 0.003, 3)
        assert ds.phase == 'INVALIDATED'

    def test_pullback_invalidated_on_close_below_vwap(self):
        ds = self._ds()
        vwap = 100.0
        for _ in range(3):
            _advance(ds, 'LONG', self._candle(105), self._features(105, vwap), 0.003, 3)
        _advance(ds, 'LONG', self._candle(100.2, low=99.8), self._features(100.2, vwap), 0.003, 3)
        _advance(ds, 'LONG', self._candle(98), self._features(98, vwap), 0.003, 3)
        assert ds.phase == 'INVALIDATED'

    def test_short_idle_to_trend(self):
        ds = self._ds()
        vwap = 100.0
        for _ in range(3):
            _advance(ds, 'SHORT', self._candle(95), self._features(95, vwap), 0.003, 3)
        assert ds.phase == 'TREND'

    def test_short_trend_to_pullback_near_vwap(self):
        ds = self._ds()
        vwap = 100.0
        for _ in range(3):
            _advance(ds, 'SHORT', self._candle(95), self._features(95, vwap), 0.003, 3)
        # dist = -0.002, > -0.003 threshold
        _advance(ds, 'SHORT', self._candle(99.8, high=100.2), self._features(99.8, vwap), 0.003, 3)
        assert ds.phase == 'PULLBACK'

    def test_short_pullback_to_signal_ready(self):
        ds = self._ds()
        vwap = 100.0
        for _ in range(3):
            _advance(ds, 'SHORT', self._candle(95), self._features(95, vwap), 0.003, 3)
        _advance(ds, 'SHORT', self._candle(99.8, high=100.2), self._features(99.8, vwap), 0.003, 3)
        # Recapture short: close well below vwap
        _advance(ds, 'SHORT', self._candle(95), self._features(95, vwap), 0.003, 3)
        assert ds.phase == 'SIGNAL_READY'

    def test_used_and_invalidated_are_terminal(self):
        for terminal_phase in ('USED', 'INVALIDATED'):
            ds = _DirectionalState(phase=terminal_phase)
            _advance(ds, 'LONG', self._candle(105), self._features(105, 100.0), 0.003, 3)
            assert ds.phase == terminal_phase  # unchanged


# --- Strategy integration tests ---

class TestVWAPPullbackStrategy:
    def test_long_signal_after_full_state_sequence(self):
        strat = VWAPPullbackStrategy()
        vwap = 100.0
        # 3 bars trend above VWAP (far)
        bars = [(f'09:{30+i:02d}', 105.0, vwap, 103.0, 107.0) for i in range(3)]
        # 1 bar pullback (close near VWAP, dist < 0.003)
        bars += [('09:33', 100.2, vwap, 99.8, 101.0)]
        # 1 bar recapture (close well above VWAP)
        bars += [('09:34', 105.0, vwap, 103.0, 107.0)]

        signals = _feed_bars(strat, bars)
        # Signal should appear on the recapture bar
        assert signals[-1] is not None
        assert signals[-1].direction == 'LONG'

    def test_short_signal_after_full_state_sequence(self):
        strat = VWAPPullbackStrategy()
        vwap = 100.0
        # 3 bars trend below VWAP
        bars = [(f'09:{30+i:02d}', 95.0, vwap, 93.0, 97.0) for i in range(3)]
        # 1 bar pullback (close near VWAP from below)
        bars += [('09:33', 99.8, vwap, 99.5, 100.2)]
        # 1 bar recapture (close well below VWAP)
        bars += [('09:34', 95.0, vwap, 93.0, 97.0)]

        signals = _feed_bars(strat, bars)
        assert signals[-1] is not None
        assert signals[-1].direction == 'SHORT'

    def test_no_signal_below_min_trend_bars(self):
        strat = VWAPPullbackStrategy()
        vwap = 100.0
        # Only 2 bars above VWAP (min is 3)
        bars = [('09:30', 105.0, vwap, 103.0, 107.0), ('09:31', 105.0, vwap, 103.0, 107.0)]
        bars += [('09:32', 100.2, vwap, 99.8, 101.0)]  # pullback
        bars += [('09:33', 105.0, vwap, 103.0, 107.0)]  # would-be recapture
        signals = _feed_bars(strat, bars)
        assert signals[-1] is None

    def test_no_signal_after_cutoff(self):
        strat = VWAPPullbackStrategy()
        vwap = 100.0
        ctx = _make_ctx('13:30', 105.0, vwap)
        # Force the strategy into SIGNAL_READY manually before calling
        ss = strat._get_session_state('NIFTY', date(2024, 1, 2))
        ss.long.phase = 'SIGNAL_READY'
        ss.long.pullback_extreme = 99.0
        assert strat.generate_signal(ctx) is None

    def test_no_signal_when_max_trades_reached(self):
        strat = VWAPPullbackStrategy()
        vwap = 100.0
        ctx = _make_ctx('10:00', 105.0, vwap, per_strategy_count=2)
        ss = strat._get_session_state('NIFTY', date(2024, 1, 2))
        ss.long.phase = 'SIGNAL_READY'
        ss.long.pullback_extreme = 99.0
        assert strat.generate_signal(ctx) is None

    def test_no_signal_when_direction_already_traded(self):
        from src.core.models import Trade
        strat = VWAPPullbackStrategy()
        existing_trade = Trade(
            trade_id='t1', strategy_name='VWAP_PULLBACK', instrument='NIFTY',
            direction='LONG', entry_time=datetime(2024, 1, 2, 9, 35),
            entry_price=105.0, stop_price=99.0, target_price=117.0,
            exit_time=None, exit_price=None, exit_reason=None,
            qty=1, gross_pnl=0, net_pnl=0, r_multiple=None, runtime_mode='backtest',
        )
        ctx = _make_ctx('10:00', 105.0, 100.0, existing_trades=[existing_trade])
        ss = strat._get_session_state('NIFTY', date(2024, 1, 2))
        ss.long.phase = 'SIGNAL_READY'
        ss.long.pullback_extreme = 99.0
        assert strat.generate_signal(ctx) is None

    def test_signal_stop_is_pullback_extreme(self):
        strat = VWAPPullbackStrategy()
        vwap = 100.0
        bars = [(f'09:{30+i:02d}', 105.0, vwap, 103.0, 107.0) for i in range(3)]
        bars += [('09:33', 100.2, vwap, 99.3, 101.0)]  # pullback_extreme = 99.3
        bars += [('09:34', 105.0, vwap, 103.0, 107.0)]
        signals = _feed_bars(strat, bars)
        sig = signals[-1]
        assert sig is not None
        assert abs(sig.stop_price - 99.3) < 0.001

    def test_target_r_in_metadata(self):
        strat = VWAPPullbackStrategy()
        vwap = 100.0
        ss = strat._get_session_state('NIFTY', date(2024, 1, 2))
        ss.long.phase = 'SIGNAL_READY'
        ss.long.pullback_extreme = 99.0
        ctx = _make_ctx('10:00', 105.0, vwap)
        sig = strat.generate_signal(ctx)
        assert sig is not None
        assert sig.metadata.get('target_r') == 2.0

    def test_session_state_resets_for_new_date(self):
        strat = VWAPPullbackStrategy()
        vwap = 100.0
        # Day 1: force into INVALIDATED
        ss1 = strat._get_session_state('NIFTY', date(2024, 1, 2))
        ss1.long.phase = 'INVALIDATED'
        # Day 2: should be fresh IDLE
        ss2 = strat._get_session_state('NIFTY', date(2024, 1, 3))
        assert ss2.long.phase == 'IDLE'

    def test_different_instruments_have_isolated_state(self):
        """NIFTY and BANKNIFTY on the same session_date must NOT share state."""
        strat = VWAPPullbackStrategy()
        ss_nifty = strat._get_session_state('NIFTY', date(2024, 1, 2))
        ss_nifty.long.phase = 'USED'
        ss_bnf = strat._get_session_state('BANKNIFTY', date(2024, 1, 2))
        assert ss_bnf.long.phase == 'IDLE'  # must be independent

    def test_reset_clears_all_session_state(self):
        """reset() must clear accumulated state so the instance is safe to reuse."""
        strat = VWAPPullbackStrategy()
        ss = strat._get_session_state('NIFTY', date(2024, 1, 2))
        ss.long.phase = 'USED'
        ss2 = strat._get_session_state('BANKNIFTY', date(2024, 1, 3))
        ss2.short.phase = 'TREND'
        assert len(strat._session_states) == 2

        strat.reset()

        assert len(strat._session_states) == 0
        # After reset, a new lookup returns fresh IDLE state
        fresh = strat._get_session_state('NIFTY', date(2024, 1, 2))
        assert fresh.long.phase == 'IDLE'
        assert fresh.short.phase == 'IDLE'

    def test_disabled_strategy_returns_no_signal(self):
        strat = VWAPPullbackStrategy()
        ctx = _make_ctx('10:00', 105.0, 100.0, config={'vwap_pullback': {'enabled': False}})
        assert strat.generate_signal(ctx) is None


# --- explain_no_signal tests ---

class TestVWAPExplainNoSignal:
    def test_explains_after_cutoff(self):
        strat = VWAPPullbackStrategy()
        ctx = _make_ctx('13:30', 105.0, 100.0)
        assert strat.explain_no_signal(ctx) == RejectionReason.AFTER_CUTOFF

    def test_explains_max_trades(self):
        strat = VWAPPullbackStrategy()
        ctx = _make_ctx('10:00', 105.0, 100.0, per_strategy_count=2)
        assert strat.explain_no_signal(ctx) == RejectionReason.MAX_TRADES_REACHED

    def test_explains_trend_not_established(self):
        strat = VWAPPullbackStrategy()
        ctx = _make_ctx('10:00', 105.0, 100.0)
        # State is fresh IDLE
        reason = strat.explain_no_signal(ctx)
        assert reason == RejectionReason.TREND_NOT_ESTABLISHED

    def test_explains_no_pullback_when_in_trend(self):
        strat = VWAPPullbackStrategy()
        ss = strat._get_session_state('NIFTY', date(2024, 1, 2))
        ss.long.phase = 'TREND'
        ss.short.phase = 'INVALIDATED'  # short must be non-IDLE so IDLE check doesn't fire first
        ctx = _make_ctx('10:00', 105.0, 100.0)
        assert strat.explain_no_signal(ctx) == RejectionReason.NO_PULLBACK
