"""Tests for IronFlyStrategy: entry filters, state machine, exit layers."""
from datetime import datetime, date, timedelta
from typing import Optional

import pytest

from src.core.enums import RejectionReason
from src.core.models import (
    Candle, FeatureSnapshot, BarEvent, EngineState, StrategyContext
)
from src.core.option_models import MultiLegSignal, MultiLegTrade
from src.features.iv_regime import IVRegimeFeature
from src.feeds.option_chain_snapshot import SyntheticOptionChainFeed
from src.strategies.iron_fly import IronFlyStrategy, PHASE_OPEN, PHASE_DONE


# --------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------

DEFAULT_CFG = {
    'iron_fly': {
        'enabled': True,
        'underlyings': ['NIFTY'],
        'allowed_dte': [0, 1, 2],
        'event_day_blacklist_0dte': True,
        'entry_window_start': '09:45',
        'entry_window_end': '13:30',
        'trend_filter': {'max_vwap_distance_pct': 0.0025},
        'range_filter': {'or_width_lookback_days': 20, 'max_or_width_vs_median': 1.0},
        'iv_regime_filter': {'lookback_days': 60, 'min_percentile': 0.25, 'max_percentile': 0.75},
        'liquidity_filter': {'max_atm_spread_pct': 0.05, 'require_two_sided_wings': True},
        'wing_width_pct_of_spot': 0.005,
        'strike_interval': {'NIFTY': 50},
        'risk_per_trade_pct': 0.005,
        'capital': 1000000,
        'max_lots_per_trade': 10,
        'lot_size': {'NIFTY': 25},
        'exits': {
            'touch_exit': {'enabled': True},
            'no_progress': {
                'enabled': True,
                'checkpoints': [
                    {'offset_minutes': 45, 'min_profit_pct_of_max': 0.10},
                    {'offset_minutes': 90, 'min_profit_pct_of_max': 0.25},
                ],
            },
            'profit_target': {'enabled': True, 'pct_of_max_profit': 0.40},
            'vol_expansion': {
                'enabled': True,
                'premium_multiple_threshold': 1.3,
                'max_spot_move_pct': 0.005,
            },
            'hard_time_stop': '15:15',
        },
    },
    'risk': {'lot_size': {'NIFTY': 25}},
}


def _warm_strategy(strategy: IronFlyStrategy, instrument='NIFTY'):
    """Seed enough OR-width history and IV observations to pass warmup gates."""
    base = date(2024, 1, 1)
    # OR width history — 10 days of width=50
    for i in range(10):
        strategy._or_width_history.setdefault(instrument, []).append((base + timedelta(days=i), 50.0))
    # IV regime — 50 observations of 0.15 (puts the query at percentile 0)
    base_ts = datetime(2024, 1, 1, 9, 30)
    for i in range(50):
        strategy._iv_regime.update(instrument, base_ts + timedelta(minutes=i), 0.15)


def _make_ctx(
    bar_close=22000.0,
    bar_time_str='10:00',
    bar_date=date(2024, 1, 17),
    vwap=22000.0,
    or_width=40.0,
    or_ready=True,
    chain_atm_iv=0.15,
    chain_spot_override: Optional[float] = None,
    cfg=None,
    queued_signals=None,
    open_ml=None,
):
    ts = datetime.combine(bar_date, datetime.strptime(bar_time_str, '%H:%M').time())
    candle = Candle(ts, 'NIFTY', bar_close - 1, bar_close + 1, bar_close - 2, bar_close, 1000)
    features = FeatureSnapshot(
        session_date=bar_date,
        minute_index=45,
        prior_close=bar_close - 5,
        vwap=vwap,
        vwap_distance=(bar_close - vwap) / vwap if vwap else 0.0,
        above_vwap=bar_close > vwap if vwap else False,
        below_vwap=bar_close < vwap if vwap else False,
        or_high=bar_close + or_width / 2,
        or_low=bar_close - or_width / 2,
        or_width=or_width,
        or_ready=or_ready,
        gap_pct=0.0,
        gap_direction=None,
        session_high_so_far=bar_close + 5,
        session_low_so_far=bar_close - 5,
    )
    bar_event = BarEvent(candle=candle, features=features, is_bar_closed=True, runtime_mode='backtest')

    feed = SyntheticOptionChainFeed(atm_iv=chain_atm_iv)
    spot = chain_spot_override if chain_spot_override is not None else bar_close
    chain = feed.snapshot_at(ts, 'NIFTY', spot)

    state = EngineState(
        instrument='NIFTY',
        session_date=bar_date,
        open_trades=[],
        closed_trades=[],
        queued_signals=queued_signals or [],
        per_strategy_day_trade_count={'IRON_FLY': 0},
        open_multi_leg_trades=open_ml or [],
    )
    return StrategyContext(
        bar_event=bar_event,
        engine_state=state,
        strategy_config=cfg or DEFAULT_CFG,
        chain_snapshot=chain,
    )


