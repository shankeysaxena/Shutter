# strategy_lab — Build Roadmap

> For current deployment status, strategy portfolio, and commands, see **[PROJECT_STATUS.md](PROJECT_STATUS.md)**.  
> This file tracks the high-level phase history.

---

## Setup

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/           # fast suite (~2s)
python3 -m pytest tests/ -m ""     # all tests including slow
```

---

## Phase History

### ✅ Phases 1–4: Core Engine + Strategies
- Data pipeline, features (VWAP, OR, Gap, ATR, Intraday)
- ORB, VWAP Pullback, Gap Behavior strategies
- Backtest runtime, replay runtime, session validation
- ExperimentRunner, unified ledger, multi-leg support

### ✅ Phase 4.6/4.8: Iron Fly + Options Infrastructure
- Multi-leg simulator (ideal/realistic/pessimistic modes)
- `HistoricalOptionChainFeed` + Parquet archive schema
- Synthetic-to-archive exporter, audit CLI
- Iron Fly validation: plumbing complete, real chain data needed
- See `Iron_Fly_Strategy_Spec_v2.md`

### ✅ Phase 4.8A: Zerodha Kite Integration
- `zerodha login`, `zerodha auto-login` (TOTP), `zerodha fetch-history`
- Historical OHLCV ingestion for NIFTY/BANKNIFTY
- See `docs/zerodha_kite_setup.md`

### ✅ Phase 4.8B–4.9A: Research + Regime Allocator
- 498-session real data validation (2025 NIFTY + BANKNIFTY)
- Regime segmentation, regime tagging, counterfactual analysis
- `fast_iter_allocator`: GOOD/NEUTRAL→VWAP_PULLBACK, BAD_ORB→VWAP_REVERSION, EXHAUSTION→OR_FAILURE_FADE
- Validated: allocator beats standalone VWAP_PULLBACK by +54%
- COMPRESSION_BREAKOUT added (research-only; double-detection issue pending fix)

### ✅ Phase 5/6: Live Paper Runtime
- `LiveBarBuilder`, `PaperExecutor`, `KiteWebSocketFeed`
- `RiskEngine` (daily cap, per-strategy cap, cooldown, kill switch)
- `LivePaperRuntime` with `LiveTradeManager`
- `SessionHealthMonitor`, Telegram notifications
- Shadow portfolio comparison
- VWAP reclaim + gap filter added to OR_FAILURE_FADE
- `live-paper run` CLI (tmux + caffeinate + preflight)

---

## Current: Stage B Live-Paper (fast iteration)

See `PROJECT_STATUS.md` for the full picture.

**Active portfolio:**
- VWAP_PULLBACK (confirmed edge)
- VWAP_REVERSION (gated to BAD_ORB)
- OR_FAILURE_FADE (gated to EXHAUSTION, low confidence)

---

## Next: COMPRESSION_BREAKOUT Redesign

**Problem:** strategy re-detects compression independently from the allocator — zero trades on 2025 data.

**Fix:** allocator stores `{comp_high, comp_low, comp_started_at}` when compression is detected. Strategy reads that range and only watches for breakout — no re-detection.

Not a launch blocker. Implement after 2-4 weeks of live-paper data collection.

---

## Future: Real Capital Path

1. 10+ weeks live-paper → confirm runtime behavior matches backtest
2. Promote allocator from paper to real if 10-20 sessions match expectations
3. Start with 1 lot NIFTY, VWAP_PULLBACK only
4. Scale incrementally per strategy as each proves live-paper edge

**Iron Fly:** needs 10-12 weekly expiry cycles of real option chain data (`record-chains fetch` daily). Target: mid-2026 validation run.
