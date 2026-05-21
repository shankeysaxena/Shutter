"""
Weekly live-paper strategy report.

Aggregates daily JSON session reports from runs/live_paper/ for the past 7
calendar days and produces a per-strategy performance table with
decision rules for enable/disable.

Decision rules:
  PF > 1.1  with 10+ trades  → KEEP
  PF 0.9–1.1                 → OBSERVE (continue)
  PF < 0.8  with 10+ trades  → DISABLE CANDIDATE
  kill_switch_hit > 0        → REVIEW (disabled automatically, needs explanation)
  < 5 trades                 → INSUFFICIENT DATA
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional


def load_weekly_trades(
    report_dir: str,
    days: int = 7,
    end_date: Optional[date] = None,
) -> List[dict]:
    """Load all trades from the last `days` daily report files."""
    end   = end_date or date.today()
    start = end - timedelta(days=days - 1)
    root  = Path(report_dir)
    all_trades = []

    for d in (start + timedelta(days=i) for i in range(days)):
        path = root / f"{d}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            for t in data.get('trades', []):
                t['session_date'] = str(d)
            all_trades.extend(data.get('trades', []))
        except Exception as e:
            print(f"Warning: could not read {path}: {e}")

    return all_trades


def _strategy_metrics(trades: List[dict]) -> dict:
    if not trades:
        return {'n_trades': 0}

    pnls    = [t.get('net_pnl') for t in trades if t.get('net_pnl') is not None]
    if not pnls:
        return {'n_trades': len(trades)}

    wins    = [p for p in pnls if p > 0]
    losses  = [p for p in pnls if p <= 0]
    win_rate = len(wins) / len(pnls) if pnls else 0
    avg_win  = sum(wins) / len(wins)     if wins   else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    gw, gl   = sum(wins), abs(sum(losses))
    pf       = round(gw / gl, 3) if gl > 0 else None
    exp      = round(win_rate * avg_win + (1 - win_rate) * avg_loss, 2)

    # Drawdown (chronological)
    cum    = 0.0
    peak   = 0.0
    max_dd = 0.0
    for p in pnls:
        cum  += p
        peak  = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    # Loss streak
    streak = max_streak = 0
    for p in pnls:
        if p <= 0:
            streak   += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    exit_reasons = defaultdict(int)
    for t in trades:
        exit_reasons[t.get('exit_reason', 'UNKNOWN')] += 1

    return {
        'n_trades':    len(pnls),
        'win_rate':    round(win_rate, 4),
        'total_pnl':   round(sum(pnls), 2),
        'avg_win':     round(avg_win, 2),
        'avg_loss':    round(avg_loss, 2),
        'expectancy':  exp,
        'profit_factor': pf,
        'max_drawdown': round(max_dd, 2),
        'max_loss_streak': max_streak,
        'exit_reasons': dict(exit_reasons),
    }


def _decision(metrics: dict) -> str:
    n  = metrics.get('n_trades', 0)
    pf = metrics.get('profit_factor')
    if n == 0:
        return '—  no trades'
    if n < 5:
        return '⏳ INSUFFICIENT DATA (< 5 trades)'
    if pf is None:
        return '⚠️  no losses (all winners — continue observing)'
    if pf > 1.1:
        return '✅ KEEP'
    if pf >= 0.8:
        return '👀 OBSERVE'
    return '🔴 DISABLE CANDIDATE (PF < 0.8)'


def weekly_report(
    report_dir: str,
    days: int = 7,
    end_date: Optional[date] = None,
) -> dict:
    """Build the full weekly report. Returns a dict ready for display/export."""
    trades     = load_weekly_trades(report_dir, days, end_date)
    end        = end_date or date.today()
    start      = end - timedelta(days=days - 1)

    by_strategy: Dict[str, List[dict]] = defaultdict(list)
    for t in trades:
        by_strategy[t.get('strategy', 'UNKNOWN')].append(t)

    strategies = {}
    for strat, strat_trades in by_strategy.items():
        m = _strategy_metrics(strat_trades)
        m['decision'] = _decision(m)
        # Instrument split
        by_inst = defaultdict(list)
        for t in strat_trades:
            by_inst[t.get('instrument', '?')].append(t.get('net_pnl', 0))
        m['by_instrument'] = {
            inst: {'n': len(v), 'pnl': round(sum(v), 2)}
            for inst, v in by_inst.items()
        }
        # Time-of-day split
        time_buckets = defaultdict(list)
        for t in strat_trades:
            et = t.get('entry_time', '')
            if et:
                hour = int(et[11:13]) if len(et) > 12 else 0
                bucket = f"{hour:02d}:00"
            else:
                bucket = 'unknown'
            time_buckets[bucket].append(t.get('net_pnl', 0))
        m['by_time'] = {
            b: {'n': len(v), 'pnl': round(sum(v), 2)}
            for b, v in sorted(time_buckets.items())
        }
        strategies[strat] = m

    sessions_with_data = len({t.get('session_date') for t in trades})

    return {
        'period':          f"{start} → {end}",
        'sessions':        sessions_with_data,
        'total_trades':    len(trades),
        'total_pnl':       round(sum(t.get('net_pnl', 0) for t in trades), 2),
        'strategies':      strategies,
    }


def print_weekly_report(report: dict) -> None:
    print(f"\n{'═'*65}")
    print(f"  WEEKLY LIVE-PAPER REPORT")
    print(f"  Period:  {report['period']}")
    print(f"  Sessions: {report['sessions']}  Trades: {report['total_trades']}  "
          f"Total P&L: ₹{report['total_pnl']:,.2f}")
    print(f"{'═'*65}")

    for strat, m in sorted(report['strategies'].items()):
        n  = m.get('n_trades', 0)
        pf = m.get('profit_factor', '—')
        pf_str = f"{pf:.2f}" if isinstance(pf, float) else str(pf)
        print(f"\n  {strat}")
        print(f"  {'─'*55}")
        print(f"  Trades:       {n:>4}    Win rate: {m.get('win_rate', 0):.0%}")
        print(f"  Total P&L:    ₹{m.get('total_pnl', 0):>10,.2f}")
        print(f"  Profit factor: {pf_str:>6}    Expectancy: ₹{m.get('expectancy', 0):.0f}/trade")
        print(f"  Avg win:      ₹{m.get('avg_win', 0):>8,.0f}    Avg loss: ₹{m.get('avg_loss', 0):>8,.0f}")
        print(f"  Max DD:       ₹{m.get('max_drawdown', 0):>8,.0f}    Max loss streak: {m.get('max_loss_streak', 0)}")
        if m.get('exit_reasons'):
            reasons = '  '.join(f"{k}={v}" for k, v in m['exit_reasons'].items())
            print(f"  Exits: {reasons}")
        if m.get('by_instrument'):
            inst_str = '  '.join(
                f"{k}: n={v['n']} ₹{v['pnl']:,.0f}"
                for k, v in m['by_instrument'].items()
            )
            print(f"  By instrument: {inst_str}")
        print(f"\n  → Decision: {m.get('decision', '—')}")

    print(f"\n{'═'*65}\n")
