import pandas as pd
from typing import Dict, List
from datetime import date, time

# NSE intraday market hours
_DEFAULT_SESSION_START = time(9, 15)
_DEFAULT_SESSION_END = time(15, 30)


class Sessionizer:
    """Splits continuous dataframe into daily sessions and computes prior close."""

    def __init__(
        self,
        session_start: time = _DEFAULT_SESSION_START,
        session_end: time = _DEFAULT_SESSION_END,
        filter_market_hours: bool = True,
    ):
        self.session_start = session_start
        self.session_end = session_end
        self.filter_market_hours = filter_market_hours

    def create_sessions(self, df: pd.DataFrame) -> Dict[date, pd.DataFrame]:
        """
        Groups the dataframe by session date and computes prior_close.
        Optionally filters out pre/post-market rows so real-data anomalies
        (holiday partials, extended-hours bars) don't corrupt the session.
        Returns a dict mapping date -> session dataframe.
        """
        if df.empty:
            return {}

        df = df.copy()  # do not mutate caller's dataframe

        if self.filter_market_hours:
            bar_time = df['timestamp'].dt.time
            df = df[(bar_time >= self.session_start) & (bar_time <= self.session_end)]

        if df.empty:
            return {}

        df['session_date'] = df['timestamp'].dt.date
        sessions = {dt: group.copy() for dt, group in df.groupby('session_date')}

        # Compute prior close and minute_index per session
        prior_close = None
        for dt in sorted(sessions.keys()):
            session_df = sessions[dt]
            session_df['prior_close'] = prior_close
            session_df['minute_index'] = range(len(session_df))
            prior_close = session_df['close'].iloc[-1]

        return sessions

    def validate_sessions(
        self,
        sessions: Dict[date, pd.DataFrame],
        expected_bars_min: int = 300,
        expected_bars_max: int = 376,
        warn_missing_minutes: bool = True,
    ) -> List[str]:
        """
        Runs basic quality checks on all sessions.
        Returns a flat list of warning strings. Empty list means no issues found.
        """
        warnings: List[str] = []
        for dt, issues in self.validate_sessions_detailed(
            sessions, expected_bars_min, expected_bars_max, warn_missing_minutes
        ).items():
            for issue in issues:
                warnings.append(f"{dt}: {issue}")
        return warnings

    def validate_sessions_detailed(
        self,
        sessions: Dict[date, pd.DataFrame],
        expected_bars_min: int = 300,
        expected_bars_max: int = 376,
        warn_missing_minutes: bool = True,
    ) -> Dict[date, List[str]]:
        """
        Like validate_sessions but returns per-session issues keyed by date.
        Used by ExperimentRunner to enforce session validation policy
        (warn / fail / skip).
        """
        return {
            dt: issues
            for dt, session_df in sessions.items()
            for issues in [self._check_session(
                session_df, expected_bars_min, expected_bars_max, warn_missing_minutes
            )]
            if issues
        }

    @staticmethod
    def _check_session(
        session_df: pd.DataFrame,
        expected_bars_min: int,
        expected_bars_max: int,
        warn_missing_minutes: bool,
    ) -> List[str]:
        issues: List[str] = []
        bar_count = len(session_df)

        if bar_count < expected_bars_min:
            issues.append(
                f"only {bar_count} bars (expected >= {expected_bars_min}) — partial session?"
            )
        if bar_count > expected_bars_max:
            issues.append(
                f"{bar_count} bars (expected <= {expected_bars_max}) — duplicate rows?"
            )

        if warn_missing_minutes and bar_count >= 2:
            ts = session_df['timestamp'].sort_values().reset_index(drop=True)
            diffs = ts.diff().dropna()
            gaps = diffs[diffs > pd.Timedelta(minutes=1)]
            for idx, gap in gaps.items():
                gap_start = ts.iloc[idx - 1]
                issues.append(
                    f"{int(gap.total_seconds() // 60)}-minute gap detected after {gap_start}"
                )

        return issues
