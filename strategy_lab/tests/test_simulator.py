"""Tests for BacktestSimulator: entry, exit, EOD, R-multiple correctness."""
import pytest
from datetime import datetime, date

from src.core.models import Candle, FeatureSnapshot, BarEvent, Signal, Trade
from src.execution.simulator import BacktestSimulator


def _make_signal(direction, stop_price, target_r=2.0):
    return Signal(
        strategy_name='ORB',
        instrument='NIFTY',
        timestamp=datetime(2024, 1, 1, 9, 30),
        direction=direction,
        entry_type='MARKET',
        stop_price=stop_price,
        target_price=0.0,  # recomputed at fill
        metadata={'target_r': target_r},
    )


def _make_bar_event(open_, high, low, close, ts_str='2024-01-01 09:31'):
    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
    candle = Candle(ts, 'NIFTY', open_, high, low, close, 1000)
    features = FeatureSnapshot(
        session_date=date(2024, 1, 1), minute_index=1,
        prior_close=None, vwap=None, vwap_distance=None,
        above_vwap=False, below_vwap=False,
        or_high=None, or_low=None, or_width=None, or_ready=False,
        gap_pct=None, gap_direction=None,
        session_high_so_far=None, session_low_so_far=None,
    )
    return BarEvent(candle=candle, features=features, is_bar_closed=True, runtime_mode='backtest')


class TestBacktestSimulatorEntry:
    def test_entry_at_bar_open_no_slippage(self):
        sim = BacktestSimulator(slippage_per_side=0.0)
        signal = _make_signal('LONG', stop_price=90.0)
        bar = _make_bar_event(open_=101.0, high=110, low=99, close=108)
        trade = sim.process_signal_for_entry(signal, bar, qty=1)
        assert trade.entry_price == 101.0

    def test_entry_long_adds_slippage(self):
        sim = BacktestSimulator(slippage_per_side=2.0)
        signal = _make_signal('LONG', stop_price=90.0)
        bar = _make_bar_event(open_=100.0, high=110, low=99, close=108)
        trade = sim.process_signal_for_entry(signal, bar, qty=1)
        assert trade.entry_price == 102.0

    def test_entry_short_subtracts_slippage(self):
        sim = BacktestSimulator(slippage_per_side=2.0)
        signal = _make_signal('SHORT', stop_price=110.0)
        bar = _make_bar_event(open_=100.0, high=105, low=90, close=92)
        trade = sim.process_signal_for_entry(signal, bar, qty=1)
        assert trade.entry_price == 98.0

    def test_target_computed_from_actual_entry_not_signal_close(self):
        sim = BacktestSimulator(slippage_per_side=0.0)
        signal = _make_signal('LONG', stop_price=90.0, target_r=2.0)
        bar = _make_bar_event(open_=100.0, high=110, low=99, close=108)
        trade = sim.process_signal_for_entry(signal, bar, qty=1)
        # entry=100, stop=90, risk=10, target=100 + 10*2 = 120
        assert trade.target_price == 120.0

    def test_target_computed_from_entry_with_slippage(self):
        sim = BacktestSimulator(slippage_per_side=2.0)
        signal = _make_signal('LONG', stop_price=90.0, target_r=2.0)
        bar = _make_bar_event(open_=100.0, high=120, low=99, close=118)
        trade = sim.process_signal_for_entry(signal, bar, qty=1)
        # risk anchored to raw open (100), not slipped entry:
        # raw_entry=100, stop=90, risk=10, entry=102 (100+2), target=102+10*2=122
        assert trade.entry_price == 102.0
        assert trade.target_price == 122.0

    def test_short_target_computed_correctly(self):
        sim = BacktestSimulator(slippage_per_side=0.0)
        signal = _make_signal('SHORT', stop_price=110.0, target_r=2.0)
        bar = _make_bar_event(open_=100.0, high=105, low=85, close=88)
        trade = sim.process_signal_for_entry(signal, bar, qty=1)
        # entry=100, stop=110, risk=10, target=100-20=80
        assert trade.target_price == 80.0

    def test_higher_slippage_reduces_pnl(self):
        """Slippage must reduce PnL, never increase it (regression guard)."""
        signal_low = _make_signal('LONG', stop_price=90.0, target_r=2.0)
        signal_high = _make_signal('LONG', stop_price=90.0, target_r=2.0)
        entry_bar = _make_bar_event(open_=100.0, high=125, low=99, close=120)

        sim_low = BacktestSimulator(slippage_per_side=1.0, brokerage=0.0)
        sim_high = BacktestSimulator(slippage_per_side=5.0, brokerage=0.0)

        t_low = sim_low.process_signal_for_entry(signal_low, entry_bar, qty=1)
        t_high = sim_high.process_signal_for_entry(signal_high, entry_bar, qty=1)

        exit_bar_low = _make_bar_event(open_=110, high=t_low.target_price + 5, low=99, close=115)
        exit_bar_high = _make_bar_event(open_=110, high=t_high.target_price + 5, low=99, close=115)
        sim_low.check_exits(t_low, exit_bar_low)
        sim_high.check_exits(t_high, exit_bar_high)

        assert t_low.exit_reason == 'TARGET'
        assert t_high.exit_reason == 'TARGET'
        assert t_low.gross_pnl > t_high.gross_pnl, (
            f"Higher slippage must reduce PnL: low={t_low.gross_pnl}, high={t_high.gross_pnl}"
        )


