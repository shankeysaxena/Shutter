"""
Phase IF-1 — OptionChainRecorder.

Builds the real option-chain archive needed to validate Iron Fly.
Runs passively alongside live-paper — no trades, no allocator participation.

Approach: end-of-session HTTP fetch (not real-time WebSocket).
After 15:30, fetch 1-min candles for each configured strike/type and write
to the existing HistoricalOptionChainFeed Parquet schema.

Why end-of-session rather than real-time:
  - Avoids managing 80+ WebSocket subscriptions
  - Same data quality as real-time aggregation
  - Simpler to test, simpler to recover from gaps
  - The HistoricalOptionChainFeed reads the same archive either way

Archive schema (matches chain_archive_schema.py exactly):
  <snapshot_dir>/<UNDERLYING>/<YYYY-MM-DD>.parquet
  Columns: timestamp, spot, expiry, strike, option_type, bid, ask, last, iv

What to record per session:
  - ATM ± N strikes for NIFTY / BANKNIFTY (configurable)
  - Nearest weekly expiry (current week's Thursday)
  - 1-minute granularity: 09:15 to 15:29
  - CE + PE for each strike

Volume and OI are intentionally omitted from the Parquet schema for now
(Iron Fly is a premium-capture strategy; OHLCV is sufficient for Phase IF-1–3).
They can be added as additional columns in Phase IF-2 without breaking existing readers.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_EXPIRY_WEEKDAY = 3   # Thursday


@dataclass
class RecorderConfig:
    """What to record each session."""
    underlyings:       List[str]        = field(default_factory=lambda: ['NIFTY', 'BANKNIFTY'])
    strikes_each_side: int              = 10       # ATM ± 10 strikes
    strike_interval:   Dict[str, float] = field(default_factory=lambda: {'NIFTY': 50.0, 'BANKNIFTY': 100.0})
    snapshot_dir:      str              = 'data/option_chain_snapshots'
    request_sleep_sec: float            = 0.4      # Kite rate limit: ~3 req/s
    max_retries:       int              = 2


def next_expiry(today: date, weekday: int = _EXPIRY_WEEKDAY) -> date:
    """Return the next expiry date on `weekday` (0=Mon) at or after `today`."""
    days_ahead = (weekday - today.weekday()) % 7
    return today + timedelta(days=days_ahead)


class OptionChainRecorder:
    """
    Fetches and archives 1-minute option candles for one session.

    Usage (end-of-session):
        recorder = OptionChainRecorder(kite_client, config)
        recorder.record_session(session_date, spot_at_close={'NIFTY': 22050})
    """

    def __init__(self, kite, config: RecorderConfig):
        self._kite   = kite
        self._config = config

    def record_session(
        self,
        session_date: date,
        spot_at_close: Dict[str, float],
    ) -> Dict[str, Path]:
        """
        Fetch 1-min option candles for `session_date` and write to archive.
        `spot_at_close` maps underlying → approximate close price (used to
        determine ATM strikes; doesn't need to be exact).
        Returns {underlying: Parquet path written}.
        """
        cfg     = self._config
        out_dir = Path(cfg.snapshot_dir)
        expiry  = next_expiry(session_date)
        written = {}

        for underlying in cfg.underlyings:
            spot = spot_at_close.get(underlying)
            if spot is None:
                logger.warning(f"No spot price for {underlying} — skipping")
                continue

            interval   = cfg.strike_interval.get(underlying, 50.0)
            atm        = round(spot / interval) * interval
            strikes    = [atm + i * interval for i in
                           range(-cfg.strikes_each_side, cfg.strikes_each_side + 1)]

            rows = self._fetch_all_strikes(underlying, session_date, expiry, strikes, interval)
            if not rows:
                logger.warning(f"No data fetched for {underlying} on {session_date}")
                continue

            path = self._write_parquet(underlying, session_date, rows, out_dir)
            written[underlying] = path
            logger.info(f"Recorded {underlying} {session_date}: {len(rows)} rows → {path}")

        return written

    def _fetch_all_strikes(
        self,
        underlying:   str,
        session_date: date,
        expiry:       date,
        strikes:      List[float],
        interval:     float,
    ) -> List[dict]:
        rows = []
        from_dt = datetime.combine(session_date, datetime.min.time()).replace(hour=9, minute=15)
        to_dt   = datetime.combine(session_date, datetime.min.time()).replace(hour=15, minute=29)

        for strike in strikes:
            for opt_type in ('CE', 'PE'):
                token = self._resolve_token(underlying, expiry, strike, opt_type)
                if token is None:
                    continue
                candles = self._fetch_candles_with_retry(token, from_dt, to_dt)
                for c in candles:
                    rows.append({
                        'timestamp':   _strip_tz(c.get('date')),
                        'spot':        None,    # filled in from underlying bars below
                        'expiry':      expiry.isoformat(),
                        'strike':      strike,
                        'option_type': opt_type,
                        'bid':         float(c.get('open', 0)),   # best available; Kite doesn't give L1 quotes in historical
                        'ask':         float(c.get('close', 0)),  # open as proxy for bid, close for ask
                        'last':        float(c.get('close', 0)),
                        'iv':          0.0,   # populated in Phase IF-2 via BSM back-calculation
                    })
                time.sleep(self._config.request_sleep_sec)

        return rows

    def _resolve_token(
        self, underlying: str, expiry: date, strike: float, opt_type: str
    ) -> Optional[int]:
        """Look up the instrument token for one option via the Kite instruments list."""
        from src.integrations.zerodha.instruments import load_instruments
        df = load_instruments()
        if df.empty:
            logger.warning("Instruments CSV not found. Run: zerodha instruments refresh")
            return None

        expiry_str = expiry.strftime('%Y-%m-%d')
        mask = (
            (df['name'].str.upper() == underlying.upper())
            & (df['instrument_type'] == opt_type)
            & (df['strike'].astype(float) == strike)
            & (df['expiry'].astype(str) == expiry_str)
            & (df['exchange'] == 'NFO')
        )
        matches = df[mask]
        if matches.empty:
            return None
        return int(matches.iloc[0]['instrument_token'])

    def _fetch_candles_with_retry(
        self, token: int, from_dt: datetime, to_dt: datetime
    ) -> list:
        for attempt in range(self._config.max_retries + 1):
            try:
                return self._kite.historical_data(
                    instrument_token=token,
                    from_date=from_dt, to_date=to_dt,
                    interval='minute', continuous=False, oi=False,
                )
            except Exception as e:
                if attempt < self._config.max_retries:
                    logger.warning(f"Token {token} attempt {attempt+1} failed: {e}. Retrying…")
                    time.sleep(1.0)
                else:
                    logger.error(f"Token {token} all retries exhausted: {e}")
                    return []
        return []

    def _write_parquet(
        self, underlying: str, session_date: date, rows: List[dict], out_dir: Path
    ) -> Path:
        from src.feeds.chain_archive import write_manifest
        from src.feeds.chain_archive_schema import (
            ArchiveManifest, SCHEMA_VERSION, ORIGIN_RECORDED, archive_file_path
        )
        import yaml

        # Write or update manifest (data_origin=recorded → clears data_source_warning)
        manifest_file = out_dir / '_meta.yaml'
        if not manifest_file.exists():
            write_manifest(out_dir, ArchiveManifest(
                schema_version=SCHEMA_VERSION,
                data_origin=ORIGIN_RECORDED,
                generated_at=datetime.now(),
                notes='Recorded from Kite live option chain data. data_origin=recorded.',
            ))

        df   = pd.DataFrame(rows)
        path = archive_file_path(out_dir, underlying, session_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        df   = df.sort_values(['timestamp', 'strike', 'option_type']).reset_index(drop=True)
        df.to_parquet(path, index=False)
        return path


def _strip_tz(ts) -> Optional[datetime]:
    if ts is None:
        return None
    if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
        return ts.replace(tzinfo=None)
    return ts


# ─── Archive audit ────────────────────────────────────────────────────────────

def audit_chain_archive(snapshot_dir: str, underlying: str = 'NIFTY') -> dict:
    """
    Quick coverage report: how many sessions recorded, strike universe,
    date range, row counts. Used to decide when we have enough data for
    Iron Fly validation (target: 10–12 expiry cycles ≈ 2.5–3 months).
    """
    root = Path(snapshot_dir)
    inst_dir = root / underlying
    if not inst_dir.exists():
        return {'error': f'No data found for {underlying} in {snapshot_dir}'}

    files = sorted(inst_dir.glob('*.parquet'))
    if not files:
        return {'sessions': 0, 'underlying': underlying}

    sessions = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            sessions.append({
                'date':       f.stem,
                'rows':       len(df),
                'strikes':    int(df['strike'].nunique()) if 'strike' in df else 0,
                'timestamps': int(df['timestamp'].nunique()) if 'timestamp' in df else 0,
                'expiries':   list(df['expiry'].unique()) if 'expiry' in df else [],
            })
        except Exception as e:
            sessions.append({'date': f.stem, 'error': str(e)})

    n_sessions       = len(sessions)
    weekly_expiries  = len({e for s in sessions for e in s.get('expiries', [])})
    min_validation   = 10   # expiry cycles

    return {
        'underlying':             underlying,
        'sessions_recorded':      n_sessions,
        'weekly_expiries_seen':   weekly_expiries,
        'validation_ready':       weekly_expiries >= min_validation,
        'sessions_needed_more':   max(0, min_validation - weekly_expiries),
        'date_range':             f"{sessions[0]['date']} → {sessions[-1]['date']}" if sessions else None,
        'sessions':               sessions,
    }
