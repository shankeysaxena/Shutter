"""
Startup preflight checks.

Runs before the live session starts. Any FAIL result blocks startup.
WARNING results are logged and notified but do not block.

Checks:
  1. Kite token valid (calls /profile endpoint)
  2. Trading day (weekday + no public holiday heuristic)
  3. Market hours or pre-market window
  4. Internet connectivity (ping Kite API)
  5. Config sanity (instruments, risk caps present)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import List, Optional

logger = logging.getLogger(__name__)

_MARKET_OPEN    = time(9, 0)    # allow startup from 09:00
_MARKET_CLOSE   = time(15, 35)  # must start before this


@dataclass
class PreflightResult:
    passed: bool
    checks: List[dict] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = '') -> None:
        self.checks.append({'check': name, 'status': status, 'detail': detail})
        if status == 'FAIL':
            self.passed = False

    def summary(self) -> str:
        lines = ['Preflight checks:']
        for c in self.checks:
            icon = '✅' if c['status'] == 'PASS' else ('⚠️' if c['status'] == 'WARN' else '❌')
            lines.append(f"  {icon} {c['check']}: {c['detail']}")
        return '\n'.join(lines)


def run_preflight(config: dict, kite=None) -> PreflightResult:
    result = PreflightResult(passed=True)

    # 1. Token / Kite client
    if kite is not None:
        try:
            profile = kite.profile()
            result.add('Kite token', 'PASS', f"logged in as {profile.get('user_name', '?')}")
        except Exception as e:
            result.add('Kite token', 'FAIL', f"Invalid or expired: {e}")
    else:
        result.add('Kite token', 'WARN', 'Kite client not provided — skipping profile check')

    # 2. Trading day
    today = date.today()
    if today.weekday() >= 5:
        day_name = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][today.weekday()]
        result.add('Trading day', 'FAIL',
                   f"{today} is a {day_name} (weekend)")
    else:
        result.add('Trading day', 'PASS', str(today))

    # 3. Market hours window
    now = datetime.now().time()
    if now < _MARKET_OPEN:
        result.add('Market hours', 'WARN', f"Pre-market ({now.strftime('%H:%M')}). Feed will idle until 09:15.")
    elif now > _MARKET_CLOSE:
        result.add('Market hours', 'FAIL', f"Market already closed ({now.strftime('%H:%M')}). Start before 15:35.")
    else:
        result.add('Market hours', 'PASS', now.strftime('%H:%M'))

    # 4. Connectivity
    try:
        import requests
        r = requests.get('https://api.kite.trade', timeout=5)
        result.add('Connectivity', 'PASS', f"Kite API reachable (status {r.status_code})")
    except Exception as e:
        result.add('Connectivity', 'FAIL', f"Cannot reach Kite API: {e}")

    # 5. Config sanity
    instruments = config.get('instruments', [])
    if not instruments:
        result.add('Config: instruments', 'FAIL', 'No instruments configured')
    else:
        result.add('Config: instruments', 'PASS', str(instruments))

    re_cfg = config.get('risk_engine', {})
    if re_cfg.get('daily_loss_cap', 0) >= 0:
        result.add('Config: daily_loss_cap', 'FAIL',
                   'daily_loss_cap must be negative (e.g. -2500)')
    else:
        result.add('Config: daily_loss_cap', 'PASS',
                   str(re_cfg['daily_loss_cap']))

    return result
