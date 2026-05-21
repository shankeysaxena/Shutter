"""
Chain archive exporter — drives an OptionChainFeed across a (date, timestamp,
spot) grid and writes the resulting snapshots to disk in the Parquet schema
defined in chain_archive_schema.py.

Primary use: produce a deterministic synthetic archive that exercises the
full HistoricalOptionChainFeed path, so Phase 4.8 plumbing can be tested
before real broker data is available.
"""
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, List, Tuple

import pandas as pd
import yaml

from src.feeds.chain_archive_schema import (
    REQUIRED_COLUMNS,
    SCHEMA_VERSION,
    ArchiveManifest,
    archive_file_path,
    manifest_path,
)
from src.feeds.option_chain_snapshot import OptionChainFeed


def write_manifest(root: Path, manifest: ArchiveManifest) -> None:
    """Write the archive's _meta.yaml. Overwrites if present."""
    Path(root).mkdir(parents=True, exist_ok=True)
    payload = {
        'schema_version': manifest.schema_version,
        'data_origin': manifest.data_origin,
    }
    if manifest.generated_at is not None:
        payload['generated_at'] = manifest.generated_at.isoformat()
    if manifest.notes is not None:
        payload['notes'] = manifest.notes
    with open(manifest_path(Path(root)), 'w') as f:
        yaml.safe_dump(payload, f, sort_keys=False)


def export_chain_archive(
    feed: OptionChainFeed,
    out_dir: Path,
    underlying: str,
    bars: Iterable[Tuple[datetime, float]],
    manifest: ArchiveManifest,
) -> Path:
    """
    Drive `feed.snapshot_at` for every (timestamp, spot) in `bars` and write
    the resulting snapshots to per-day Parquet files under `out_dir/{underlying}/`.

    `bars` is an iterable of (timestamp, spot) pairs ordered by timestamp.
    A new Parquet file is written each time the session date changes.

    Returns the archive root path. Manifest is (over)written at the root.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    write_manifest(out_dir, manifest)

    current_day: date = None
    rows_for_day: List[dict] = []

    def _flush(day: date) -> None:
        if not rows_for_day:
            return
        path = archive_file_path(out_dir, underlying, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(rows_for_day, columns=REQUIRED_COLUMNS)
        df = df.sort_values(['timestamp', 'strike', 'option_type']).reset_index(drop=True)
        df.to_parquet(path, index=False)

    n_snapshots = 0
    for ts, spot in bars:
        snap = feed.snapshot_at(ts, underlying, spot)
        if snap is None:
            continue
        day = ts.date()
        if current_day is None:
            current_day = day
        elif day != current_day:
            _flush(current_day)
            rows_for_day = []
            current_day = day
        for q in snap.quotes:
            rows_for_day.append({
                'timestamp': snap.timestamp,
                'spot': snap.spot,
                'expiry': snap.expiry.isoformat() if isinstance(snap.expiry, date) else str(snap.expiry),
                'strike': q.strike,
                'option_type': q.option_type,
                'bid': q.bid,
                'ask': q.ask,
                'last': q.last,
                'iv': q.iv,
            })
        n_snapshots += 1

    if current_day is not None:
        _flush(current_day)
    return out_dir


def export_from_ohlcv(
    feed: OptionChainFeed,
    ohlcv_csv: Path,
    out_dir: Path,
    underlying: str,
    manifest: ArchiveManifest,
    sample_every_n_minutes: int = 1,
) -> Path:
    """
    Convenience: read a NIFTY/BANKNIFTY OHLCV CSV (timestamp + close columns)
    and drive the chain feed at every bar's close. Useful for generating a
    synthetic archive aligned to an existing OHLCV dataset.

    `sample_every_n_minutes`: down-sample for faster export / smaller archives
    while developing. Production archives should keep sample_every_n_minutes=1.
    """
    df = pd.read_csv(ohlcv_csv, parse_dates=['timestamp'])
    df = df.sort_values('timestamp').reset_index(drop=True)
    if sample_every_n_minutes > 1:
        df = df.iloc[::sample_every_n_minutes]
    bars = [(row['timestamp'].to_pydatetime(), float(row['close']))
            for _, row in df.iterrows()]
    return export_chain_archive(feed, out_dir, underlying, bars, manifest)