# --------------------------------------------------------------------
# Entry tests
# --------------------------------------------------------------------

class TestIronFlyEntry:
    def _strategy(self):
        return IronFlyStrategy(iv_regime=IVRegimeFeature(min_observations=10), or_history_min_days=3)

    def test_happy_path_emits_signal(self):
        s = self._strategy()
        _warm_strategy(s)
        # Seed IVs uniformly over [0.10, 0.20] so 0.15 sits at percentile ~0.5
        s._iv_regime.reset()
        for i in range(40):
            iv = 0.10 + 0.10 * (i / 40)
            s._iv_regime.update('NIFTY', datetime(2024, 1, 1, 9, 30) + timedelta(minutes=i), iv)
        ctx = _make_ctx(chain_atm_iv=0.15)
        sig = s.generate_signal(ctx)
        assert sig is not None
        assert isinstance(sig, MultiLegSignal)
        assert len(sig.legs) == 4
        assert sig.metadata['max_loss_per_lot_rupees'] > 0
        assert sig.metadata['atm_strike'] == 22000
        # 22000 * 0.005 = 110 -> round(110/50) * 50 = 2 * 50 = 100
        assert sig.metadata['wing_width'] == 100

    def test_disabled_returns_none(self):
        s = self._strategy()
        cfg = {**DEFAULT_CFG, 'iron_fly': {**DEFAULT_CFG['iron_fly'], 'enabled': False}}
        ctx = _make_ctx(cfg=cfg)
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.DISABLED

    def test_outside_entry_window_too_early(self):
        s = self._strategy()
        _warm_strategy(s)
        ctx = _make_ctx(bar_time_str='09:30')
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.OUTSIDE_ENTRY_WINDOW

    def test_outside_entry_window_too_late(self):
        s = self._strategy()
        _warm_strategy(s)
        ctx = _make_ctx(bar_time_str='14:00')
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.OUTSIDE_ENTRY_WINDOW

    def test_chain_unavailable(self):
        s = self._strategy()
        _warm_strategy(s)
        ctx = _make_ctx()
        ctx.chain_snapshot = None
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.CHAIN_NOT_AVAILABLE

    def test_trend_too_strong(self):
        s = self._strategy()
        _warm_strategy(s)
        # bar far from vwap: distance > 0.25%
        ctx = _make_ctx(bar_close=22500, vwap=22000)
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.TREND_TOO_STRONG

    def test_or_width_too_wide(self):
        s = self._strategy()
        _warm_strategy(s)
        # or_width=200 vs median 50 -> too wide
        ctx = _make_ctx(or_width=200.0)
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.OR_WIDTH_TOO_WIDE

    def test_or_history_not_ready(self):
        s = self._strategy()
        # Don't warm OR width history
        for i in range(20):
            s._iv_regime.update('NIFTY', datetime(2024, 1, 1, 9, 30) + timedelta(minutes=i),
                                0.10 + 0.01 * (i / 20))
        ctx = _make_ctx()
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.OR_HISTORY_NOT_READY

    def test_iv_regime_not_warm(self):
        s = self._strategy()
        # Warm OR history only
        base = date(2024, 1, 1)
        for i in range(10):
            s._or_width_history.setdefault('NIFTY', []).append((base + timedelta(days=i), 50.0))
        ctx = _make_ctx()
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.IV_REGIME_NOT_WARM

    def test_iv_regime_out_of_band(self):
        s = self._strategy()
        _warm_strategy(s)
        # All buffered IVs are 0.15; query 0.30 -> percentile = 1.0 -> above max 0.75
        ctx = _make_ctx(chain_atm_iv=0.30)
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.IV_REGIME_OUT_OF_BAND

    def test_structure_already_open(self):
        s = self._strategy()
        _warm_strategy(s)
        # Mark state as ENTERING
        state = s._state_for('NIFTY', date(2024, 1, 17))
        state.phase = PHASE_OPEN
        ctx = _make_ctx()
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.STRUCTURE_ALREADY_OPEN

    def test_day_done(self):
        s = self._strategy()
        _warm_strategy(s)
        state = s._state_for('NIFTY', date(2024, 1, 17))
        state.phase = PHASE_DONE
        ctx = _make_ctx()
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.DAY_DONE


