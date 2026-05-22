"""
strategy_lab CLI.

Usage:
    # Zerodha data management
    python -m src.cli.main zerodha login
    python -m src.cli.main zerodha instruments refresh
    python -m src.cli.main zerodha fetch-history \\
        --symbol NIFTY --from 2024-01-01 --to 2024-12-31 --interval minute

    # Live paper trading (Phase 6)
    python -m src.cli.main live-paper start
    python -m src.cli.main live-paper start --config config/base.yaml --policy deterministic_no_orb
    python -m src.cli.main live-paper status

Add new command groups here as the lab grows.
"""
import argparse
import sys
from datetime import date as date_


# Map well-known index symbols to their canonical segment so the user
# doesn't need to know the Kite segment taxonomy for common cases.
_INDEX_SEGMENT_MAP = {
    'NIFTY': 'NSE-INDICES',
    'BANKNIFTY': 'NSE-INDICES',
    'NIFTY 50': 'NSE-INDICES',
    'NIFTY BANK': 'NSE-INDICES',
    'FINNIFTY': 'NSE-INDICES',
    'MIDCPNIFTY': 'NSE-INDICES',
}


def _resolve_segment(symbol: str, segment: str) -> str:
    """Auto-upgrade segment to NSE-INDICES for known index symbols."""
    return _INDEX_SEGMENT_MAP.get(symbol.upper(), segment)


def _parse_date(s: str) -> date_:
    try:
        return date_.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date {s!r}. Use YYYY-MM-DD format.")


def _cmd_zerodha_auto_login(args) -> int:
    """Automated login using stored TOTP — no browser needed."""
    from src.integrations.zerodha.auth import auto_login, load_saved_token
    from src.integrations.zerodha.config import load_config

    config = load_config()
    saved  = load_saved_token(config)
    if saved and not args.force:
        print("Saved token found. Pass --force to refresh.")
        return 0

    print("Running automated TOTP login...")
    try:
        token = auto_login(config)
        print("Auto-login successful. Token saved to .kite_token.json")
        return 0
    except Exception as e:
        print(f"ERROR: Auto-login failed — {e}")
        print("Fall back to manual: python -m src.cli.main zerodha login")
        return 1


def _cmd_zerodha_login(args) -> int:
    from src.integrations.zerodha.auth import (
        capture_token_via_localhost,
        exchange_token,
        get_login_url,
        load_saved_token,
    )
    from src.integrations.zerodha.config import load_config

    config = load_config()

    saved = load_saved_token(config)
    if saved and not args.force:
        print("Saved token found. Using it. Pass --force to re-authenticate.")
        return 0

    url = get_login_url(config)
    print(f"\nOpen this URL in your browser:\n  {url}\n")
    print("Waiting for redirect to http://127.0.0.1:8080 (timeout: 120s)...")
    request_token = capture_token_via_localhost(port=8080, timeout=120)

    if not request_token:
        print("ERROR: No request_token received. Login may have timed out or failed.")
        return 1

    exchange_token(config, request_token)
    print("Login successful. Token saved to .kite_token.json")
    return 0


def _cmd_zerodha_instruments(args) -> int:
    from src.integrations.zerodha.auth import load_saved_token
    from src.integrations.zerodha.config import load_config
    from src.integrations.zerodha.instruments import refresh_instruments

    config = load_config()
    token = load_saved_token(config)
    if not token:
        print("ERROR: No access token. Run: python -m src.cli.main zerodha login")
        return 1

    path = refresh_instruments(config, token)
    print(f"Instruments saved to {path}")
    return 0


