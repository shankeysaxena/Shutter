"""
End-to-end smoke test for the Iron Fly strategy on synthetic data.

Generates ~45 sessions of NIFTY OHLCV (mostly range-bound, with a few trend
days mixed in), applies the standard feature pipeline, and runs the
BacktestRuntime wired with SyntheticOptionChainFeed + MultiLegSimulator.

Purpose: validate that the integrated multi-leg pipeline produces non-zero
trades with sane P&L. NOT a strategy validation — synthetic chain prices
follow Black-Scholes exactly, so the strategy is trading against a model
that is, by construction, perfectly consistent.

Run: python3 run_iron_fly_smoke.py
"""
import sys
from datetime import datetime, date, timedelta

import numpy as np
import pandas as pd

from src.data.sessionizer import Sessionizer
from src.features.vwap import VWAPFeature
from src.features.opening_range import OpeningRangeFeature
from src.features.gap import GapFeature
from src.features.intraday import IntradaySessionFeature
from src.execution.simulator import BacktestSimulator
from src.execution.multi_leg_simulator import MultiLegSimulator, MODE_REALISTIC
from src.feeds.option_chain_snapshot import SyntheticOptionChainFeed
from src.features.iv_regime import IVRegimeFeature
from src.strategies.iron_fly import IronFlyStrategy
from src.runtimes.backtest import BacktestRuntime


def generate_synthetic_nifty(n_days: int, seed: int = 42) -> pd.DataFrame:
    """Generate n_days of synthetic NIFTY 1-min OHLCV.

    Day type mix: 70% range, 20% mild trend, 10% strong trend.
    Range days produce a tight opening range and minimal drift from open.
    """
    rng = np.random.default_rng(seed)
    rows = []
    spot = 22000.0
    start = date(2024, 1, 1)

    for day_offset in range(n_days):
        d = start + timedelta(days=day_offset)
        if d.weekday() >= 5:   # skip weekends
            continue

        # Day type
        r = rng.random()
        if r < 0.7:
            day_type = 'range'
            drift_per_min = 0.0
            vol_per_min = 1.5
        elif r < 0.9:
            day_type = 'mild'
            drift_per_min = rng.choice([-0.3, 0.3])
            vol_per_min = 2.0
        else:
            day_type = 'trend'
            drift_per_min = rng.choice([-0.8, 0.8])
            vol_per_min = 2.5

        # Open with small overnight gap from previous close
        spot += rng.normal(0, 10)

        for i in range(375):
            t = datetime.combine(d, datetime.strptime('09:15', '%H:%M').time()) + timedelta(minutes=i)
            change = rng.normal(drift_per_min, vol_per_min)
            o = spot
            c = spot + change
            high = max(o, c) + abs(rng.normal(0, 0.8))
            low = min(o, c) - abs(rng.normal(0, 0.8))
            rows.append({
                'timestamp': t,
                'instrument': 'NIFTY',
                'open': o, 'high': high, 'low': low, 'close': c,
                'volume': 1000,
            })
            spot = c

    return pd.DataFrame(rows)