# --------------------------------------------------------------------
# State machine / callbacks
# --------------------------------------------------------------------

class TestStateMachine:
    def test_on_filled_transitions_to_open(self):
        s = IronFlyStrategy()
        bar_date = date(2024, 1, 17)
        st = s._state_for('NIFTY', bar_date)
        from src.strategies.iron_fly import PHASE_ENTERING
        st.phase = PHASE_ENTERING

        # Build a minimal trade
        trade = MultiLegTrade(
            trade_id='t1',
            strategy_name='IRON_FLY',
            instrument='NIFTY',
            structure_type='IRON_FLY',
            entry_time=datetime(2024, 1, 17, 10, 0),
            entry_fills=[],
            net_entry_credit=1000.0,
        )
        s.on_multi_leg_filled(trade)
        assert s._state_for('NIFTY', bar_date).phase == PHASE_OPEN
        assert s._state_for('NIFTY', bar_date).entry_max_profit_rupees == 1000.0
        assert s._state_for('NIFTY', bar_date).open_trade_id == 't1'

    def test_on_closed_transitions_to_done(self):
        s = IronFlyStrategy()
        bar_date = date(2024, 1, 17)
        st = s._state_for('NIFTY', bar_date)
        st.phase = PHASE_OPEN
        st.open_trade_id = 't1'

        trade = MultiLegTrade(
            trade_id='t1', strategy_name='IRON_FLY', instrument='NIFTY',
            structure_type='IRON_FLY',
            entry_time=datetime(2024, 1, 17, 10, 0),
            entry_fills=[], net_entry_credit=1000.0,
        )
        s.on_multi_leg_closed(trade)
        assert s._state_for('NIFTY', bar_date).phase == PHASE_DONE

    def test_reset_clears_everything(self):
        s = IronFlyStrategy()
        _warm_strategy(s)
        s._state_for('NIFTY', date(2024, 1, 17)).phase = PHASE_OPEN
        s.reset()
        assert len(s._session_states) == 0
        assert len(s._or_width_history) == 0
        assert s._iv_regime.buffer_size('NIFTY') == 0


# --------------------------------------------------------------------
# Exit tests
# --------------------------------------------------------------------

