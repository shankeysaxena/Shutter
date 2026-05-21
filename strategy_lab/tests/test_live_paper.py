"""
Phase 6 tests — three scenarios that were missing after the review:

  1. Rejected exit order halts the session
  2. SessionHealthMonitor wiring (tick → bar → reset propagation)
  3. Full live-paper entry → stop/target hit → risk state update lifecycle
"""
from datetime import date, datetime, timedelta
from typing import List
from unittest.mock import MagicMock

import pytest

from src.core.models import EngineState, Signal, Trade
from src.execution.executor import OrderRequest, OrderStatus
from src.execution.paper_executor import PaperExecutor
from src.live.bar_builder import LiveBar, Tick
from src.live.risk_engine import RiskEngine, RiskState
from src.live.session_monitor import SessionHealthMonitor
from src.live.trade_manager import LiveTradeManager


# ─── Shared fixtures ──────────────────────────────────────────────────────────

def _make_trade(direction='LONG', instrument='NIFTY', strategy='VWAP_PULLBACK',
                entry_price=22000.0, stop=21800.0, target=22300.0) -> Trade:
    return Trade(
        trade_id='t1', strategy_name=strategy, instrument=instrument,
        direction=direction, entry_time=datetime(2025,3,13,9,30),
        entry_price=entry_price, stop_price=stop, target_price=target,
        exit_time=None, exit_price=None, exit_reason=None,
        qty=25, gross_pnl=0.0, net_pnl=0.0, r_multiple=None,
        runtime_mode='live_paper',
    )


def _make_bar(close, high=None, low=None, instrument='NIFTY',
               ts=None) -> LiveBar:
    ts = ts or datetime(2025,3,13,9,31)
    return LiveBar(
        timestamp=ts, instrument=instrument,
        open=close-2, high=high or close+2, low=low or close-2,
        close=close, volume=1000, tick_count=5,
    )


def _risk_cfg():
    return {
        'risk_engine': {
            'daily_loss_cap': -15000, 'per_strategy_loss_cap': -8000,
            'max_open_positions': 4, 'max_trades_per_session': 20,
            'cooldown_after_losses': 3, 'cooldown_minutes': 30,
        }
    }


def _state():
    return EngineState(
        instrument='NIFTY', session_date=date(2025,3,13),
        open_trades=[], closed_trades=[], queued_signals=[],
        per_strategy_day_trade_count={'VWAP_PULLBACK': 0},
    )


# ─── 1. Rejected exit halts session ──────────────────────────────────────────

class TestRejectedExitHaltsSession:
    def _executor_that_rejects_exit(self):
        """PaperExecutor subclass that rejects exit orders."""
        pe = PaperExecutor()
        original_submit = pe.submit_order

        def mock_submit(req):
            if req.metadata.get('exit_reason') in ('STOP', 'TARGET', 'EOD'):
                # Override fill to REJECTED
                import threading
                with pe._lock:
                    from src.execution.executor import OrderStatus
                    status = OrderStatus(
                        order_id=req.order_id, instrument=req.instrument,
                        direction=req.direction, quantity=req.quantity,
                        status='REJECTED', message='Broker rejected exit',
                        metadata=dict(req.metadata),
                    )
                    pe._orders[req.order_id] = status
                    pe._new_fills.append(status)
            else:
                original_submit(req)
            return req.order_id

        pe.submit_order = mock_submit
        return pe

    def test_rejected_exit_halts_session(self):
        re   = RiskEngine(_risk_cfg())
        rs   = re.reset_session()
        exe  = self._executor_that_rejects_exit()
        tm   = LiveTradeManager(executor=exe, risk_engine=re, lot_sizes={'NIFTY': 25})
        state = _state()

        # Place a LONG trade
        trade = _make_trade()
        state.open_trades.append(trade)
        tm._pending_exits['exit-order-1'] = trade
        state.open_trades.remove(trade)  # simulate it was removed for exit

        # Inject a rejected fill
        rejected_fill = OrderStatus(
            order_id='exit-order-1', instrument='NIFTY', direction='SELL',
            quantity=25, status='REJECTED', message='test rejection',
            metadata={'exit_reason': 'STOP'},
        )
        tm.on_fills([rejected_fill], state, rs)

        # Session must be halted
        assert rs.halted
        assert 'EXIT_ORDER_REJECTED' in rs.halt_reason

        # Trade restored to open_trades as record
        assert len(state.open_trades) == 1
        assert state.open_trades[0].trade_id == 't1'

    def test_cancelled_exit_also_halts(self):
        re = RiskEngine(_risk_cfg())
        rs = re.reset_session()
        pe = PaperExecutor()
        tm = LiveTradeManager(executor=pe, risk_engine=re, lot_sizes={'NIFTY': 25})
        state = _state()

        trade = _make_trade()
        tm._pending_exits['eo2'] = trade

        cancelled_fill = OrderStatus(
            order_id='eo2', instrument='NIFTY', direction='SELL',
            quantity=25, status='CANCELLED', message='cancelled by broker',
            metadata={'exit_reason': 'TARGET'},
        )
        tm.on_fills([cancelled_fill], state, rs)
        assert rs.halted
        assert 'EXIT_ORDER_CANCELLED' in rs.halt_reason