class TestBacktestSimulatorExits:
    def _open_trade(self, direction, entry_price, stop_price, target_price, qty=1):
        return Trade(
            trade_id='test',
            strategy_name='ORB',
            instrument='NIFTY',
            direction=direction,
            entry_time=datetime(2024, 1, 1, 9, 31),
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            exit_time=None,
            exit_price=None,
            exit_reason=None,
            qty=qty,
            gross_pnl=0.0,
            net_pnl=0.0,
            r_multiple=None,
            runtime_mode='backtest',
        )

    def test_long_target_hit(self):
        sim = BacktestSimulator()
        trade = self._open_trade('LONG', 100, 90, 120)
        bar = _make_bar_event(open_=105, high=122, low=103, close=121)
        result = sim.check_exits(trade, bar)
        assert result is True
        assert trade.exit_reason == 'TARGET'
        assert trade.exit_price == 120.0

    def test_long_stop_hit(self):
        sim = BacktestSimulator()
        trade = self._open_trade('LONG', 100, 90, 120)
        bar = _make_bar_event(open_=105, high=108, low=88, close=89)
        result = sim.check_exits(trade, bar)
        assert result is True
        assert trade.exit_reason == 'STOP'
        assert trade.exit_price == 90.0

    def test_long_both_hit_stop_first_conservative(self):
        sim = BacktestSimulator()
        trade = self._open_trade('LONG', 100, 90, 120)
        bar = _make_bar_event(open_=95, high=125, low=85, close=115)
        result = sim.check_exits(trade, bar)
        assert result is True
        assert trade.exit_reason == 'STOP'  # conservative

    def test_short_target_hit(self):
        sim = BacktestSimulator()
        trade = self._open_trade('SHORT', 100, 110, 80)
        bar = _make_bar_event(open_=95, high=98, low=78, close=79)
        result = sim.check_exits(trade, bar)
        assert result is True
        assert trade.exit_reason == 'TARGET'

    def test_short_stop_hit(self):
        sim = BacktestSimulator()
        trade = self._open_trade('SHORT', 100, 110, 80)
        bar = _make_bar_event(open_=105, high=112, low=103, close=111)
        result = sim.check_exits(trade, bar)
        assert result is True
        assert trade.exit_reason == 'STOP'

    def test_no_exit_when_neither_hit(self):
        sim = BacktestSimulator()
        trade = self._open_trade('LONG', 100, 90, 120)
        bar = _make_bar_event(open_=101, high=115, low=95, close=112)
        result = sim.check_exits(trade, bar)
        assert result is False
        assert trade.exit_time is None

    def test_eod_exit_at_close(self):
        sim = BacktestSimulator()
        trade = self._open_trade('LONG', 100, 90, 120)
        bar = _make_bar_event(open_=105, high=110, low=103, close=107, ts_str='2024-01-01 15:29')
        result = sim.check_exits(trade, bar, is_eod=True)
        assert result is True
        assert trade.exit_reason == 'EOD'

    def test_r_multiple_computed_correctly(self):
        sim = BacktestSimulator()
        trade = self._open_trade('LONG', 100, 90, 120)  # risk=10, target=120
        bar = _make_bar_event(open_=100, high=125, low=99, close=122)
        sim.check_exits(trade, bar)
        assert trade.exit_reason == 'TARGET'
        # gross_pnl = (120 - 100) * 1 = 20, risk = 10, r_multiple = 2.0
        assert abs(trade.r_multiple - 2.0) < 0.001

    def test_brokerage_deducted(self):
        sim = BacktestSimulator(brokerage=20.0)
        trade = self._open_trade('LONG', 100, 90, 120)
        bar = _make_bar_event(open_=100, high=125, low=99, close=122)
        sim.check_exits(trade, bar)
        assert trade.net_pnl == trade.gross_pnl - 20.0
