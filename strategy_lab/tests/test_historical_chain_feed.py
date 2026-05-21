"""Tests for HistoricalOptionChainFeed + chain_archive export round-trip (Phase 4.8a)."""
import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

# Phase 4.8a: skip cleanly when pyarrow isn't installed instead of failing.
# CI / local dev with the full requirements-dev.txt will have pyarrow.
pytest.importorskip("pyarrow")

from src.feeds.chain_archive import export_chain_archive, write_manifest
from src.feeds.chain_archive_schema import (
    ArchiveManifest,
    ORIGIN_BROKER,
    ORIGIN_RECORDED,
    ORIGIN_SYNTHETIC,
    ORIGIN_UNKNOWN,
    REQUIRED_COLUMNS,
    SCHEMA_VERSION,
    archive_file_path,
    manifest_path,
)
from src.feeds.option_chain_snapshot import (
    HistoricalOptionChainFeed,
    SyntheticOptionChainFeed,
    WeeklyExpiryProvider,
)


@pytest.fixture
def tmp_archive():
    d = tempfile.mkdtemp()
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def populated_synthetic_archive(tmp_archive):
    """Build a small synthetic archive: 1 day, 3 timestamps, NIFTY ATM ≈ 22000."""
    feed = SyntheticOptionChainFeed(
        atm_iv=0.15,
        skew=-0.02,
        smile=0.30,
        strike_interval={'NIFTY': 50.0},
        num_strikes_each_side=5,
        expiry_provider=WeeklyExpiryProvider(weekday=3),
    )
    bars = [
        (datetime(2024, 1, 2, 9, 30), 22000.0),
        (datetime(2024, 1, 2, 10, 30), 22010.0),
        (datetime(2024, 1, 2, 14, 0), 21995.0),
    ]
    manifest = ArchiveManifest(
        schema_version=SCHEMA_VERSION,
        data_origin=ORIGIN_SYNTHETIC,
        generated_at=datetime(2024, 1, 2, 16, 0),
        notes='test fixture',
    )
    export_chain_archive(feed, tmp_archive, 'NIFTY', bars, manifest)
    return tmp_archive


class TestExporterRoundTrip:
    def test_files_written(self, populated_synthetic_archive):
        root = populated_synthetic_archive
        assert (root / '_meta.yaml').exists()
        assert (root / 'NIFTY' / '2024-01-02.parquet').exists()

    def test_schema_columns_present(self, populated_synthetic_archive):
        df = pd.read_parquet(populated_synthetic_archive / 'NIFTY' / '2024-01-02.parquet')
        for col in REQUIRED_COLUMNS:
            assert col in df.columns

    def test_three_timestamps_recorded(self, populated_synthetic_archive):
        df = pd.read_parquet(populated_synthetic_archive / 'NIFTY' / '2024-01-02.parquet')
        assert df['timestamp'].nunique() == 3

    def test_load_returns_same_quotes(self, populated_synthetic_archive):
        feed_loaded = HistoricalOptionChainFeed(str(populated_synthetic_archive))
        snap = feed_loaded.snapshot_at(datetime(2024, 1, 2, 9, 30), 'NIFTY', 22000.0)
        assert snap is not None
        # 5 strikes each side + ATM = 11 strikes × 2 types = 22 quotes
        assert len(snap.quotes) == 22
        # Has ATM strike
        assert any(q.strike == 22000 and q.option_type == 'CE' for q in snap.quotes)