# ─── 2. SessionHealthMonitor wiring ──────────────────────────────────────────

class TestHealthMonitorWiring:
    def test_on_tick_updates_last_tick(self):
        mon = SessionHealthMonitor()
        ts  = datetime(2025, 3, 13, 10, 0, 0)
        mon.on_tick('NIFTY', ts)
        assert mon._last_tick['NIFTY'] == ts

    def test_on_bar_completed_updates_last_bar(self):
        mon = SessionHealthMonitor()
        ts  = datetime(2025, 3, 13, 10, 1)
        mon.on_bar_completed('NIFTY', ts)
        assert mon._last_bar['NIFTY'] == ts

    def test_bar_gap_fires_alert(self):
        alerts = []
        mon = SessionHealthMonitor(
            max_bar_gap_minutes=2,
            alert_callback=lambda t, d: alerts.append((t, d)),
        )
        mon.on_bar_completed('NIFTY', datetime(2025, 3, 13, 10, 0))
        # 5-minute gap — above the 2-minute threshold
        mon.on_bar_completed('NIFTY', datetime(2025, 3, 13, 10, 5))
        assert any(a[0] == 'BAR_GAP' for a in alerts)

    def test_reset_clears_state(self):
        mon = SessionHealthMonitor()
        mon.on_tick('NIFTY', datetime(2025, 3, 13, 10, 0))
        mon.on_bar_completed('NIFTY', datetime(2025, 3, 13, 10, 0))
        mon.reset_session()
        assert 'NIFTY' not in mon._last_tick
        assert 'NIFTY' not in mon._last_bar

    def test_health_summary_no_deadlock(self):
        """Calling health_summary() must not deadlock (RLock fix)."""
        mon = SessionHealthMonitor()
        mon.on_tick('NIFTY', datetime(2025, 3, 13, 10, 0))
        summary = mon.health_summary()   # would deadlock with threading.Lock
        assert 'is_healthy' in summary

    def test_disconnect_sets_disconnected_flag(self):
        """is_healthy() returns True outside market hours regardless. Test the flag directly."""
        mon = SessionHealthMonitor()
        mon.on_tick('NIFTY', datetime(2025, 3, 13, 10, 0))
        assert not mon._disconnected
        mon.on_disconnect()
        assert mon._disconnected
        mon.on_connect()
        assert not mon._disconnected


# ─── 3. Full entry → stop/target → risk lifecycle ────────────────────────────

