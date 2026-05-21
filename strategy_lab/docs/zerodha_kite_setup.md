# Zerodha Kite Connect — Setup Guide

This document covers how to set up and use Kite Connect for historical OHLCV ingestion into strategy_lab.

---

## 1. Create a Kite Connect app

1. Go to [https://developers.kite.trade](https://developers.kite.trade)
2. Log in with your Zerodha account
3. Click **Create new app**
4. Set **Redirect URL** to: `http://127.0.0.1:8080`
   - This is required for the local CLI login flow
5. Note your **API Key** and **API Secret**

> **Subscription:** Kite Connect requires a paid subscription (₹2000/mo as of 2025). The historical API is included.

---

## 2. Set environment variables

Copy `.env.example` to `.env` and fill in your values:

```sh
cp .env.example .env
```

Edit `.env`:

```
KITE_API_KEY=your_api_key_here
KITE_API_SECRET=your_api_secret_here
```

Load them before running any CLI commands:

```sh
export $(grep -v '^#' .env | xargs)
```

> **Security:** `.env` and `.kite_token.json` are gitignored. Never commit them.

---

## 3. Login

```sh
python -m src.cli.main zerodha login
```

This will:
1. Print a Kite login URL — open it in your browser
2. After you approve, Kite redirects to `http://127.0.0.1:8080`
3. The CLI captures the `request_token` automatically
4. Exchanges it for an `access_token` and saves it to `.kite_token.json`

The token is valid for one trading day. Re-run `login` each morning.

If you need to force re-authentication:

```sh
python -m src.cli.main zerodha login --force
```

---

## 4. Refresh instruments cache

Download the full instruments dump (needed for resolving non-index symbols):

```sh
python -m src.cli.main zerodha instruments refresh
```

Saves to `data/instruments/instruments_<YYYY-MM-DD>.csv`.

For NIFTY and BANKNIFTY index tokens, the loader has hardcoded fallbacks so this step is optional for most use cases.

---

## 5. Fetch historical candles

```sh
# Fetch NIFTY 1-minute candles for 2024
python -m src.cli.main zerodha fetch-history \
    --symbol NIFTY \
    --from 2024-01-01 \
    --to 2024-12-31 \
    --interval minute

# Fetch BANKNIFTY
python -m src.cli.main zerodha fetch-history \
    --symbol BANKNIFTY \
    --from 2024-01-01 \
    --to 2024-12-31

# Daily candles
python -m src.cli.main zerodha fetch-history \
    --symbol NIFTY \
    --from 2020-01-01 \
    --to 2024-12-31 \
    --interval day
```

Output is saved to `data/raw/<SYMBOL>.csv` in the canonical OHLCV format used by the backtest pipeline. If the file already exists, new rows are appended and deduplicated.

> **API limit:** minute-interval requests are chunked automatically at 60-day windows to stay within Kite's historical API limits.

---

## 6. Using fetched data in backtest

The output format matches what the existing backtest pipeline expects:

```
timestamp,instrument,open,high,low,close,volume
2024-01-02 09:15:00,NIFTY,21740.5,21758.2,...
```

Run a backtest as normal:

```sh
python main.py --config config/base.yaml
```

No code changes needed in the backtest layer — the DataLoader reads from `data/raw/NIFTY.csv` regardless of whether the file was generated synthetically or from Kite.

---

## 7. Security warnings

- **Never commit** `.env`, `.kite_token.json`, or any file containing your API key/secret
- Both are in `.gitignore`; verify before every `git commit`
- Access tokens expire daily. Automate `zerodha login` via a cron/script if you need unattended operation, but store the token securely
- This integration does **not** implement order placement or live trading. It is read-only historical data only

---

## 8. Troubleshooting

| Problem | Solution |
|---|---|
| `Missing required environment variable` | Run `export $(grep -v '^#' .env \| xargs)` |
| `No access token. Run zerodha login` | Run `python -m src.cli.main zerodha login` |
| Login times out | Ensure redirect URL in Kite app is `http://127.0.0.1:8080` |
| `Zero rows returned` | Check that the instrument_token is correct and the date range has trading days |
| `Instruments CSV not found` | Run `python -m src.cli.main zerodha instruments refresh` |
