"""
Chain archive on-disk schema (Phase 4.8a).

Directory layout:
    <root>/
        _meta.yaml                          # archive manifest (data_origin etc.)
        <UNDERLYING>/
            YYYY-MM-DD.parquet              # one file per session
            ...

Per-file Parquet schema (one row per quote, per timestamp):
    timestamp     datetime64[ns]   bar timestamp (no timezone; assume IST)
    spot          float64          underlying spot at this timestamp
    expiry        string ISO date  option expiry (YYYY-MM-DD)
    strike        float64
    option_type   string           'CE' | 'PE'
    bid           float64          best bid (real from WebSocket depth; proxy in historical fallback)
    ask           float64          best ask
    last          float64          last traded price
    iv            float64          implied vol as decimal (0.15 == 15%)

Phase 2 optional columns (present when recorded via WebSocket real-time feed):
    volume        int64            cumulative day volume at this bar
    oi            int64            open interest at this bar

Readers that don't need volume/OI can ignore these columns safely.
A single file is expected to contain ~375 timestamps × ~80 strikes×2-types ≈ 30k rows.
Files are sorted by (timestamp, strike, option_type) on write for efficient scan.
"""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# Required Parquet columns (present in all archive files)
REQUIRED_COLUMNS = [
    'timestamp',
    'spot',
    'expiry',
    'strike',
    'option_type',
    'bid',
    'ask',
    'last',
    'iv',
]

# Optional Phase 2 columns — present when recorded via WebSocket real-time feed
OPTIONAL_COLUMNS = ['volume', 'oi']

# Manifest file name at the archive root
META_FILENAME = '_meta.yaml'

# Schema version — bump when on-disk format changes incompatibly
SCHEMA_VERSION = 1

# data_origin values. Loader/runner uses these to decide whether the
# `data_source_warning` should clear in ExperimentResult.
ORIGIN_SYNTHETIC = 'synthetic'
ORIGIN_RECORDED = 'recorded'
ORIGIN_BROKER = 'broker'
ORIGIN_UNKNOWN = 'unknown'

_REAL_ORIGINS = {ORIGIN_RECORDED, ORIGIN_BROKER}


@dataclass
class ArchiveManifest:
    """Parsed contents of <root>/_meta.yaml."""
    schema_version: int
    data_origin: str
    generated_at: Optional[datetime] = None
    notes: Optional[str] = None

    def is_real_data(self) -> bool:
        """Whether this archive can clear the synthetic-chain warning.

        Defensive: anything other than 'recorded' or 'broker' (including the
        default 'synthetic' and any unknown value) keeps the warning on.
        """
        return self.data_origin in _REAL_ORIGINS


def archive_file_path(root: Path, underlying: str, session_date) -> Path:
    """Resolve the Parquet path for one (instrument, date) tuple."""
    return Path(root) / underlying / f"{session_date.isoformat()}.parquet"


def manifest_path(root: Path) -> Path:
    return Path(root) / META_FILENAME