def _cmd_zerodha_fetch_history(args) -> int:
    from src.integrations.zerodha.auth import load_saved_token
    from src.integrations.zerodha.config import load_config
    from src.integrations.zerodha.historical_loader import (
        build_kite_client, fetch_candles, save_candles,
    )
    from src.integrations.zerodha.instruments import resolve_instrument_token
    from pathlib import Path

    # Date validation
    from_date = args.from_date
    to_date = args.to_date
    if from_date > to_date:
        print(f"ERROR: --from {from_date} is after --to {to_date}")
        return 1

    symbol = args.symbol.upper()
    segment = _resolve_segment(symbol, args.segment)

    config = load_config()
    token = load_saved_token(config)
    if not token:
        print("ERROR: No access token. Run: python -m src.cli.main zerodha login")
        return 1

    instrument_token = resolve_instrument_token(symbol, segment)
    print(f"Resolved {symbol} ({segment}) → instrument_token={instrument_token}")
    print(f"Range: {from_date} → {to_date}, interval={args.interval}")

    if args.dry_run:
        from src.integrations.zerodha.historical_loader import _date_chunks, KITE_MINUTE_CHUNK_DAYS
        chunk_days = KITE_MINUTE_CHUNK_DAYS if args.interval == 'minute' else 200
        chunks = _date_chunks(from_date, to_date, chunk_days)
        print(f"Dry run — would make {len(chunks)} API request(s):")
        for i, (s, e) in enumerate(chunks, 1):
            print(f"  [{i}] {s} → {e}")
        return 0

    kite = build_kite_client(config, token)
    print("Fetching candles...")
    df = fetch_candles(kite, instrument_token, symbol, from_date, to_date, args.interval)
    print(f"Fetched {len(df)} rows")

    out_dir = Path(args.out_dir) if args.out_dir else None
    dest = save_candles(df, symbol, out_dir=out_dir)
    print(f"Saved → {dest}")
    return 0


def _cmd_record_chains_fetch(args) -> int:
    import yaml
    from datetime import date as date_
    from src.integrations.zerodha.auth import load_saved_token
    from src.integrations.zerodha.config import load_config
    from src.integrations.zerodha.historical_loader import build_kite_client
    from src.live.option_chain_recorder import OptionChainRecorder, RecorderConfig

    kite_cfg     = load_config()
    access_token = load_saved_token(kite_cfg)
    if not access_token:
        print("ERROR: No access token. Run: python -m src.cli.main zerodha login")
        return 1

    session_date = date_.fromisoformat(args.date) if args.date else date_.today()
    kite         = build_kite_client(kite_cfg, access_token)
    cfg          = RecorderConfig(
        underlyings       = [u.upper() for u in args.underlying],
        strikes_each_side = args.strikes,
        snapshot_dir      = args.out_dir,
    )
    recorder = OptionChainRecorder(kite, cfg)

    print(f"\n{'='*60}")
    print(f"  OPTION CHAIN RECORDER — Phase IF-1")
    print(f"  Session:      {session_date}")
    print(f"  Underlyings:  {cfg.underlyings}")
    print(f"  Strikes:      ATM ± {cfg.strikes_each_side}")
    print(f"  Archive:      {cfg.snapshot_dir}")
    print(f"  Purpose:      Iron Fly validation data (no trades)")
    print(f"{'='*60}\n")

    # We need spot prices to determine ATM strikes.
    # Use the underlying's close from data/raw/zerodha if available, else prompt.
    spot_at_close = {}
    for u in cfg.underlyings:
        csv = f'data/raw/zerodha/{u}.csv'
        try:
            import pandas as pd
            df = pd.read_csv(csv, parse_dates=['timestamp'])
            day_df = df[df['timestamp'].dt.date == session_date]
            if not day_df.empty:
                spot_at_close[u] = float(day_df['close'].iloc[-1])
                print(f"  {u} close (from OHLCV): {spot_at_close[u]:.2f}")
            else:
                spot_at_close[u] = float(input(f"  Enter approximate {u} close price for {session_date}: "))
        except Exception:
            spot_at_close[u] = float(input(f"  Enter approximate {u} close price for {session_date}: "))

    written = recorder.record_session(session_date, spot_at_close)
    print(f"\nRecorded sessions:")
    for u, path in written.items():
        print(f"  {u}: {path}")
    if not written:
        print("  Nothing written — check instruments cache and token availability")
    return 0 if written else 1