class TestIronFlyExits:
    def _open_trade_setup(self):
        """Returns (strategy, open_trade, helper to build exit ctx)."""
        s = IronFlyStrategy(iv_regime=IVRegimeFeature(min_observations=10), or_history_min_days=3)
        bar_date = date(2024, 1, 17)
        st = s._state_for('NIFTY', bar_date)
        st.phase = PHASE_OPEN
        st.entry_time = datetime(2024, 1, 17, 10, 0)
        st.entry_spot = 22000
        st.short_call_strike = 22000
        st.short_put_strike = 22000
        # Touch boundaries default to wings; fixture wing width = 150 -> ±150
        st.touch_upper = 22150
        st.touch_lower = 21850
        st.open_trade_id = 't1'

        # Build a trade whose mark-to-market the exit logic will probe
        feed = SyntheticOptionChainFeed(atm_iv=0.15)
        ts = datetime(2024, 1, 17, 10, 0)
        snap = feed.snapshot_at(ts, 'NIFTY', 22000)
        from src.execution.multi_leg_simulator import MultiLegSimulator, MODE_IDEAL
        from src.core.option_models import OptionLeg, MultiLegSignal
        sig = MultiLegSignal('IRON_FLY', 'NIFTY', ts, 'IRON_FLY', [
            OptionLeg('NIFTY', snap.expiry, 22000, 'CE', 'SELL', qty=1),
            OptionLeg('NIFTY', snap.expiry, 22000, 'PE', 'SELL', qty=1),
            OptionLeg('NIFTY', snap.expiry, 22150, 'CE', 'BUY', qty=1),
            OptionLeg('NIFTY', snap.expiry, 21850, 'PE', 'BUY', qty=1),
        ])
        sim = MultiLegSimulator(mode=MODE_IDEAL, brokerage_per_leg=0)
        trade = sim.open_trade(sig, snap, lots=1, lot_size=25)
        trade.trade_id = 't1'

        # Anchor state to the *actual* entry premium and net credit so ratios
        # computed against later bars are meaningful (else vol_expansion would
        # fire spuriously on any tested guard).
        sc_fill = next(f for f in trade.entry_fills if f.leg.side == 'SELL' and f.leg.option_type == 'CE')
        sp_fill = next(f for f in trade.entry_fills if f.leg.side == 'SELL' and f.leg.option_type == 'PE')
        st.entry_short_premium_per_unit = sc_fill.fill_price + sp_fill.fill_price
        st.entry_max_profit_rupees = trade.net_entry_credit
        return s, trade, feed, snap.expiry

    def _ctx_with_open_trade(self, s, trade, bar_time_str, spot, chain_atm_iv=0.15):
        ts = datetime.combine(date(2024, 1, 17), datetime.strptime(bar_time_str, '%H:%M').time())
        feed = SyntheticOptionChainFeed(atm_iv=chain_atm_iv)
        snap = feed.snapshot_at(ts, 'NIFTY', spot)
        candle = Candle(ts, 'NIFTY', spot - 1, spot + 1, spot - 2, spot, 1000)
        features = FeatureSnapshot(
            session_date=date(2024, 1, 17), minute_index=60, prior_close=22000,
            vwap=22000, vwap_distance=0, above_vwap=False, below_vwap=False,
            or_high=22050, or_low=21950, or_width=100, or_ready=True,
            gap_pct=0, gap_direction=None,
            session_high_so_far=22050, session_low_so_far=21950,
        )
        bar_event = BarEvent(candle, features, True, 'backtest')
        state = EngineState(
            'NIFTY', date(2024, 1, 17), [], [], [],
            {'IRON_FLY': 0},
            open_multi_leg_trades=[trade],
        )
        return StrategyContext(bar_event, state, DEFAULT_CFG, chain_snapshot=snap)

    def test_touch_exit_call(self):
        s, trade, _, _ = self._open_trade_setup()
        # Default touch_upper = 22150; move spot to that boundary
        ctx = self._ctx_with_open_trade(s, trade, '10:30', spot=22150)
        exits = s.evaluate_multi_leg_exits(ctx)
        assert len(exits) == 1
        assert exits[0][1] == 'TOUCH_EXIT_CALL'

    def test_touch_exit_put(self):
        s, trade, _, _ = self._open_trade_setup()
        # Default touch_lower = 21850; move spot below
        ctx = self._ctx_with_open_trade(s, trade, '10:30', spot=21840)
        exits = s.evaluate_multi_leg_exits(ctx)
        assert len(exits) == 1
        assert exits[0][1] == 'TOUCH_EXIT_PUT'

    def test_profit_target_fires_when_decayed(self):
        s, trade, _, _ = self._open_trade_setup()
        st = s._state_for('NIFTY', date(2024, 1, 17))
        # Pre-fire no-progress checkpoints so only profit_target / time-stop remain
        st.no_progress_fired.update({45, 90})
        # Shrink entry_max_profit so any modest decay crosses 40%
        st.entry_max_profit_rupees = 10.0
        ctx = self._ctx_with_open_trade(s, trade, '14:00', spot=22000)  # 4h later, theta has worked
        exits = s.evaluate_multi_leg_exits(ctx)
        assert len(exits) == 1
        assert exits[0][1] == 'PROFIT_TARGET'

    def test_hard_time_stop(self):
        s, trade, _, _ = self._open_trade_setup()
        st = s._state_for('NIFTY', date(2024, 1, 17))
        st.entry_max_profit_rupees = 10_000_000  # so profit target won't fire
        # Pre-fire no-progress checkpoints so they don't claim the exit slot
        st.no_progress_fired.update({45, 90})
        ctx = self._ctx_with_open_trade(s, trade, '15:15', spot=22000)  # well inside touch boundaries
        exits = s.evaluate_multi_leg_exits(ctx)
        assert len(exits) == 1
        assert exits[0][1] == 'HARD_TIME_STOP'

    def test_no_progress_t45_fires_when_flat(self):
        s, trade, _, _ = self._open_trade_setup()
        st = s._state_for('NIFTY', date(2024, 1, 17))
        st.entry_max_profit_rupees = 10_000_000  # huge -> pct never reaches checkpoint
        ctx = self._ctx_with_open_trade(s, trade, '10:45', spot=22000)  # 45 min after entry, inside boundaries
        exits = s.evaluate_multi_leg_exits(ctx)
        assert len(exits) == 1
        assert exits[0][1] == 'NO_PROGRESS_T+45'

    def test_touch_exit_wins_over_other_exits(self):
        """If spot is at touch boundary AND time has elapsed, touch wins."""
        s, trade, _, _ = self._open_trade_setup()
        st = s._state_for('NIFTY', date(2024, 1, 17))
        st.entry_max_profit_rupees = 10_000_000
        # Spot at touch_upper triggers touch before any time-based exit
        ctx = self._ctx_with_open_trade(s, trade, '15:15', spot=22150)
        exits = s.evaluate_multi_leg_exits(ctx)
        assert len(exits) == 1
        assert 'TOUCH' in exits[0][1]
