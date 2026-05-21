"""
Zerodha Kite Connect configuration.

Reads credentials from environment variables only — never from source code.
Copy .env.example to .env and fill in values; never commit .env.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Canonical paths
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
TOKEN_FILE = _PROJECT_ROOT / '.kite_token.json'
INSTRUMENTS_DIR = _PROJECT_ROOT / 'data' / 'instruments'

# Kite's minute-data historical API hard limit is 60 days per request.
KITE_MINUTE_CHUNK_DAYS = 60


@dataclass
class KiteConfig:
    api_key: str
    api_secret: str
    access_token: Optional[str] = None
    redirect_url: str = 'http://127.0.0.1:8080'


def load_config() -> KiteConfig:
    """
    Build KiteConfig from environment variables, falling back to .env file.

    Resolution order:
      1. Shell environment (already exported vars take precedence)
      2. .env file in the project root (loaded automatically — no export needed)
    """
    _load_dotenv()

    api_key = os.environ.get('KITE_API_KEY', '').strip()
    api_secret = os.environ.get('KITE_API_SECRET', '').strip()

    missing = [v for v, val in [('KITE_API_KEY', api_key), ('KITE_API_SECRET', api_secret)] if not val]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            f"Set them in .env (copy from .env.example) — no export needed."
        )

    return KiteConfig(
        api_key=api_key,
        api_secret=api_secret,
        access_token=os.environ.get('KITE_ACCESS_TOKEN', '').strip() or None,
    )


def _load_dotenv() -> None:
    """
    Read .env from the project root and populate os.environ for any key not
    already set. Silently skips if the file doesn't exist.
    Shell-exported vars always win (we never overwrite existing env entries).
    """
    env_file = _PROJECT_ROOT / '.env'
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:   # shell vars take precedence
            os.environ[key] = value
