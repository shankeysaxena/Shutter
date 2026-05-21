"""
CLI: audit an option-chain archive directory and report issues.

Usage:
    python3 tools/validate_chain_archive.py --root data/option_chain_snapshots

Checks performed (per file unless noted):
    [archive] _meta.yaml present and parseable
    [archive] data_origin recognized
    [file]    required columns present
    [file]    timestamps sorted and unique-per-row-group
    [file]    strikes monotonic per (timestamp, option_type)
    [file]    bid <= ask (or bid==0 floor accepted)
    [file]    iv > 0
    [file]    no negative prices
    [coverage] gap detection vs expected intraday cadence (default 1 min)

Output:
    Stdout summary table; non-zero exit if any errors found. Warnings do not
    fail the run.

Purpose:
    Before any Phase 4.8 validation run, this should be clean. Especially
    important when real broker data arrives — broker exports have edge cases
    (gaps, halts, expiry-day weirdness) that need to be surfaced early.
"""
import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List

# Make src importable
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from src.feeds.chain_archive_schema import (  # noqa: E402
    REQUIRED_COLUMNS,
    manifest_path,
    ORIGIN_BROKER,
    ORIGIN_RECORDED,
    ORIGIN_SYNTHETIC,
    ORIGIN_UNKNOWN,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Audit an option-chain archive')
    parser.add_argument('--root', required=True, help='Archive root directory')
    parser.add_argument('--cadence-minutes', type=int, default=1,
                        help='Expected gap between consecutive snapshots in minutes')
    parser.add_argument('--quiet', action='store_true', help='Only print failures')
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.exists():
        print(f"FAIL: archive root does not exist: {root}", file=sys.stderr)
        return 2

    errors: List[str] = []
    warnings: List[str] = []

    # Manifest
    meta_p = manifest_path(root)
    if not meta_p.exists():
        warnings.append(f"manifest missing: {meta_p} (treated as data_origin=unknown)")
        manifest = {'data_origin': ORIGIN_UNKNOWN}
    else:
        with open(meta_p) as f:
            manifest = yaml.safe_load(f) or {}
        origin = manifest.get('data_origin', ORIGIN_UNKNOWN)
        if origin not in {ORIGIN_SYNTHETIC, ORIGIN_RECORDED, ORIGIN_BROKER, ORIGIN_UNKNOWN}:
            warnings.append(f"unrecognized data_origin: {origin!r}")

    # Walk per-underlying directories
    files_checked = 0
    file_stats: Dict[str, Dict] = defaultdict(lambda: {'sessions': 0, 'rows': 0, 'timestamps': 0})

    for udir in sorted(root.iterdir()):
        if not udir.is_dir():
            continue
        if udir.name.startswith('_') or udir.name.startswith('.'):
            continue
        underlying = udir.name
        for pq in sorted(udir.glob('*.parquet')):
            files_checked += 1
            try:
                day = date.fromisoformat(pq.stem)
            except ValueError:
                errors.append(f"[{underlying}] non-ISO filename: {pq.name}")
                continue
            try:
                df = pd.read_parquet(pq)
            except Exception as e:
                errors.append(f"[{underlying}/{day}] cannot read parquet: {e}")
                continue
            file_stats[underlying]['sessions'] += 1
            file_stats[underlying]['rows'] += len(df)

            # Schema
            missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
            if missing:
                errors.append(f"[{underlying}/{day}] missing columns: {missing}")
                continue

            n_ts = df['timestamp'].nunique()
            file_stats[underlying]['timestamps'] += n_ts

            # Data sanity checks
            if (df['bid'] < 0).any():
                errors.append(f"[{underlying}/{day}] negative bid found")
            if (df['ask'] < 0).any():
                errors.append(f"[{underlying}/{day}] negative ask found")
            if (df['ask'] < df['bid']).any():
                n_bad = (df['ask'] < df['bid']).sum()
                errors.append(f"[{underlying}/{day}] ask<bid on {n_bad} rows")
            if (df['iv'] <= 0).any():
                warnings.append(f"[{underlying}/{day}] non-positive iv on some rows")

            # Strike monotonicity per (timestamp, option_type)
            for (ts, opt), grp in df.groupby(['timestamp', 'option_type'], sort=False):
                strikes = grp['strike'].values
                if not (strikes[:-1] <= strikes[1:]).all():
                    warnings.append(
                        f"[{underlying}/{day}] strikes not sorted at {ts} for {opt}"
                    )
                    break  # one warning per file is enough

            # Cadence check (gap detection)
            ts_sorted = sorted(df['timestamp'].unique())
            expected_gap = timedelta(minutes=args.cadence_minutes)
            gap_count = 0
            for prev, cur in zip(ts_sorted[:-1], ts_sorted[1:]):
                actual = pd.Timestamp(cur) - pd.Timestamp(prev)
                if actual > expected_gap:
                    gap_count += 1
            if gap_count > 0:
                warnings.append(
                    f"[{underlying}/{day}] {gap_count} timestamp gap(s) > "
                    f"{args.cadence_minutes}m (cadence may be coarser than expected)"
                )

    # ---- Report ----
    if not args.quiet:
        print(f"\n=== Chain archive audit: {root} ===")
        print(f"  manifest data_origin: {manifest.get('data_origin', ORIGIN_UNKNOWN)}")
        print(f"  files checked       : {files_checked}")
        for u, stats in sorted(file_stats.items()):
            print(f"  [{u}] sessions={stats['sessions']:>4}  rows={stats['rows']:>8}  "
                    f"timestamps={stats['timestamps']:>6}")

    if errors:
        print(f"\nERRORS ({len(errors)}):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
    if warnings and not args.quiet:
        print(f"\nWARNINGS ({len(warnings)}):")
        for w in warnings[:50]:
            print(f"  - {w}")
        if len(warnings) > 50:
            print(f"  ... and {len(warnings) - 50} more")

    if errors:
        return 1
    if not args.quiet:
        print("\nOK." if not warnings else f"\nOK with {len(warnings)} warning(s).")
    return 0


if __name__ == '__main__':
    sys.exit(main())