def _cmd_record_chains_audit(args) -> int:
    from src.live.option_chain_recorder import audit_chain_archive
    import json

    result = audit_chain_archive(args.out_dir, args.underlying)
    print(f"\n{'='*60}")
    print(f"  IRON FLY ARCHIVE AUDIT — {args.underlying}")
    print(f"{'='*60}")
    print(f"  Sessions recorded:      {result.get('sessions_recorded', 0)}")
    print(f"  Weekly expiries seen:   {result.get('weekly_expiries_seen', 0)}")
    print(f"  Date range:             {result.get('date_range', 'N/A')}")
    print(f"  Validation ready:       {'✅ YES' if result.get('validation_ready') else '❌ NO'}")
    if not result.get('validation_ready'):
        n = result.get('sessions_needed_more', 0)
        print(f"  More expiry cycles needed: {n} (≈ {n} weeks more recording)")
    print()
    return 0


def _cmd_live_paper_start(args) -> int:
    import signal as signal_mod
    import yaml
    import logging
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    # Load config
    config_path = args.config or 'config/base.yaml'
    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Resolve credentials
    from src.integrations.zerodha.config import load_config as load_kite_config
    from src.integrations.zerodha.auth import load_saved_token

    kite_cfg = load_kite_config()
    access_token = load_saved_token(kite_cfg)
    if not access_token:
        print("ERROR: No access token. Run: python -m src.cli.main zerodha login")
        return 1

    # Build strategies + allocator
    from src.backtest.experiment import _build_strategies
    from src.strategies.allocator import wrap_strategies, wrap_strategies_with_options

    policy = args.policy or 'deterministic_no_orb'
    base_strategies = _build_strategies(config)

    # Use options conversion gate when any strategy has convert_to_options: true
    options_enabled = config.get('options', {}).get('enabled', False)
    has_convert = any(
        v.get('convert_to_options', False)
        for v in config.get('strategies', {}).values()
        if isinstance(v, dict)
    )
    if options_enabled and has_convert:
        strategies = wrap_strategies_with_options(base_strategies, policy, config)
    else:
        strategies = wrap_strategies(base_strategies, policy)

    # Build live components
    from src.execution.paper_executor import PaperExecutor
    from src.live.bar_builder import LiveBarBuilder
    from src.live.kite_feed import KiteWebSocketFeed
    from src.live.risk_engine import RiskEngine
    from src.live.session_monitor import SessionHealthMonitor
    from src.runtimes.live_paper import LivePaperRuntime

    from src.live.notifications import from_config as build_notifier
    from src.live.preflight import run_preflight
    from src.integrations.zerodha.historical_loader import build_kite_client

    # Build Kite client for preflight check
    kite_client = build_kite_client(kite_cfg, access_token)

    # Preflight checks
    preflight = run_preflight(config, kite=kite_client)
    print(preflight.summary())
    if not preflight.passed:
        print("\nPreflight FAILED — session not started.")
        return 1

    notifier    = build_notifier(config, session_name=config.get('experiment_name','live-paper'))
    executor    = PaperExecutor(slippage_pct=config.get('costs', {}).get('slippage_per_side', 0) / 22000)
    bar_builder = LiveBarBuilder()
    risk_engine = RiskEngine(config)
    feed        = KiteWebSocketFeed(kite_cfg.api_key, access_token)
    monitor     = SessionHealthMonitor(stale_feed_seconds=120)

    # Wire monitor alert callback to notifier
    def _on_health_alert(alert_type: str, detail: str) -> None:
        notifier.send(f"*{alert_type}*: {detail}", WARNING)
    monitor = SessionHealthMonitor(stale_feed_seconds=120, alert_callback=_on_health_alert)

    # Wire monitor callbacks into feed
    feed.set_connect_callback(lambda *_: monitor.on_connect())
    feed.set_disconnect_callback(lambda *_: monitor.on_disconnect())

    # Build shadow evaluators with their own config (all strategies enabled)
    from src.live.shadow_evaluator import build_shadow_evaluators
    # Do NOT filter out the primary policy — a shadow of the same policy
    # against the full shadow config is the correct simulated baseline.
    # Promotion signal should be: shadow(allocator) > shadow(pullback), not > live primary.
    shadow_policies = list(args.shadow or [])
    if shadow_policies:
        shadow_cfg_path = getattr(args, 'shadow_config', None) or args.config
        with open(shadow_cfg_path) as f:
            shadow_config = yaml.safe_load(f)
        shadow_evaluators = build_shadow_evaluators(shadow_config, shadow_policies)
    else:
        shadow_evaluators = []
    if shadow_evaluators:
        shadow_cfg_label = getattr(args, 'shadow_config', None) or '(same as primary)'
        print(f"  Shadow portfolios: {[s.name for s in shadow_evaluators]}")
        print(f"  Shadow config:     {shadow_cfg_label}")
        print(f"  (observation only — simulated P&L, no orders placed by shadows)")

    # Build option chain feed when options mode is active
    option_chain_feed = None
    if options_enabled and has_convert:
        from src.backtest.experiment import _build_chain_feed
        option_chain_feed = _build_chain_feed(config)
        if option_chain_feed is not None:
            print(f"  Option chain feed: {option_chain_feed.data_origin}")

    runtime = LivePaperRuntime(
        strategies=strategies,
        executor=executor,
        risk_engine=risk_engine,
        feed=feed,
        bar_builder=bar_builder,
        config=config,
        instruments=config.get('instruments', ['NIFTY']),
        monitor=monitor,
        shadow_evaluators=shadow_evaluators,
        notifier=notifier,
        option_chain_feed=option_chain_feed,
    )
    monitor.start_background_check(interval_seconds=30)

    print(f"\n{'='*60}")
    print(f"  LIVE PAPER SESSION")
    print(f"  Policy:      {policy}")
    print(f"  Instruments: {config.get('instruments', ['NIFTY'])}")
    print(f"  Strategies:  {[s.name for s in strategies]}")
    print(f"  Executor:    PaperExecutor (no real orders)")
    print(f"{'='*60}\n")

    try:
        runtime.start()
    except Exception as e:
        notifier.send(f"🚨 *Session failed to start*\n`{e}`", 'CRITICAL')
        raise

    print("Feed connected. Press Ctrl+C to stop.\n")

    # Block until Ctrl+C
    stop_event = [False]

    def _handle_signal(signum, frame):
        stop_event[0] = True

    signal_mod.signal(signal_mod.SIGINT,  _handle_signal)
    signal_mod.signal(signal_mod.SIGTERM, _handle_signal)

    import time
    from datetime import datetime, time as time_
    _AUTO_SHUTDOWN_AT = time_(15, 31)   # hard fallback if last bar never arrives

    from datetime import timedelta
    _MARKET_OPEN   = time_(9, 15)
    _MAX_RETRIES      = 8          # reconnect attempts before giving up
    _RETRY_BASE_SEC   = 10         # first retry after 10s
    _RETRY_MULTIPLIER = 2          # exponential: 10, 20, 40, 80s
    _RETRY_CAP_SEC    = 120        # never wait more than 2 min between retries
    _reconnect_count  = [0]
    try:
      while not stop_event[0] and runtime.is_running:
        time.sleep(1)
        now = datetime.now()

        # Shutdown as soon as EOD flush is done (last bar processed + summary sent)
        if runtime.eod_complete:
            print("\nEOD flush complete — session closing.")
            break

        # Hard wall-clock fallback at 15:31 (handles missing last bar)
        if now.time() >= _AUTO_SHUTDOWN_AT and not runtime._auto_shutdown:
            print("\n15:31 reached — auto shutdown...")
            runtime.trigger_eod_shutdown()
            break

        # Stale-feed handler: no bars during market hours
        last_bar = runtime._last_bar_time
        if (last_bar is not None
                and _MARKET_OPEN <= now.time() <= _AUTO_SHUTDOWN_AT):
            stale_mins = (now - last_bar.replace(tzinfo=None)
                          if last_bar.tzinfo else now - last_bar).total_seconds() / 60

            half_window = runtime._max_stale_minutes / 2

            # Auto-reconnect on silent TCP hang (disconnected=False but no data)
            # Tries up to _MAX_RETRIES times with increasing delays before giving up.
            if (stale_mins >= half_window
                    and not monitor._disconnected
                    and hasattr(feed, 'reconnect')
                    and _reconnect_count[0] < _MAX_RETRIES):

                retry_n   = _reconnect_count[0] + 1
                # Exponential backoff: 10s, 20s, 40s, 80s (capped at 120s)
                delay_s   = min(_RETRY_BASE_SEC * (_RETRY_MULTIPLIER ** (retry_n - 1)),
                                _RETRY_CAP_SEC)
                _reconnect_count[0] = retry_n

                msg = (f"Silent TCP hang ({stale_mins:.0f} min). "
                       f"Auto-reconnect attempt {retry_n}/{_MAX_RETRIES}...")
                print(f"\n[WARNING] {msg}")
                notifier.send(msg, 'WARNING')
                try:
                    feed.reconnect()
                    monitor.on_connect()
                    _reconnect_count[0] = 0   # success — reset counter
                    print(f"[INFO] Reconnect successful.")
                    notifier.send(
                        f"✅ Feed reconnected (attempt {retry_n}). Resuming.", 'INFO'
                    )
                except Exception as e:
                    print(f"[WARNING] Reconnect attempt {retry_n} failed: {e}. "
                          f"Retrying in {delay_s}s...")
                    notifier.send(
                        f"Reconnect attempt {retry_n} failed: {e}. "
                        f"Next retry in {delay_s}s.", 'WARNING'
                    )
                    time.sleep(delay_s)

            # All retries exhausted — force EOD shutdown
            elif (stale_mins >= runtime._max_stale_minutes
                    and _reconnect_count[0] >= _MAX_RETRIES):
                msg = (f"No bars for {stale_mins:.0f} min. "
                       f"All {_MAX_RETRIES} reconnect attempts failed. "
                       f"Force-closing trades and stopping.")
                print(f"\n[CRITICAL] {msg}")
                notifier.send(msg, 'CRITICAL')
                runtime.trigger_eod_shutdown()
                break

        if not monitor.is_healthy():
            print(f"[WARNING] Feed unhealthy: {monitor.health_summary()}")

    except Exception as e:
        import traceback
        err = traceback.format_exc()
        notifier.send(f"🚨 *Session crashed*\n`{e}`", 'CRITICAL')
        print(f"\n[CRITICAL] Session crashed: {e}\n{err}")
        runtime.trigger_eod_shutdown()

    print("\nShutting down...")
    try:
        runtime.stop()
    except Exception as e:
        notifier.send(f"🚨 *Session crashed on shutdown*\n`{e}`", 'CRITICAL')
    monitor.stop()

    # Print session summary
    positions = runtime.get_positions()
    risk      = runtime.get_risk_state()
    print(f"\n{'='*60}")
    print(f"  SESSION SUMMARY")
    print(f"  Total fills:    {risk.total_fills}")
    print(f"  Session P&L:    ₹{risk.session_net_pnl:,.2f}")
    print(f"  Open positions: {risk.open_positions}")
    print(f"  Halted:         {risk.halted}")
    if positions:
        print(f"  Final positions: {positions}")

    # Shadow portfolio comparison
    if shadow_evaluators:
        print(f"\n{'='*60}")
        print(f"  SHADOW PORTFOLIO COMPARISON")
        print(f"{'='*60}")
        print(f"  {'Policy':<26} {'Trades':>7} {'P&L':>14} {'WR':>7}  note")
        print(f"  {'-'*65}")
        print(f"  {'[LIVE] '+policy:<26} {'—':>7} {'₹'+str(round(risk.session_net_pnl,2)):>14} {'—':>7}  live fills")
        for shadow in shadow_evaluators:
            s = shadow.session_summary()
            wr  = f"{s['win_rate']:.0%}" if s['win_rate'] is not None else '—'
            pnl = f"₹{s['total_pnl']:,.0f}"
            print(f"  {'[SHADOW] '+shadow.name:<26} {s['n_trades']:>7} {pnl:>14} {wr:>7}  simulated")

    print(f"{'='*60}\n")
    return 0


