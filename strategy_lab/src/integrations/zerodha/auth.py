"""
Zerodha Kite Connect authentication.

Two login modes:

  Manual (browser-based):
    1. Generate login URL → user visits in browser
    2. Browser redirects to http://127.0.0.1:8080?request_token=<token>
    3. Exchange for access_token → saved to .kite_token.json

  Automated (TOTP, no browser):
    1. POST credentials to Kite login API
    2. Generate TOTP from stored secret (pyotp) → POST to 2FA endpoint
    3. Extract request_token from redirect → exchange for access_token
    Required env vars: KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET

Token file format:
  {"access_token": "...", "api_key": "...", "generated_at": "YYYY-MM-DDTHH:MM:SS"}
"""
import hashlib
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from src.integrations.zerodha.config import TOKEN_FILE, KiteConfig


def _get_kite_connect():
    try:
        from kiteconnect import KiteConnect
        return KiteConnect
    except ImportError as e:
        raise ImportError(
            "kiteconnect is required for Zerodha integration. "
            "Install with: pip install kiteconnect  "
            "or: pip install -r requirements.txt"
        ) from e


def get_login_url(config: KiteConfig) -> str:
    """Return the Kite login URL. User must open this in a browser."""
    kite = _get_kite_connect()(api_key=config.api_key)
    return kite.login_url()


def exchange_token(config: KiteConfig, request_token: str) -> str:
    """
    Exchange request_token for access_token using SHA-256 checksum.
    Saves the token to TOKEN_FILE and returns the access_token string.
    """
    kite = _get_kite_connect()(api_key=config.api_key)
    session_data = kite.generate_session(request_token, api_secret=config.api_secret)
    access_token = session_data['access_token']
    _save_token(config.api_key, access_token)
    return access_token


def load_saved_token(config: KiteConfig) -> Optional[str]:
    """
    Return a saved access_token if TOKEN_FILE exists and belongs to the
    current api_key. Returns None if file missing or key mismatch.
    """
    if not TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(TOKEN_FILE.read_text())
        if data.get('api_key') != config.api_key:
            return None
        return data.get('access_token')
    except (json.JSONDecodeError, OSError):
        return None


def capture_token_via_localhost(port: int = 8080, timeout: int = 120) -> Optional[str]:
    """
    Spin up a one-shot HTTP server on localhost:{port} that captures the
    request_token from Kite's redirect URL and shuts down.
    Returns the request_token string or None if the request doesn't arrive.
    """
    captured = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            token = qs.get('request_token', [None])[0]
            status = qs.get('status', [''])[0]
            if token and status == 'success':
                captured['request_token'] = token
                self._respond(200, b'Login successful. You can close this tab.')
            else:
                self._respond(400, b'Login failed or cancelled.')

        def _respond(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass   # silence access log

    server = HTTPServer(('127.0.0.1', port), _Handler)
    server.timeout = timeout
    server.handle_request()
    server.server_close()
    return captured.get('request_token')


def auto_login(config) -> str:
    """
    Fully automated login using stored credentials + TOTP.
    No browser required. Returns the access_token.

    Required env vars (in .env):
      KITE_USER_ID       — Zerodha client ID (e.g. AB1234)
      KITE_PASSWORD      — Zerodha login password
      KITE_TOTP_SECRET   — TOTP secret from 2FA setup (base32 string)

    The TOTP secret is the string shown when you first enable 2FA in Zerodha.
    It looks like: JBSWY3DPEHPK3PXP
    If you only have the QR code, scan it with any TOTP app and export the secret.
    """
    import os, requests, pyotp
    from urllib.parse import urlparse, parse_qs

    user_id     = os.environ.get('KITE_USER_ID', '').strip()
    password    = os.environ.get('KITE_PASSWORD', '').strip()
    totp_secret = os.environ.get('KITE_TOTP_SECRET', '').strip()

    missing = [v for v, val in [
        ('KITE_USER_ID', user_id), ('KITE_PASSWORD', password),
        ('KITE_TOTP_SECRET', totp_secret),
    ] if not val]
    if missing:
        raise EnvironmentError(
            f"Auto-login requires: {', '.join(missing)}. Add them to .env"
        )

    sess = requests.Session()

    # Step 1: POST credentials → get request_id for 2FA
    r1 = sess.post(
        'https://kite.zerodha.com/api/login',
        data={'user_id': user_id, 'password': password},
        timeout=10,
    )
    if not r1.ok:
        raise RuntimeError(f"Login step 1 failed: {r1.status_code} {r1.text[:200]}")
    data1 = r1.json()
    if data1.get('status') != 'success':
        raise RuntimeError(f"Login step 1 rejected: {data1.get('message', data1)}")
    request_id = data1['data']['request_id']

    # Step 2: Generate TOTP and POST to 2FA endpoint
    totp_code = pyotp.TOTP(totp_secret).now()
    r2 = sess.post(
        'https://kite.zerodha.com/api/twofa',
        data={
            'user_id':    user_id,
            'request_id': request_id,
            'twofa_value': totp_code,
            'twofa_type': 'totp',
            'skip_session': True,
        },
        timeout=10,
        allow_redirects=False,
    )
    if not r2.ok and r2.status_code not in (302, 303):
        raise RuntimeError(f"2FA step failed: {r2.status_code} {r2.text[:200]}")
    data2 = r2.json() if r2.headers.get('content-type', '').startswith('application/json') else {}
    if data2.get('status') == 'error':
        raise RuntimeError(f"2FA rejected: {data2.get('message', data2)}")

    # Step 3: Follow redirect to capture request_token from the redirect URL
    login_url   = _get_kite_connect()(api_key=config.api_key).login_url()
    r3 = sess.get(login_url, timeout=10, allow_redirects=True)
    final_url   = r3.url
    params      = parse_qs(urlparse(final_url).query)
    request_token = params.get('request_token', [None])[0]

    if not request_token:
        raise RuntimeError(
            f"Could not extract request_token from redirect. "
            f"Final URL: {final_url[:200]}"
        )

    access_token = exchange_token(config, request_token)
    return access_token


def _save_token(api_key: str, access_token: str) -> None:
    payload = {
        'api_key': api_key,
        'access_token': access_token,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
    }
    TOKEN_FILE.write_text(json.dumps(payload, indent=2))
