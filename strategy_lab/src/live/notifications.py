"""
NotificationService — Telegram alerts for the live runtime.

Levels:
  INFO     — routine events (session start, fills, summary)
  WARNING  — degraded state (cooldown, stale feed, bar gap)
  CRITICAL — action required (halt, kill switch, failed exit, no token)

Design:
  - Falls back silently if Telegram not configured (no crash)
  - Never blocks the trading loop — fire-and-forget via a background thread
  - Self-correction may reconnect feeds, but NEVER overrides risk halts;
    this service only notifies, never modifies RiskState

Setup (once):
  1. Message @BotFather on Telegram → /newbot → copy bot token
  2. Send any message to your new bot
  3. curl https://api.telegram.org/bot<TOKEN>/getUpdates → copy chat_id
  4. Add to .env:
       TELEGRAM_BOT_TOKEN=...
       TELEGRAM_CHAT_ID=...
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Alert level constants
INFO     = 'INFO'
WARNING  = 'WARNING'
CRITICAL = 'CRITICAL'

_EMOJI = {
    INFO:     'ℹ️',
    WARNING:  '⚠️',
    CRITICAL: '🚨',
}


class NotificationService:
    """
    Fire-and-forget Telegram notifier. Never raises on failure —
    a notification bug must not affect trade execution.
    """

    def __init__(
        self,
        bot_token:    Optional[str] = None,
        chat_id:      Optional[str] = None,
        min_level:    str           = INFO,
        session_name: str           = 'live-paper',
    ):
        self._token       = bot_token or os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
        self._chat_id     = chat_id   or os.environ.get('TELEGRAM_CHAT_ID', '').strip()
        self._min_level   = min_level
        self._session     = session_name
        self._configured  = bool(self._token and self._chat_id)
        self._level_order = {INFO: 0, WARNING: 1, CRITICAL: 2}

        if not self._configured:
            logger.warning(
                "Telegram not configured. Set TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID in .env to enable notifications."
            )

    def send(self, message: str, level: str = INFO) -> None:
        """Send a notification. Non-blocking — dispatches to background thread."""
        if not self._configured:
            return
        if self._level_order.get(level, 0) < self._level_order.get(self._min_level, 0):
            return
        threading.Thread(
            target=self._dispatch,
            args=(message, level),
            daemon=True,
        ).start()

    def _dispatch(self, message: str, level: str) -> None:
        try:
            import requests
            emoji = _EMOJI.get(level, '')
            ts    = datetime.now().strftime('%H:%M:%S')
            text  = f"{emoji} *[{level}]* `{self._session}` `{ts}`\n{message}"
            url   = f"https://api.telegram.org/bot{self._token}/sendMessage"
            resp  = requests.post(url, json={
                'chat_id':    self._chat_id,
                'text':       text,
                'parse_mode': 'Markdown',
            }, timeout=5)
            if not resp.ok:
                logger.warning(f"Telegram send failed: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            logger.warning(f"Telegram notification error (non-fatal): {e}")


# ─── Convenience builders ─────────────────────────────────────────────────────

def from_config(config: dict, session_name: str = 'live-paper') -> NotificationService:
    """Build from config/base.yaml notifications block."""
    nc = config.get('notifications', {})
    tg = nc.get('telegram', {})
    return NotificationService(
        bot_token    = os.environ.get(tg.get('bot_token_env', 'TELEGRAM_BOT_TOKEN'), ''),
        chat_id      = os.environ.get(tg.get('chat_id_env',   'TELEGRAM_CHAT_ID'),  ''),
        min_level    = nc.get('min_level', INFO),
        session_name = session_name,
    )
