import json
import pandas as pd
import numpy as np
from typing import List, Dict, Optional
from src.core.models import Trade
from src.core.option_models import MultiLegTrade

class MetricsEngine:
    """Computes trade ledgers and evaluation metrics."""

    @staticmethod
    def generate_trade_ledger(
        trades: List[Trade],
        multi_leg_trades: Optional[List[MultiLegTrade]] = None,
    ) -> pd.DataFrame:
        """
        Project single-leg + multi-leg trades into one unified DataFrame.
        Multi-leg trades carry None in single-leg-only columns (direction,
        stop/target, r_multiple) and vice versa. `trade_type` distinguishes.
        """
        data = []
        for t in trades or []:
            data.append({
                'trade_id': t.trade_id,
                'strategy': t.strategy_name,
                'instrument': t.instrument,
                'trade_type': 'single_leg',
                'structure': t.direction,
                'direction': t.direction,
                'entry_time': t.entry_time,
                'entry_price': t.entry_price,
                'stop_price': t.stop_price,
                'target_price': t.target_price,
                'exit_time': t.exit_time,
                'exit_price': t.exit_price,
                'exit_reason': t.exit_reason,
                'qty': t.qty,
                'n_legs': 1,
                'net_entry_credit': None,
                'net_exit_debit': None,
                'gross_pnl': t.gross_pnl,
                'net_pnl': t.net_pnl,
                'r_multiple': t.r_multiple,
                'runtime_mode': t.runtime_mode,
                'date': t.entry_time.date() if t.entry_time else None,
                'entry_hour': t.entry_time.hour if t.entry_time else None,
                'metadata_json': json.dumps(t.metadata, default=str) if t.metadata else None,
            })

        for t in multi_leg_trades or []:
            total_qty = sum(f.leg.qty for f in t.entry_fills) if t.entry_fills else 0
            data.append({
                'trade_id': t.trade_id,
                'strategy': t.strategy_name,
                'instrument': t.instrument,
                'trade_type': 'multi_leg',
                'structure': t.structure_type,
                'direction': None,
                'entry_time': t.entry_time,
                'entry_price': None,
                'stop_price': None,
                'target_price': None,
                'exit_time': t.exit_time,
                'exit_price': None,
                'exit_reason': t.exit_reason,
                'qty': total_qty,
                'n_legs': len(t.entry_fills),
                'net_entry_credit': t.net_entry_credit,
                'net_exit_debit': t.net_exit_debit,
                'gross_pnl': t.gross_pnl,
                'net_pnl': t.net_pnl,
                'r_multiple': None,
                'runtime_mode': t.runtime_mode,
                'date': t.entry_time.date() if t.entry_time else None,
                'entry_hour': t.entry_time.hour if t.entry_time else None,
                'metadata_json': json.dumps(t.metadata, default=str) if t.metadata else None,
            })

        if not data:
            return pd.DataFrame()
        return pd.DataFrame(data)

    @staticmethod
    def calculate_summary(ledger: pd.DataFrame) -> Dict:
        if ledger.empty:
            return {}

        # Sort by entry_time so cumulative PnL and drawdown are always chronological
        ledger = ledger.sort_values('entry_time').reset_index(drop=True)

        total_trades = len(ledger)
        winners = ledger[ledger['net_pnl'] > 0]
        losers = ledger[ledger['net_pnl'] <= 0]

        win_rate = len(winners) / total_trades if total_trades > 0 else 0
        avg_win = winners['net_pnl'].mean() if len(winners) > 0 else 0.0
        avg_loss = losers['net_pnl'].mean() if len(losers) > 0 else 0.0

        # Expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)
        loss_rate = 1 - win_rate
        expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)

        # Profit factor = gross wins / gross losses
        # None when there are no losses (all winners) to avoid inf in reports
        gross_wins = winners['net_pnl'].sum()
        gross_losses = abs(losers['net_pnl'].sum())
        profit_factor: float | None = gross_wins / gross_losses if gross_losses > 0 else None

        # Max drawdown (peak-to-trough on cumulative PnL)
        cum_pnl = ledger['net_pnl'].cumsum()
        running_max = cum_pnl.cummax()
        drawdown = cum_pnl - running_max
        max_drawdown = drawdown.min()

        # Longest consecutive losing streak
        is_loss = (ledger['net_pnl'] <= 0).astype(int)
        streak = 0
        max_streak = 0
        for v in is_loss:
            streak = streak + 1 if v else 0
            max_streak = max(max_streak, streak)

        return {
            'total_trades': total_trades,
            'win_rate': round(win_rate, 4),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'expectancy': round(expectancy, 2),
            'profit_factor': round(profit_factor, 4) if profit_factor is not None else None,
            'total_net_pnl': round(ledger['net_pnl'].sum(), 2),
            'max_drawdown': round(max_drawdown, 2),
            'max_consecutive_losses': max_streak,
            'avg_r_multiple': round(ledger['r_multiple'].mean(), 4) if 'r_multiple' in ledger.columns else None,
        }

    @staticmethod
    def pnl_by_day(ledger: pd.DataFrame) -> pd.DataFrame:
        """Daily net PnL summary."""
        if ledger.empty or 'date' not in ledger.columns:
            return pd.DataFrame()
        return ledger.groupby('date')['net_pnl'].sum().reset_index().rename(columns={'net_pnl': 'daily_net_pnl'})

    @staticmethod
    def aggregate_rejections(event_log: List[Dict]) -> Dict:
        """
        Counts no_signal events from the event log, grouped by strategy then reason.
        Returns: {strategy_name: {reason: count}}
        Requires event_log to be collected during the run.
        """
        from collections import defaultdict
        counts: Dict = defaultdict(lambda: defaultdict(int))
        for entry in event_log:
            if entry.get('event_type') == 'no_signal':
                strategy = entry.get('strategy', 'unknown')
                reason = entry.get('reason', 'unknown')
                counts[strategy][reason] += 1
        return {s: dict(reasons) for s, reasons in counts.items()}

    @staticmethod
    def pnl_by_strategy(ledger: pd.DataFrame) -> pd.DataFrame:
        """Per-strategy breakdown."""
        if ledger.empty:
            return pd.DataFrame()
        return ledger.groupby('strategy').agg(
            total_trades=('net_pnl', 'count'),
            total_net_pnl=('net_pnl', 'sum'),
            win_rate=('net_pnl', lambda x: (x > 0).mean()),
        ).reset_index()
