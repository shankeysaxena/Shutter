"""Tests for multi-leg analytics: summary, leg-level ledger, exit-reason breakdown."""
from datetime import date, datetime

from src.analytics.multi_leg_metrics import (
    compute_multi_leg_summary,
    multi_leg_leg_ledger,
    exit_reason_breakdown,
)
from src.core.option_models import LegFill, MultiLegTrade, OptionLeg


def _trade(net_pnl, exit_reason='PROFIT_TARGET', net_credit=1000.0, max_loss=500.0, trade_id='t'):
    exp = date(2024, 1, 4)
    legs = [
        OptionLeg('NIFTY', exp, 22000, 'CE', 'SELL', qty=1),
        OptionLeg('NIFTY', exp, 22000, 'PE', 'SELL', qty=1),
        OptionLeg('NIFTY', exp, 22100, 'CE', 'BUY', qty=1),
        OptionLeg('NIFTY', exp, 21900, 'PE', 'BUY', qty=1),
    ]
    ts = datetime(2024, 1, 2, 10, 0)
    return MultiLegTrade(
        trade_id=trade_id,
        strategy_name='IRON_FLY',
        instrument='NIFTY',
        structure_type='IRON_FLY',
        entry_time=ts,
        entry_fills=[LegFill(leg=l, fill_price=100.0, fill_time=ts) for l in legs],
        net_entry_credit=net_credit,
        exit_time=datetime(2024, 1, 2, 14, 0),
        exit_fills=[LegFill(leg=l, fill_price=80.0, fill_time=ts) for l in legs],
        net_exit_debit=net_credit - net_pnl,
        exit_reason=exit_reason,
        gross_pnl=net_pnl + 160,    # 8 leg-sides × ₹20 brokerage
        net_pnl=net_pnl,
        metadata={'max_loss_per_lot_rupees': max_loss},
        lot_size=25,
    )


class TestComputeMultiLegSummary:
    def test_empty_trades(self):
        assert compute_multi_leg_summary([]) == {'total_trades': 0}

    def test_single_winner(self):
        s = compute_multi_leg_summary([_trade(net_pnl=300)])
        assert s['total_trades'] == 1
        assert s['win_rate'] == 1.0
        assert s['avg_win'] == 300
        assert s['avg_loss'] == 0.0
        assert s['total_net_pnl'] == 300

    def test_mixed_wins_and_losses(self):
        trades = [
            _trade(net_pnl=500, trade_id='w1'),
            _trade(net_pnl=300, trade_id='w2'),
            _trade(net_pnl=-200, trade_id='l1'),
        ]
        s = compute_multi_leg_summary(trades)
        assert s['total_trades'] == 3
        assert s['win_rate'] == round(2/3, 4)
        assert s['avg_win'] == 400
        assert s['avg_loss'] == -200
        assert s['total_net_pnl'] == 600

    def test_exits_by_reason_grouping(self):
        trades = [
            _trade(net_pnl=200, exit_reason='PROFIT_TARGET', trade_id='a'),
            _trade(net_pnl=-100, exit_reason='TOUCH_EXIT_CALL', trade_id='b'),
            _trade(net_pnl=-150, exit_reason='TOUCH_EXIT_CALL', trade_id='c'),
        ]
        s = compute_multi_leg_summary(trades)
        assert s['exits_by_reason']['PROFIT_TARGET'] == {'n': 1, 'total_pnl': 200}
        assert s['exits_by_reason']['TOUCH_EXIT_CALL'] == {'n': 2, 'total_pnl': -250}

    def test_drawdown_is_chronological_min(self):
        # Two consecutive losses produce drawdown of -300
        trades = [
            _trade(net_pnl=-200, trade_id='1'),
            _trade(net_pnl=-100, trade_id='2'),
            _trade(net_pnl=500, trade_id='3'),
        ]
        s = compute_multi_leg_summary(trades)
        assert s['max_drawdown'] == -300


class TestMultiLegLegLedger:
    def test_returns_eight_rows_per_completed_trade(self):
        df = multi_leg_leg_ledger([_trade(net_pnl=100)])
        # 4 entry legs + 4 exit legs = 8 rows
        assert len(df) == 8
        assert set(df['side_event'].unique()) == {'ENTRY', 'EXIT'}

    def test_columns_present(self):
        df = multi_leg_leg_ledger([_trade(net_pnl=100)])
        expected = {
            'trade_id', 'strategy', 'instrument', 'structure', 'side_event',
            'leg_side', 'option_type', 'strike', 'expiry', 'qty', 'lot_size',
            'fill_price', 'fill_time',
        }
        assert expected.issubset(set(df.columns))

    def test_empty_returns_empty(self):
        df = multi_leg_leg_ledger([])
        assert df.empty


class TestExitReasonBreakdown:
    def test_sorted_by_count(self):
        trades = [
            _trade(net_pnl=-100, exit_reason='TOUCH_EXIT_CALL', trade_id='a'),
            _trade(net_pnl=-50, exit_reason='TOUCH_EXIT_CALL', trade_id='b'),
            _trade(net_pnl=200, exit_reason='PROFIT_TARGET', trade_id='c'),
        ]
        df = exit_reason_breakdown(trades)
        # most-frequent reason first
        assert df.iloc[0]['exit_reason'] == 'TOUCH_EXIT_CALL'
        assert df.iloc[0]['count'] == 2
        assert df.iloc[1]['exit_reason'] == 'PROFIT_TARGET'

    def test_empty_returns_empty_with_columns(self):
        df = exit_reason_breakdown([])
        assert df.empty
        assert list(df.columns) == ['exit_reason', 'count', 'total_pnl', 'avg_pnl']