class TestHistoricalChainFeed:
    def test_missing_directory_returns_none(self, tmp_archive):
        feed = HistoricalOptionChainFeed(str(tmp_archive / 'does_not_exist'))
        assert feed.snapshot_at(datetime(2024, 1, 2, 10, 0), 'NIFTY', 22000) is None

    def test_missing_day_returns_none(self, populated_synthetic_archive):
        feed = HistoricalOptionChainFeed(str(populated_synthetic_archive))
        assert feed.snapshot_at(datetime(2024, 1, 3, 10, 0), 'NIFTY', 22000) is None

    def test_unknown_timestamp_within_known_day_returns_none(self, populated_synthetic_archive):
        feed = HistoricalOptionChainFeed(str(populated_synthetic_archive))
        # 2024-01-02 has timestamps at 09:30 / 10:30 / 14:00 — not 12:00
        assert feed.snapshot_at(datetime(2024, 1, 2, 12, 0), 'NIFTY', 22000) is None

    def test_known_timestamp_returns_snapshot(self, populated_synthetic_archive):
        feed = HistoricalOptionChainFeed(str(populated_synthetic_archive))
        snap = feed.snapshot_at(datetime(2024, 1, 2, 10, 30), 'NIFTY', 22010.0)
        assert snap is not None
        assert snap.underlying == 'NIFTY'
        assert snap.spot == 22010.0
        assert snap.expiry == date(2024, 1, 4)   # next Thursday

    def test_repeat_query_uses_cache(self, populated_synthetic_archive, monkeypatch):
        feed = HistoricalOptionChainFeed(str(populated_synthetic_archive))
        feed.snapshot_at(datetime(2024, 1, 2, 9, 30), 'NIFTY', 22000.0)
        # After first load, the day's data should be in the cache.
        # Simulate a disk failure on a subsequent read — repeat must still succeed.
        import pandas as pd
        original = pd.read_parquet
        def boom(*a, **k):
            raise IOError("disk should not be touched on cache hit")
        monkeypatch.setattr(pd, 'read_parquet', boom)
        snap = feed.snapshot_at(datetime(2024, 1, 2, 10, 30), 'NIFTY', 22010.0)
        assert snap is not None

    def test_schema_violation_raises_in_strict_mode(self, tmp_archive):
        # Build a malformed Parquet file (missing 'iv' column)
        (tmp_archive / 'NIFTY').mkdir(parents=True)
        bad = pd.DataFrame([{
            'timestamp': datetime(2024, 1, 2, 10, 0),
            'spot': 22000.0, 'expiry': '2024-01-04', 'strike': 22000.0,
            'option_type': 'CE', 'bid': 1.0, 'ask': 1.1, 'last': 1.05,
            # 'iv' missing
        }])
        bad.to_parquet(tmp_archive / 'NIFTY' / '2024-01-02.parquet', index=False)
        feed = HistoricalOptionChainFeed(str(tmp_archive), strict_schema=True)
        with pytest.raises(ValueError, match='missing required columns'):
            feed.snapshot_at(datetime(2024, 1, 2, 10, 0), 'NIFTY', 22000.0)


class TestDataOriginPropagation:
    def test_synthetic_origin_in_manifest(self, populated_synthetic_archive):
        feed = HistoricalOptionChainFeed(str(populated_synthetic_archive))
        assert feed.data_origin == ORIGIN_SYNTHETIC

    def test_unknown_when_manifest_missing(self, tmp_archive):
        # No _meta.yaml in this dir
        feed = HistoricalOptionChainFeed(str(tmp_archive))
        assert feed.data_origin == ORIGIN_UNKNOWN

    def test_recorded_origin(self, tmp_archive):
        write_manifest(tmp_archive, ArchiveManifest(
            schema_version=SCHEMA_VERSION, data_origin=ORIGIN_RECORDED,
        ))
        feed = HistoricalOptionChainFeed(str(tmp_archive))
        assert feed.data_origin == ORIGIN_RECORDED
        assert feed.manifest.is_real_data()

    def test_broker_origin(self, tmp_archive):
        write_manifest(tmp_archive, ArchiveManifest(
            schema_version=SCHEMA_VERSION, data_origin=ORIGIN_BROKER,
        ))
        feed = HistoricalOptionChainFeed(str(tmp_archive))
        assert feed.data_origin == ORIGIN_BROKER
        assert feed.manifest.is_real_data()

    def test_synthetic_is_not_real_data(self, populated_synthetic_archive):
        feed = HistoricalOptionChainFeed(str(populated_synthetic_archive))
        assert feed.manifest.is_real_data() is False

    def test_synthetic_chain_feed_origin(self):
        feed = SyntheticOptionChainFeed()
        assert feed.data_origin == 'synthetic'


class TestWarningClearsForRealData:
    """The runner's data_source_warning should clear ONLY for 'recorded'/'broker'."""

    def _build_feed_with_origin(self, tmp_archive, origin):
        write_manifest(tmp_archive, ArchiveManifest(
            schema_version=SCHEMA_VERSION, data_origin=origin,
        ))
        return HistoricalOptionChainFeed(str(tmp_archive))

    def test_synthetic_keeps_warning(self, tmp_archive):
        f = self._build_feed_with_origin(tmp_archive, ORIGIN_SYNTHETIC)
        assert not f.manifest.is_real_data()

    def test_recorded_clears_warning(self, tmp_archive):
        f = self._build_feed_with_origin(tmp_archive, ORIGIN_RECORDED)
        assert f.manifest.is_real_data()

    def test_unknown_keeps_warning(self, tmp_archive):
        f = self._build_feed_with_origin(tmp_archive, 'wishful_thinking')
        assert not f.manifest.is_real_data()