def _cmd_live_paper_weekly_report(args) -> int:
    from src.analytics.weekly_report import weekly_report, print_weekly_report
    import json
    from pathlib import Path

    report = weekly_report(
        report_dir=args.run_dir,
        days=args.days,
    )
    print_weekly_report(report)

    if args.save:
        out = Path(args.run_dir) / 'weekly_reports'
        out.mkdir(parents=True, exist_ok=True)
        from datetime import date
        path = out / f"week_{date.today()}.json"
        path.write_text(json.dumps(report, indent=2, default=str))
        print(f"Report saved → {path}")
    return 0


def _cmd_live_paper_run(args) -> int:
    """
    Single command: login check → tmux session → caffeinate → live-paper start.

    If already inside a tmux session, runs directly (no nesting).
    Requires tmux (brew install tmux) and AC power for caffeinate to hold sleep.
    """
    import os, shutil, subprocess, sys

    # --- Prerequisites ---
    if not shutil.which('tmux'):
        print("ERROR: tmux not found.")
        print("Install: brew install tmux")
        print("Then re-run this command.")
        return 1

    if not shutil.which('caffeinate'):
        print("WARNING: caffeinate not found (not macOS?). Sleep prevention disabled.")
        caffeinate_prefix = ''
    else:
        caffeinate_prefix = 'caffeinate -dims '

    # --- Token check ---
    from src.integrations.zerodha.auth import load_saved_token
    from src.integrations.zerodha.config import load_config
    try:
        kite_cfg = load_config()
        token    = load_saved_token(kite_cfg)
        if not token:
            print("ERROR: No Kite access token.")
            print("Run first:  python3 -m src.cli.main zerodha login")
            return 1
    except Exception as e:
        print(f"ERROR loading credentials: {e}")
        return 1

    # --- If already in tmux, just start directly ---
    if os.environ.get('TMUX'):
        print("Already inside tmux — starting live-paper directly.")
        return _cmd_live_paper_start(args)

    # --- Build the inner start command ---
    session_name = 'live-paper'
    inner = (
        f"python3 -m src.cli.main live-paper start"
        f" --config {args.config}"
        f" --policy {args.policy}"
    )
    if getattr(args, 'shadow_config', None):
        inner += f" --shadow-config {args.shadow_config}"
    if args.shadow:
        inner += f" --shadow {' '.join(args.shadow)}"

    full_cmd = f"{caffeinate_prefix}{inner}"

    # Kill any existing live-paper tmux session cleanly
    subprocess.run(['tmux', 'kill-session', '-t', session_name],
                    capture_output=True)

    # Create new detached tmux session and run the command inside it
    result = subprocess.run(
        ['tmux', 'new-session', '-d', '-s', session_name, full_cmd],
    )
    if result.returncode != 0:
        print("ERROR: Failed to create tmux session.")
        return 1

    print(f"\n{'='*55}")
    print(f"  Live-paper session started in tmux + caffeinate")
    print(f"  Session name: {session_name}")
    print(f"{'='*55}")
    print(f"\n  Attach (watch logs):  tmux attach -t {session_name}")
    print(f"  Detach (keep running): Ctrl+B then D")
    print(f"  Kill session:          tmux kill-session -t {session_name}")
    print(f"\n  ⚡ Keep laptop plugged in — caffeinate requires AC power")
    print(f"  📱 Telegram alerts are active\n")
    return 0