class TestLivePaperLifecycle:
    def _setup(self):
        re    = RiskEngine(_risk_cfg())
        rs    = re.reset_session()
        pe    = PaperExecutor(slippage_pct=0)
        pe.update_market_price('NIFTY', 22000.0)
        tm    = LiveTradeManager(executor=pe, risk_engine=re, lot_sizes={'NIFTY': 25})
        state = _state()
        return re, rs, pe, tm, state

    def _entry_fill(self, pe, order_id, instrument='NIFTY',
                     direction='BUY', qty=25, price=22000.0) -> OrderStatus:
        return OrderStatus(
            order_id=order_id, instrument=instrument, direction=direction,
            quantity=qty, status='FILLED', filled_qty=qty, fill_price=price,
            fill_time=datetime(2025,3,13,9,31),
        )

    def test_entry_creates_trade(self):
        re, rs, pe, tm, state = self._setup()
        fill = self._entry_fill(pe, 'o1')
        sig  = Signal('VWAP_PULLBACK','NIFTY',datetime(2025,3,13,9,30),
                       'LONG','MARKET',21800,22300)
        tm.on_entry_fill(fill, sig.stop_price, sig.target_price,
                          'VWAP_PULLBACK', state, rs)
        assert len(state.open_trades) == 1
        assert state.open_trades[0].entry_price == 22000.0
        assert rs.open_positions == 1
        assert rs.entries_accepted == 1

    def test_stop_hit_submits_exit(self):
        re, rs, pe, tm, state = self._setup()
        fill = self._entry_fill(pe, 'o1')
        sig  = Signal('VWAP_PULLBACK','NIFTY',datetime(2025,3,13,9,30),
                       'LONG','MARKET',21800,22300)
        tm.on_entry_fill(fill, sig.stop_price, sig.target_price,
                          'VWAP_PULLBACK', state, rs)

        # Bar whose low crosses the stop (21800)
        stop_bar = _make_bar(close=21900, low=21780)
        tm.check_exits(stop_bar, state, rs)

        # Trade moved out of open_trades (pending exit)
        assert len(state.open_trades) == 0
        assert len(tm._pending_exits) == 1

        # Process the exit fill
        exit_fills = pe.get_fills()   # PaperExecutor fills immediately
        assert len(exit_fills) == 1
        tm.on_fills(exit_fills, state, rs)

        # Trade now in closed_trades with STOP reason
        assert len(state.closed_trades) == 1
        t = state.closed_trades[0]
        assert t.exit_reason == 'STOP'
        assert t.net_pnl < 0
        assert rs.open_positions == 0
        assert rs.fills_closed == 1

    def test_target_hit_produces_profit(self):
        re, rs, pe, tm, state = self._setup()
        fill = self._entry_fill(pe, 'o1', price=22000.0)
        tm.on_entry_fill(fill, 21800.0, 22300.0, 'VWAP_PULLBACK', state, rs)

        # Update market price to target level BEFORE check_exits so the
        # PaperExecutor fills the exit order at this price (not entry price)
        pe.update_market_price('NIFTY', 22350.0)
        target_bar = _make_bar(close=22250, high=22350)
        tm.check_exits(target_bar, state, rs)
        exit_fills = pe.get_fills()
        tm.on_fills(exit_fills, state, rs)

        t = state.closed_trades[0]
        assert t.exit_reason == 'TARGET'
        assert t.net_pnl > 0
        assert rs.session_net_pnl > 0

    def test_daily_loss_cap_halts_after_large_loss(self):
        re, rs, pe, tm, state = self._setup()
        re.record_trade_close(rs, 'VWAP_PULLBACK', -20000.0)
        halted, reason = re.should_halt_session(rs)
        assert halted
        assert 'daily_loss_cap' in reason

    # ── Kill switch tests (reviewer required) ──────────────────────────────

    def test_10_consecutive_losses_kills_strategy(self):
        """10 consecutive losses → strategy permanently disabled for session.
        Note: we call record_trade_close directly (bypassing approve_signal)
        so cooldown doesn't block the count — in production a real trade
        already filled before close is recorded."""
        re = RiskEngine({
            'risk_engine': {
                'daily_loss_cap': -999999,
                'per_strategy_loss_cap': -999999,
                'max_open_positions': 4,
                'max_trades_per_session': 100,
                'cooldown_after_losses': 3,
                'cooldown_minutes': 30,
                'strategy_kill_switch': {'consecutive_losses': 10},
            }
        })
        rs = re.reset_session()

        for _ in range(10):
            re.record_trade_close(rs, 'VWAP_PULLBACK', -100.0)

        assert 'VWAP_PULLBACK' in rs.killed_strategies

    def test_killed_strategy_rejected_by_approve_signal(self):
        """After kill, approve_signal returns False with 'strategy_killed' reason."""
        re = RiskEngine({
            'risk_engine': {
                'daily_loss_cap': -999999,
                'per_strategy_loss_cap': -999999,
                'max_open_positions': 4,
                'max_trades_per_session': 100,
                'cooldown_after_losses': 3,
                'cooldown_minutes': 30,
                'strategy_kill_switch': {'consecutive_losses': 10},
            }
        })
        rs = re.reset_session()
        sig = Signal('VWAP_PULLBACK','NIFTY',datetime.now(),'LONG','MARKET',21800,22300)

        for _ in range(10):
            re.record_trade_close(rs, 'VWAP_PULLBACK', -100.0)

        approved, reason = re.approve_signal(sig, rs)
        assert not approved
        assert 'strategy_killed' in reason

    def test_cooldown_at_3_does_not_prevent_kill_at_10(self):
        """
        Cooldowns fire at losses 3, 6, 9 — but kill_streak still accumulates.
        At loss 10 the strategy must be killed.
        """
        re = RiskEngine({
            'risk_engine': {
                'daily_loss_cap': -999999,
                'per_strategy_loss_cap': -999999,
                'max_open_positions': 4,
                'max_trades_per_session': 100,
                'cooldown_after_losses': 3,
                'cooldown_minutes': 1,
                'strategy_kill_switch': {'consecutive_losses': 10},
            }
        })
        rs = re.reset_session()

        for loss_n in range(1, 11):
            re.record_trade_close(rs, 'VWAP_PULLBACK', -100.0)

            if loss_n in (3, 6, 9):
                # Cooldown should have fired; cooldown_streak reset but kill_streak not
                assert rs.cooldown_streaks.get('VWAP_PULLBACK', 0) == 0, \
                    f"cooldown_streak should reset at loss {loss_n}"
                assert rs.kill_streaks.get('VWAP_PULLBACK', 0) == loss_n, \
                    f"kill_streak should be {loss_n} at loss {loss_n}"

        # After 10th loss, strategy is killed
        assert 'VWAP_PULLBACK' in rs.killed_strategies, \
            "Strategy must be killed after 10 consecutive losses despite cooldowns"

    def test_exit_metadata_carries_detection_bar(self):
        re, rs, pe, tm, state = self._setup()
        fill = self._entry_fill(pe, 'o1', price=22000.0)
        tm.on_entry_fill(fill, 21800.0, 22300.0, 'VWAP_PULLBACK', state, rs)

        stop_bar = _make_bar(close=21900, low=21780,
                              ts=datetime(2025,3,13,9,35))
        tm.check_exits(stop_bar, state, rs)
        pe.update_market_price('NIFTY', 21900.0)
        exit_fills = pe.get_fills()
        tm.on_fills(exit_fills, state, rs)

        t = state.closed_trades[0]
        assert t.metadata.get('exit_detection_bar_ts') is not None
        assert t.metadata.get('exit_expected_price') == 21800.0
        assert t.metadata.get('bar_vs_fill_slippage') is not None
