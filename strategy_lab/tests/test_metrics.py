"""Tests for MetricsEngine."""
import pytest
from datetime import datetime

from src.core.models import Trade
from src.analytics.metrics import MetricsEngine


def _make_trade(net_pnl, r_multiple=None, direction='LONG'):
    gross = net_pnl + 0  # assume no brokerage in these tests
    return Trade(
        trade_id='test',
        strategy_name='ORB',
        instrument='NIFTY',
        direction=direction,
        entry_time=datetime(2024, 1, 1, 9, 35),
        entry_price=100.0,
        stop_price=90.0,
        target_price=120.0,
        exit_time=datetime(2024, 1, 1, 10, 0),
        exit_price=120.0,
        exit_reason='TARGET',
        qty=1,
        gross_pnl=gross,
        net_pnl=net_pnl,
        r_multiple=r_multiple,
        runtime_mode='backtest',
    )


class TestMetricsEngine:
    def test_empty_trades_returns_empty_summary(self):
        ledger = MetricsEngine.generate_trade_ledger([])
        summary = MetricsEngine.calculate_summary(ledger)
        assert summary == {}

    def test_total_trades_count(self):
        trades = [_make_trade(100), _make_trade(-50), _make_trade(200)]
        ledger = MetricsEngine.generate_trade_ledger(trades)
        summary = MetricsEngine.calculate_summary(ledger)
        assert summary['total_trades'] == 3

    def test_win_rate(self):
        trades = [_make_trade(100), _make_trade(200), _make_trade(-50)]
        ledger = MetricsEngine.generate_trade_ledger(trades)
        summary = MetricsEngine.calculate_summary(ledger)
        assert abs(summary['win_rate'] - 2/3) < 0.001

    def test_avg_win(self):
        trades = [_make_trade(100), _make_trade(200), _make_trade(-50)]
        ledger = MetricsEngine.generate_trade_ledger(trades)
        summary = MetricsEngine.calculate_summary(ledger)
        assert summary['avg_win'] == 150.0

    def test_avg_loss(self):
        trades = [_make_trade(100), _make_trade(-50), _make_trade(-150)]
        ledger = MetricsEngine.generate_trade_ledger(trades)
        summary = MetricsEngine.calculate_summary(ledger)
        assert summary['avg_loss'] == -100.0

    def test_total_net_pnl(self):
        trades = [_make_trade(100), _make_trade(-50), _make_trade(200)]
        ledger = MetricsEngine.generate_trade_ledger(trades)
        summary = MetricsEngine.calculate_summary(ledger)
        assert summary['total_net_pnl'] == 250.0

    def test_profit_factor(self):
        trades = [_make_trade(100), _make_trade(100), _make_trade(-50)]
        ledger = MetricsEngine.generate_trade_ledger(trades)
        summary = MetricsEngine.calculate_summary(ledger)
        assert abs(summary['profit_factor'] - 4.0) < 0.001  # 200 / 50

    def test_profit_factor_no_losses_returns_none(self):
        # When there are no losing trades, profit_factor is None (no-loss run, not infinity)
        trades = [_make_trade(100), _make_trade(200)]
        ledger = MetricsEngine.generate_trade_ledger(trades)
        summary = MetricsEngine.calculate_summary(ledger)
        assert summary['profit_factor'] is None

    def test_max_drawdown(self):
        # cumPnL: 100, 50, 150 → drawdown: 0, -50, 0 → max drawdown = -50
        trades = [_make_trade(100), _make_trade(-50), _make_trade(100)]
        ledger = MetricsEngine.generate_trade_ledger(trades)
        summary = MetricsEngine.calculate_summary(ledger)
        assert summary['max_drawdown'] == -50.0

    def test_max_consecutive_losses(self):
        trades = [
            _make_trade(100),
            _make_trade(-50),
            _make_trade(-50),
            _make_trade(-50),
            _make_trade(200),
        ]
        ledger = MetricsEngine.generate_trade_ledger(trades)
        summary = MetricsEngine.calculate_summary(ledger)
        assert summary['max_consecutive_losses'] == 3

    def test_expectancy_positive(self):
        # win_rate=0.5, avg_win=200, avg_loss=-100 → expectancy = 0.5*200 + 0.5*(-100) = 50
        trades = [_make_trade(200), _make_trade(-100)]
        ledger = MetricsEngine.generate_trade_ledger(trades)
        summary = MetricsEngine.calculate_summary(ledger)
        assert summary['expectancy'] == 50.0

    def test_trade_ledger_has_required_columns(self):
        trades = [_make_trade(100)]
        ledger = MetricsEngine.generate_trade_ledger(trades)
        required = {'trade_id', 'strategy', 'direction', 'entry_time', 'net_pnl', 'r_multiple', 'exit_reason'}
        assert required.issubset(set(ledger.columns))