def _cmd_live_paper_status(args) -> int:
    """Show today's session report and recent alerts."""
    import json
    from pathlib import Path
    from datetime import date

    report_path = Path('runs') / 'live_paper' / f"{date.today()}.json"
    if not report_path.exists():
        print("No session report found for today.")
        print("Either the session hasn't started or hasn't ended yet.")
        print("\nTo see live logs, check the terminal running live-paper start.")
        print("Health alerts are sent to your Telegram automatically.")
        return 0

    data = json.loads(report_path.read_text())
    print(f"\n{'='*55}")
    print(f"  TODAY'S SESSION — {data.get('session_date')}")
    print(f"{'='*55}")
    print(f"  Trades:      {data.get('total_trades', 0)}")
    print(f"  Net P&L:     ₹{data.get('total_net_pnl', 0):,.2f}")
    wr_str = f"{data['win_rate']:.0%}" if data.get('win_rate') else '—'
    print(f"  Win rate:    {wr_str}")
    print(f"  Halted:      {data.get('halted', False)}")
    if data.get('halt_reason'):
        print(f"  Halt reason: {data['halt_reason']}")
    if data.get('trades'):
        print(f"\n  Trades:")
        for t in data['trades']:
            pnl = f"₹{t['net_pnl']:+,.0f}" if t.get('net_pnl') else '—'
            print(f"    {t['strategy']:<20} {t['direction']:<6} "
                  f"{t.get('exit_reason','OPEN'):<8} {pnl}")
    print(f"{'='*55}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m src.cli.main',
        description='strategy_lab command-line tools',
    )
    sub = parser.add_subparsers(dest='group')

    # ---- zerodha group ----
    z = sub.add_parser('zerodha', help='Zerodha Kite Connect commands')
    z_sub = z.add_subparsers(dest='command')

    # zerodha login (manual browser flow)
    login_p = z_sub.add_parser('login', help='Authenticate with Kite via browser (manual)')
    login_p.add_argument('--force', action='store_true', help='Re-authenticate even if token exists')
    login_p.set_defaults(func=_cmd_zerodha_login)

    # zerodha auto-login (TOTP, no browser)
    al_p = z_sub.add_parser('auto-login',
                              help='Authenticate automatically using stored TOTP (no browser)')
    al_p.add_argument('--force', action='store_true', help='Refresh even if token exists')
    al_p.set_defaults(func=_cmd_zerodha_auto_login)

    # zerodha instruments refresh
    inst_p = z_sub.add_parser('instruments', help='Manage instruments cache')
    inst_sub = inst_p.add_subparsers(dest='inst_command')
    ref_p = inst_sub.add_parser('refresh', help='Download and cache instruments dump')
    ref_p.set_defaults(func=_cmd_zerodha_instruments)

    # zerodha fetch-history
    fh_p = z_sub.add_parser('fetch-history', help='Download historical OHLCV candles')
    fh_p.add_argument('--symbol', required=True, help='Symbol, e.g. NIFTY, BANKNIFTY, RELIANCE')
    fh_p.add_argument('--from', required=True, dest='from_date', type=_parse_date, metavar='YYYY-MM-DD')
    fh_p.add_argument('--to', required=True, dest='to_date', type=_parse_date, metavar='YYYY-MM-DD')
    fh_p.add_argument('--interval', default='minute',
                       choices=['minute', '3minute', '5minute', '10minute',
                                '15minute', '30minute', '60minute', 'day'])
    fh_p.add_argument('--segment', default='NSE',
                       help='Exchange segment (default: NSE; auto-upgraded to NSE-INDICES for NIFTY/BANKNIFTY)')
    fh_p.add_argument('--out-dir', default=None, metavar='DIR',
                       help='Output directory (default: data/raw/)')
    fh_p.add_argument('--dry-run', action='store_true',
                       help='Show what would be fetched without making API calls')
    fh_p.set_defaults(func=_cmd_zerodha_fetch_history)

    # ---- record-chains group ----
    rc = sub.add_parser('record-chains', help='Iron Fly validation data recorder (Phase IF-1)')
    rc_sub = rc.add_subparsers(dest='rc_command')

    # record-chains fetch
    rf_p = rc_sub.add_parser('fetch', help='Fetch and archive option chain candles for one session')
    rf_p.add_argument('--date', default=None, metavar='YYYY-MM-DD',
                       help='Session date to fetch (default: today)')
    rf_p.add_argument('--underlying', nargs='+', default=['NIFTY', 'BANKNIFTY'])
    rf_p.add_argument('--strikes', type=int, default=10,
                       help='Strikes each side of ATM (default: 10)')
    rf_p.add_argument('--out-dir', default='data/option_chain_snapshots')
    rf_p.set_defaults(func=_cmd_record_chains_fetch)

    # record-chains audit
    ra_p = rc_sub.add_parser('audit', help='Check archive coverage and validation readiness')
    ra_p.add_argument('--underlying', default='NIFTY')
    ra_p.add_argument('--out-dir', default='data/option_chain_snapshots')
    ra_p.set_defaults(func=_cmd_record_chains_audit)

    # ---- live-paper group ----
    lp = sub.add_parser('live-paper', help='Live paper trading session (Phase 6)')
    lp_sub = lp.add_subparsers(dest='lp_command')

    # live-paper run (recommended: tmux + caffeinate + start in one command)
    run_p = lp_sub.add_parser('run',
        help='Start in tmux + caffeinate (recommended — lid-safe, single command)')
    run_p.add_argument('--config', default='config/live_paper_stage_a.yaml', metavar='PATH')
    run_p.add_argument('--policy', default='vwap_pullback_only',
                        choices=['all_on', 'vwap_pullback_only', 'conservative',
                                 'deterministic', 'deterministic_no_orb',
                                 'fast_iter_allocator'])
    run_p.add_argument('--shadow', nargs='*', default=[], metavar='POLICY')
    run_p.add_argument('--shadow-config', default=None, dest='shadow_config', metavar='PATH')
    run_p.set_defaults(func=_cmd_live_paper_run)

    # live-paper start
    start_p = lp_sub.add_parser('start', help='Start a live paper session (foreground)')
    start_p.add_argument(
        '--config', default='config/base.yaml', metavar='PATH',
        help='Config file (default: config/base.yaml)',
    )
    start_p.add_argument(
        '--policy', default='deterministic_no_orb',
        choices=['all_on', 'vwap_pullback_only', 'conservative',
                 'deterministic', 'deterministic_no_orb', 'fast_iter_allocator'],
        help='Primary executing policy (default: deterministic_no_orb)',
    )
    start_p.add_argument(
        '--shadow', nargs='*', default=[],
        metavar='POLICY',
        help='Shadow evaluation policies (space-separated, no orders placed). '
             'Example: --shadow deterministic_no_orb all_on',
    )
    start_p.add_argument(
        '--shadow-config', default=None, dest='shadow_config', metavar='PATH',
        help='Config for shadow portfolios (default: same as --config). '
             'Use config/shadow_allocator_validation.yaml to enable all strategies. '
             'Example: --shadow-config config/shadow_allocator_validation.yaml',
    )
    start_p.set_defaults(func=_cmd_live_paper_start)

    # live-paper status
    status_p = lp_sub.add_parser('status', help='Check session health')
    status_p.set_defaults(func=_cmd_live_paper_status)

    # live-paper weekly-report
    wr_p = lp_sub.add_parser('weekly-report', help='Weekly strategy performance report')
    wr_p.add_argument('--run-dir', default='runs/live_paper',
                       help='Directory containing daily JSON reports')
    wr_p.add_argument('--days', type=int, default=7,
                       help='Number of calendar days to include (default: 7)')
    wr_p.add_argument('--save', action='store_true',
                       help='Save report JSON to runs/live_paper/weekly_reports/')
    wr_p.set_defaults(func=_cmd_live_paper_weekly_report)

    return parser


def main(argv=None):
    # Load .env before anything else so ALL credentials are available
    # (Kite, Telegram, TOTP) regardless of which command runs.
    from src.integrations.zerodha.config import _load_dotenv
    _load_dotenv()

    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, 'func'):
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
