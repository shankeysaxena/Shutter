"""Shared utilities for constructing core models from dataframe rows."""
import pandas as pd
from src.core.models import Candle, FeatureSnapshot, BarEvent


def row_to_bar_event(row: pd.Series, instrument: str, runtime_mode: str) -> BarEvent:
    """
    Constructs a BarEvent from a fully-featured session dataframe row.
    Used by both BacktestRuntime and ReplayFeed so the construction logic
    lives in exactly one place.
    """
    candle = Candle(
        timestamp=row['timestamp'],
        instrument=instrument,
        open=row['open'],
        high=row['high'],
        low=row['low'],
        close=row['close'],
        volume=row['volume'],
    )
    features = FeatureSnapshot(
        session_date=row.get('session_date'),
        minute_index=row.get('minute_index', 0),
        prior_close=row.get('prior_close'),
        vwap=row.get('vwap'),
        vwap_distance=row.get('vwap_distance'),
        above_vwap=bool(row.get('above_vwap', False)),
        below_vwap=bool(row.get('below_vwap', False)),
        or_high=row.get('or_high'),
        or_low=row.get('or_low'),
        or_width=row.get('or_width'),
        or_ready=bool(row.get('or_ready', False)),
        gap_pct=row.get('gap_pct'),
        gap_direction=row.get('gap_direction'),
        session_high_so_far=row.get('session_high_so_far'),
        session_low_so_far=row.get('session_low_so_far'),
        intraday_atr=row.get('intraday_atr'),
        vwap_atr_distance=row.get('vwap_atr_distance'),
    )
    return BarEvent(candle=candle, features=features, is_bar_closed=True, runtime_mode=runtime_mode)
