"""
CLI: tag sessions by market regime and break down P&L by regime.

Phase 4.8B.2 — Analysis only. Strategy logic unchanged.

Usage:
    python3 tools/run_regime_analysis.py \\
        --run-dir runs/<run_folder> \\
        --data-dir data/raw/zerodha

Outputs:
    <run_dir>/regimes/
        session_regimes.csv   — one row per session with regime + metrics
        pnl_by_regime.csv     — P&L breakdown by (strategy, regime)
"""
import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import json  # noqa: E402
import pandas as pd  # noqa: E402

from src.analytics.regime import (  # noqa: E402
    pnl_by_regime,
    regimes_to_df,
    tag_sessions,
)


def _extract_or_widths_from_trades(trades_df: pd.DataFrame) -> dict:
    """Pull or_high / or_low from ORB metadata to get accurate per-day OR widths."""
    result = {}
    for _, row in trades_df.iterrows():
        try:
            meta = json.loads(row['metadata_json']) if isinstance(row['metadata_json'], str) else {}
            or_high = meta.get('or_high')
            or_low  = meta.get('or_low')
            if or_high and or_low and or_low > 0:
                mid = (or_high + or_low) / 2
                import datetime
                d = pd.to_datetime(row['entry_time']).date()
                result[d] = (or_high - or_low) / mid
        except Exception:
            pass
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Tag sessions by market regime and break down P&L')
    parser.add_argument('--run-dir',    required=True, help='Path to run directory')
    parser.add_argument('--data-dir',   required=True, help='Path to OHLCV data directory')
    parser.add_argument('--instrument', default='NIFTY')
    args = parser.parse_args(argv)

    run_dir  = Path(args.run_dir)
    data_dir = Path(args.data_dir)

    trades_path = run_dir / 'trades.csv'
    ohlcv_path  = data_dir / f'{args.instrument}.csv'

    if not trades_path.exists():
        print(f"ERROR: {trades_path} not found", file=sys.stderr); return 1
    if not ohlcv_path.exists():
        print(f"ERROR: {ohlcv_path} not found", file=sys.stderr); return 1

    trades_df = pd.read_csv(trades_path)
    ohlcv_df  = pd.read_csv(ohlcv_path, parse_dates=['timestamp'])

    # Use OR widths from trades metadata (more accurate than bar-level estimate)
    or_widths = _extract_or_widths_from_trades(trades_df)

    regimes = tag_sessions(ohlcv_df, or_width_by_date=or_widths)
    regime_df = regimes_to_df(regimes)
    pnl_df    = pnl_by_regime(trades_df, regimes)

    # Save
    out = run_dir / 'regimes'
    out.mkdir(parents=True, exist_ok=True)
    regime_df.to_csv(out / 'session_regimes.csv', index=False)
    pnl_df.to_csv(out / 'pnl_by_regime.csv', index=False)

    # Print
    print(f"\n{'═'*60}")
    print("  SESSION REGIMES")
    print(f"{'═'*60}")
    counts = regime_df['regime'].value_counts()
    for regime, n in counts.items():
        print(f"  {regime:<12} {n:>3} sessions")
    print()
    print(regime_df[['session_date','regime','or_width_pct','gap_abs_pct','session_range_pct']].to_string(index=False))

    print(f"\n{'═'*60}")
    print("  P&L BY REGIME")
    print(f"{'═'*60}")
    print(pnl_df.to_string(index=False))

    print(f"\nSaved → {out}/")
    return 0


if __name__ == '__main__':
    sys.exit(main())
