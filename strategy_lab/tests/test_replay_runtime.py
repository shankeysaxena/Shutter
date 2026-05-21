"""
Tests for ReplayRuntime and backtest/replay consistency.

Key invariant: the same historical data + config fed through BacktestRuntime
and ReplayRuntime must produce identical trade counts, identical fills, and
identical exit outcomes. Any divergence indicates a bug in one of the runtimes.
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, date, time
from typing import List

from src.data.sessionizer import Sessionizer
from src.features.vwap import VWAPFeature
from src.features.opening_range import OpeningRangeFeature
from src.features.gap import GapFeature
from src.strategies.orb import ORBStrategy
from src.execution.simulator import BacktestSimulator
from src.runtimes.backtest import BacktestRuntime
from src.runtimes.replay import ReplayRuntime
from src.feeds.replay_feed import ReplayFeed
from src.analytics.metrics import MetricsEngine
from src.analytics.comparison import compare_runs


# --- Synthetic session builder ---

def _make_session_df(session_date_str: str, open_price: float = 21000.0, trend: str = 'up') -> pd.DataFrame:
    """
    Builds a realistic synthetic 1-min session for testing.
    OR window (09:15-09:30) is choppy; after 09:30 a clean breakout occurs.
    """
    np.random.seed(42)
    session_date = pd.to_datetime(session_date_str)
    timestamps = [session_date + pd.Timedelta(hours=9, minutes=15 + i) for i in range(375)]

    price = open_price
    rows = []
    for i, ts in enumerate(timestamps):
        if i < 15:   # OR window — choppy
            change = np.random.uniform(-3, 3)
        elif i < 45:  # breakout window
            change = np.random.uniform(5, 15) if trend == 'up' else np.random.uniform(-15, -5)
        else:
            change = np.random.uniform(-2, 2)

        o = price
        c = price + change
        h = max(o, c) + abs(np.random.normal(0, 1))
        lo = min(o, c) - abs(np.random.normal(0, 1))
        rows.append({'timestamp': ts, 'open': o, 'high': h, 'low': lo, 'close': c, 'volume': 1000})
        price = c

    return pd.DataFrame(rows)


def _apply_features(session_df: pd.DataFrame) -> pd.DataFrame:
    session_df = VWAPFeature().calculate(session_df)
    session_df = OpeningRangeFeature(start_time=time(9, 15), end_time=time(9, 30)).calculate(session_df)
    session_df = GapFeature().calculate(session_df)
    return session_df


_CONFIG = {
    'risk': {'lot_size': {'NIFTY': 1}, 'max_total_trades_per_day': 4},
    'costs': {'slippage_per_side': 2.0, 'brokerage_per_trade': 20.0},
    'strategies': {'orb': {'enabled': True, 'no_entry_after': '12:00', 'target_r': 2.0}},
}


def _build_sessions(session_date_str: str, trend: str = 'up'):
    raw = _make_session_df(session_date_str, trend=trend)
    sessionizer = Sessionizer(filter_market_hours=True)
    sessions = sessionizer.create_sessions(raw)
    return {dt: _apply_features(df) for dt, df in sessions.items()}


def _run_backtest(sessions, instrument='NIFTY'):
    strategies = [ORBStrategy()]
    simulator = BacktestSimulator(slippage_per_side=2.0, brokerage=20.0)
    runtime = BacktestRuntime(strategies=strategies, simulator=simulator, config=_CONFIG)
    all_trades = []
    for session_date, session_df in sessions.items():
        all_trades.extend(runtime.run_session(instrument, session_date, session_df))
    return all_trades


def _run_replay(sessions, instrument='NIFTY'):
    strategies = [ORBStrategy()]
    simulator = BacktestSimulator(slippage_per_side=2.0, brokerage=20.0)
    runtime = ReplayRuntime(strategies=strategies, simulator=simulator, config=_CONFIG)
    feed = ReplayFeed(sessions=sessions, instrument=instrument)
    trades, event_log = runtime.run(feed)
    return trades, event_log


# --- Consistency tests ---

class TestReplayBacktestConsistency:
    def test_same_trade_count_uptrend(self):
        sessions = _build_sessions('2024-01-02', trend='up')
        bt_trades = _run_backtest(sessions)
        rp_trades, _ = _run_replay(sessions)
        assert len(bt_trades) == len(rp_trades), (
            f"Trade count mismatch: backtest={len(bt_trades)} replay={len(rp_trades)}"
        )

    def test_same_trade_count_downtrend(self):
        sessions = _build_sessions('2024-01-03', trend='down')
        bt_trades = _run_backtest(sessions)
        rp_trades, _ = _run_replay(sessions)
        assert len(bt_trades) == len(rp_trades)

    def test_same_total_pnl(self):
        sessions = _build_sessions('2024-01-02', trend='up')
        bt_trades = _run_backtest(sessions)
        rp_trades, _ = _run_replay(sessions)
        bt_pnl = sum(t.net_pnl for t in bt_trades)
        rp_pnl = sum(t.net_pnl for t in rp_trades)
        assert abs(bt_pnl - rp_pnl) < 0.01, f"PnL mismatch: backtest={bt_pnl} replay={rp_pnl}"

    def test_same_exit_reasons(self):
        sessions = _build_sessions('2024-01-02', trend='up')
        bt_trades = _run_backtest(sessions)
        rp_trades, _ = _run_replay(sessions)
        bt_exits = sorted(t.exit_reason for t in bt_trades)
        rp_exits = sorted(t.exit_reason for t in rp_trades)
        assert bt_exits == rp_exits

    def test_same_entry_prices(self):
        sessions = _build_sessions('2024-01-02', trend='up')
        bt_trades = _run_backtest(sessions)
        rp_trades, _ = _run_replay(sessions)
        for i, (bt, rp) in enumerate(zip(bt_trades, rp_trades)):
            assert abs(bt.entry_price - rp.entry_price) < 0.001, (
                f"Entry price mismatch at trade {i}: backtest={bt.entry_price} replay={rp.entry_price}"
            )

    def test_compare_runs_utility_reports_consistent(self):
        sessions = _build_sessions('2024-01-02', trend='up')
        bt_ledger = MetricsEngine.generate_trade_ledger(_run_backtest(sessions))
        rp_ledger = MetricsEngine.generate_trade_ledger(_run_replay(sessions)[0])
        result = compare_runs(bt_ledger, rp_ledger)
        assert not result['diverged'], f"compare_runs flagged divergence: {result['discrepancies']}"

    def test_no_trades_on_flat_session(self):
        """A flat session with no OR breakout should produce no trades in either runtime."""
        np.random.seed(0)
        session_date = pd.to_datetime('2024-01-04')
        timestamps = [session_date + pd.Timedelta(hours=9, minutes=15 + i) for i in range(375)]
        # Flat price — will never break OR
        rows = [{'timestamp': ts, 'open': 21000, 'high': 21001, 'low': 20999, 'close': 21000, 'volume': 500}
                for ts in timestamps]
        raw = pd.DataFrame(rows)
        sessionizer = Sessionizer(filter_market_hours=True)
        sessions = sessionizer.create_sessions(raw)
        sessions = {dt: _apply_features(df) for dt, df in sessions.items()}

        bt_trades = _run_backtest(sessions)
        rp_trades, _ = _run_replay(sessions)
        assert len(bt_trades) == 0
        assert len(rp_trades) == 0


# --- Event log tests ---

class TestReplayEventLog:
    def test_event_log_is_non_empty_when_trades_exist(self):
        sessions = _build_sessions('2024-01-02', trend='up')
        trades, event_log = _run_replay(sessions)
        if trades:
            assert len(event_log) > 0

    def test_every_entry_filled_has_matching_exit(self):
        sessions = _build_sessions('2024-01-02', trend='up')
        _, event_log = _run_replay(sessions)
        fills = [e for e in event_log if e['event_type'] == 'entry_filled']
        exits = [e for e in event_log if e['event_type'].startswith('exit_')]
        assert len(fills) == len(exits), (
            f"fills={len(fills)} but exits={len(exits)}"
        )

    def test_event_log_has_required_fields(self):
        sessions = _build_sessions('2024-01-02', trend='up')
        _, event_log = _run_replay(sessions)
        for entry in event_log:
            assert 'event_type' in entry
            assert 'timestamp' in entry
            assert 'instrument' in entry

    def test_signal_queued_events_appear_before_entry_filled(self):
        sessions = _build_sessions('2024-01-02', trend='up')
        _, event_log = _run_replay(sessions)
        event_types = [e['event_type'] for e in event_log]
        # For any entry_filled, there should be a signal_queued somewhere before it
        if 'entry_filled' in event_types:
            first_fill = event_types.index('entry_filled')
            assert 'signal_queued' in event_types[:first_fill]

    def test_eod_exit_logged_when_trade_not_closed(self):
        """
        Build a session where price never moves enough to hit stop or target,
        forcing an EOD exit, and verify the log captures it.
        """
        # Use a very flat session after the OR breakout:
        # OR window flat, then tiny breakout, then price doesn't reach target or stop
        np.random.seed(10)
        session_date = pd.to_datetime('2024-01-05')
        timestamps = [session_date + pd.Timedelta(hours=9, minutes=15 + i) for i in range(375)]
        price = 21000.0
        rows = []
        for i, ts in enumerate(timestamps):
            if i < 15:
                change = np.random.uniform(-2, 2)
            elif i == 15:
                change = 10.0   # single bar breakout above OR high
            else:
                change = 0.0    # price freezes — never hits target or stop
            o = price
            c = price + change
            h = max(o, c) + 0.5
            lo = min(o, c) - 0.5
            rows.append({'timestamp': ts, 'open': o, 'high': h, 'low': lo, 'close': c, 'volume': 1000})
            price = c

        raw = pd.DataFrame(rows)
        sessionizer = Sessionizer(filter_market_hours=True)
        sessions = sessionizer.create_sessions(raw)
        sessions = {dt: _apply_features(df) for dt, df in sessions.items()}

        _, event_log = _run_replay(sessions)
        eod_events = [e for e in event_log if e['event_type'] == 'exit_eod']
        # If a trade was opened, it should have been force-exited at EOD
        fills = [e for e in event_log if e['event_type'] == 'entry_filled']
        assert len(eod_events) == len(fills)