def main():
    print(">> Generating synthetic NIFTY data (60 days)...")
    df = generate_synthetic_nifty(n_days=90)  # 90 calendar days ~= 60 trading days
    print(f"   Rows: {len(df)}, span: {df['timestamp'].min()} -> {df['timestamp'].max()}")

    sessionizer = Sessionizer()
    sessions = sessionizer.create_sessions(df)
    print(f"   Trading sessions: {len(sessions)}")

    vwap_feat = VWAPFeature()
    or_feat = OpeningRangeFeature()
    gap_feat = GapFeature()
    intraday_feat = IntradaySessionFeature()

    config = {
        'runtime': {'mode': 'backtest'},
        'instruments': ['NIFTY'],
        'costs': {'slippage_per_side': 0, 'brokerage_per_trade': 20},
        'risk': {
            'mode': 'fixed_lot',
            'lot_size': {'NIFTY': 25},
            'max_total_trades_per_day': 4,
        },
        'strategies': {
            'iron_fly': {
                'enabled': True,
                'underlyings': ['NIFTY'],
                'allowed_dte': [0, 1, 2, 3],   # widened a touch for synthetic data without holiday calendar
                'event_day_blacklist_0dte': False,  # no event-day list in smoke test
                'entry_window_start': '09:45',
                'entry_window_end': '13:30',
                'trend_filter': {'max_vwap_distance_pct': 0.0025},
                'range_filter': {'or_width_lookback_days': 10, 'max_or_width_vs_median': 1.5},
                'iv_regime_filter': {'lookback_days': 60, 'min_percentile': 0.20, 'max_percentile': 0.80},
                'liquidity_filter': {'max_atm_spread_pct': 0.05, 'require_two_sided_wings': True},
                'wing_width_pct_of_spot': 0.005,
                'strike_interval': {'NIFTY': 50},
                'risk_per_trade_pct': 0.005,
                'capital': 1_000_000,
                'max_lots_per_trade': 5,
                'lot_size': {'NIFTY': 25},
                'exits': {
                    'touch_exit': {'enabled': True, 'distance_pct_of_wing': 1.0},
                    # NOTE: spec defaults (10% / 25%) are unrealistically aggressive for
                    # intraday iron fly under round-trip spread costs. Tuned down here;
                    # production thresholds need re-derivation on real chain data.
                    'no_progress': {
                        'enabled': True,
                        'checkpoints': [
                            {'offset_minutes': 45, 'min_profit_pct_of_max': 0.01},
                            {'offset_minutes': 90, 'min_profit_pct_of_max': 0.05},
                        ],
                    },
                    'profit_target': {'enabled': True, 'pct_of_max_profit': 0.15},
                    'vol_expansion': {
                        'enabled': True,
                        'premium_multiple_threshold': 1.3,
                        'max_spot_move_pct': 0.005,
                    },
                    'hard_time_stop': '15:15',
                },
            },
        },
    }

    # Relaxed warmup: 50 obs of IV needed, ~3 days of OR
    strategy = IronFlyStrategy(
        iv_regime=IVRegimeFeature(lookback_days=60, min_observations=50),
        or_history_min_days=3,
    )

    # IV varies day-to-day so the IV-regime percentile filter has signal.
    # Without this the synthetic feed returns a constant IV and percentile
    # collapses to 0 every bar.
    def daily_varying_iv(ts, underlying):
        day_offset = (ts.date() - date(2024, 1, 1)).days
        # 0.10 -> 0.20 sinusoid, period ~14 days
        return 0.15 + 0.05 * np.sin(day_offset * 2 * np.pi / 14)

    chain_feed = SyntheticOptionChainFeed(
        atm_iv=0.15,
        skew=-0.02,
        smile=0.30,
        strike_interval={'NIFTY': 50, 'BANKNIFTY': 100},
        num_strikes_each_side=20,
        atm_iv_provider=daily_varying_iv,
    )

    runtime = BacktestRuntime(
        strategies=[strategy],
        simulator=BacktestSimulator(slippage_per_side=0, brokerage=20),
        config=config,
        multi_leg_simulator=MultiLegSimulator(mode=MODE_REALISTIC, brokerage_per_leg=20),
        chain_feed=chain_feed,
    )

    all_ml_trades = []
    rejection_counts = {}

    import time
    t_start = time.time()
    session_dates = sorted(sessions.keys())
    n_sessions = len(session_dates)
    print(f"\n>> Running {n_sessions} sessions...")

    for i, session_date in enumerate(session_dates, 1):
        session_df = sessions[session_date]
        # Apply feature pipeline in canonical order
        session_df = vwap_feat.calculate(session_df)
        session_df = or_feat.calculate(session_df)
        session_df = gap_feat.calculate(session_df)
        session_df = intraday_feat.calculate(session_df)
        session_df['session_date'] = session_date

        sl, ml, event_log = runtime.run_session_full('NIFTY', session_date, session_df)
        all_ml_trades.extend(ml)
        for ev in event_log:
            if ev.get('event_type') == 'no_signal' and ev.get('strategy') == 'IRON_FLY':
                r = ev.get('reason', 'unknown')
                rejection_counts[r] = rejection_counts.get(r, 0) + 1
        if i % 10 == 0 or i == n_sessions:
            elapsed = time.time() - t_start
            print(f"   [{i:>3}/{n_sessions}] {session_date}  trades_so_far={len(all_ml_trades):>3}  elapsed={elapsed:.1f}s")

    elapsed = time.time() - t_start
    print(f"\n>> Run complete in {elapsed:.1f}s")
    print(f">> Total multi-leg trades: {len(all_ml_trades)}")
    if all_ml_trades:
        gross = sum(t.gross_pnl or 0 for t in all_ml_trades)
        net = sum(t.net_pnl or 0 for t in all_ml_trades)
        wins = sum(1 for t in all_ml_trades if (t.net_pnl or 0) > 0)
        print(f"   Gross P&L: ₹{gross:,.2f}")
        print(f"   Net P&L:   ₹{net:,.2f}")
        print(f"   Win rate:  {wins / len(all_ml_trades):.1%}  ({wins}/{len(all_ml_trades)})")

        # By exit reason
        by_reason = {}
        for t in all_ml_trades:
            r = t.exit_reason or 'NONE'
            by_reason.setdefault(r, []).append(t.net_pnl or 0)
        print("\n   Exits by reason:")
        for reason, pnls in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            print(f"     {reason:<24} n={len(pnls):>3}  total ₹{sum(pnls):>12,.2f}  avg ₹{sum(pnls)/len(pnls):>9,.2f}")

        print("\n   First 5 trades:")
        for t in all_ml_trades[:5]:
            print(f"     {t.entry_time}  credit=₹{t.net_entry_credit:>8,.2f}  exit={t.exit_reason:<20}  net P&L=₹{t.net_pnl:>8,.2f}")

    print("\n>> Top rejection reasons (IRON_FLY):")
    for r, n in sorted(rejection_counts.items(), key=lambda kv: -kv[1])[:8]:
        print(f"   {r:<28} {n}")

    return 0 if all_ml_trades else 1


if __name__ == '__main__':
    sys.exit(main())
