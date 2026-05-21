import warnings
import pandas as pd
from pathlib import Path

REQUIRED_COLUMNS = {'timestamp', 'open', 'high', 'low', 'close', 'volume'}
OHLCV_NUMERIC = ['open', 'high', 'low', 'close', 'volume']

class DataLoader:
    """Loads and standardizes historical OHLCV data."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)

    def load_historical_data(self, instrument: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        Loads CSV data for an instrument.
        Expected columns: timestamp, open, high, low, close, volume.
        Raises ValueError on schema or data quality failures.

        start_date / end_date accept ISO strings or None.
        None means "use the full extent of the file" — equivalent to
        writing `start: null` / `end: null` in config.
        """
        file_path = self.data_dir / f"{instrument}.csv"
        if not file_path.exists():
            raise FileNotFoundError(f"Data file for {instrument} not found at {file_path}")

        df = pd.read_csv(file_path, parse_dates=['timestamp'])

        # Column presence
        missing = REQUIRED_COLUMNS - set(df.columns)
        if missing:
            raise ValueError(f"{instrument}: missing required columns: {missing}")

        # Null OHLCV
        null_counts = df[OHLCV_NUMERIC].isnull().sum()
        if null_counts.any():
            raise ValueError(f"{instrument}: null values found in OHLCV columns:\n{null_counts[null_counts > 0]}")

        df.sort_values('timestamp', inplace=True)
        df.reset_index(drop=True, inplace=True)

        # Duplicate timestamps
        dupes = df['timestamp'].duplicated().sum()
        if dupes > 0:
            raise ValueError(f"{instrument}: {dupes} duplicate timestamps found")

        # Monotonic check (guaranteed after sort, but assert)
        if not df['timestamp'].is_monotonic_increasing:
            raise ValueError(f"{instrument}: timestamps are not monotonically increasing after sort")

        total_rows = len(df)
        file_start = df['timestamp'].iloc[0].date()
        file_end = df['timestamp'].iloc[-1].date()

        if start_date:
            df = df[df['timestamp'] >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df['timestamp'] <= pd.to_datetime(end_date)]

        if df.empty:
            raise ValueError(
                f"{instrument}: no data found for date range "
                f"{start_date or 'start'} → {end_date or 'end'}. "
                f"File contains data from {file_start} to {file_end}. "
                f"Update date_range in your config or set start/end to null to use all available data."
            )

        # Warn when the date filter silently discards a large portion of the file.
        # Threshold: if more than 20% of rows were filtered out, surface it.
        filtered_rows = len(df)
        discarded = total_rows - filtered_rows
        if discarded > 0:
            discarded_pct = discarded / total_rows
            if discarded_pct > 0.20:
                warnings.warn(
                    f"{instrument}: date_range filter discarded {discarded} of {total_rows} rows "
                    f"({discarded_pct:.0%}). File spans {file_start} → {file_end} but config "
                    f"requests {start_date or file_start} → {end_date or file_end}. "
                    f"Set start/end to null in config to use all available data.",
                    stacklevel=3,
                )

        df.reset_index(drop=True, inplace=True)
        return df
