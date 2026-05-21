"""
CLI: post-process a backtest run directory and produce segmentation reports.

Usage:
    python3 tools/run_segmentation.py \\
        --run-dir runs/phase4_orb_vwap_gap_synthetic_20260519_004738_1343 \\
        --data-dir data/raw/zerodha

Outputs:
    <run_dir>/segments/
        by_or_width.csv
        by_gap.csv
        by_time.csv
        by_volatility.csv

Each file contains one row per bucket with: n_trades, win_rate, expectancy,
total_pnl, profit_factor, avg_r, max_drawdown.
"""
import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import pandas as pd  # noqa: E402

from src.analytics.segmentation import (  # noqa: E402
    build_session_features,
    save_segments,
    segment_trades,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Generate segmentation reports for a backtest run')
    parser.add_argument('--run-dir', required=True, help='Path to run directory')
    parser.add_argument('--data-dir', required=True, help='Path to OHLCV data directory')
    parser.add_argument('--instrument', default='NIFTY', help='Instrument symbol (default: NIFTY)')
    parser.add_argument('--strategy', nargs='*', help='Filter to specific strategies (default: all)')
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    data_dir = Path(args.data_dir)

    trades_path = run_dir / 'trades.csv'
    ohlcv_path = data_dir / f'{args.instrument}.csv'

    if not trades_path.exists():
        print(f"ERROR: trades.csv not found at {trades_path}", file=sys.stderr)
        return 1
    if not ohlcv_path.exists():
        print(f"ERROR: OHLCV file not found at {ohlcv_path}", file=sys.stderr)
        return 1

    print(f"Loading trades:  {trades_path}")
    trades_df = pd.read_csv(trades_path)
    print(f"  {len(trades_df)} trades")

    print(f"Loading OHLCV:   {ohlcv_path}")
    ohlcv_df = pd.read_csv(ohlcv_path, parse_dates=['timestamp'])
    session_df = build_session_features(ohlcv_df)
    print(f"  {len(session_df)} sessions")

    strategies = args.strategy or None
    segments = segment_trades(trades_df, session_df, strategies=strategies)

    if not segments:
        print("No segments produced — check that trades.csv has single-leg trades.")
        return 1

    save_segments(segments, run_dir)

    print(f"\nSegments saved → {run_dir}/segments/")
    for name, df in segments.items():
        print(f"\n{'─'*60}")
        print(f"  {name}")
        print(f"{'─'*60}")
        print(df.to_string(index=False))

    return 0


if __name__ == '__main__':
    sys.exit(main())
