# Strategy Lab — Project Status

> Living document. Updated at each major milestone.  
> Last updated: 2026-05-20

---

## Current Phase: Stage B Live-Paper (Fast Iteration)

**What's running:**
```sh
python3 -m src.cli.main zerodha login
python3 -m src.cli.main live-paper run \
    --config config/live_paper_fast_iter.yaml \
    --policy fast_iter_allocator \
    --shadow-config config/shadow_allocator_validation.yaml \
    --shadow vwap_pullback_only deterministic_no_orb fast_iter_allocator all_on
```

**Weekly review:**
```sh
python3 -m src.cli.main live-paper weekly-report --run-dir runs/live_paper
```

---

## Strategy Portfolio Status

| Strategy | Status | Regime Gate | Backtest P&L (2025 NIFTY) | Notes |
|---|---|---|---|---|
| VWAP_PULLBACK | ✅ Active — anchor | All regimes | +₹36,002 (PF 1.38) | Only confirmed edge |
| VWAP_REVERSION | ✅ Active — secondary | BAD_ORB only | -₹30k ungated → +₹8k gated | Direction right, R:R needs live calibration |
| OR_FAILURE_FADE | ⚠️ Active — low confidence | EXHAUSTION only | -₹8k (504→37 trades after filters) | Gap filter + VWAP reclaim added; still weak |
| COMPRESSION_BREAKOUT | 🔬 Research shadow | LOW_VOL_COMP only | 0 trades (structural issue) | Needs architectural redesign — see Known Issues |
| ORB | ❌ Disabled | — | -₹102k (2025) | Structurally weak; needs full-session range info to gate correctly |
| GAP_BEHAVIOR | ❌ Disabled | — | No confirmed edge | |
| IRON_FLY | ❌ Disabled | — | Not validated on real chain data | Needs 10-12 expiry cycles of real option data |

---

## Architecture Overview

```
Live market (Kite WebSocket)
    ↓
KiteWebSocketFeed → LiveBarBuilder (1-min bars)
    ↓
MarketStateDetector
    ├─ Base regime: GOOD_ORB / NEUTRAL / BAD_ORB / CHOPPY_BAD / EXHAUSTION
    └─ Additive: compression_detected = True (mid-session, after 10:00)
    ↓
StrategyEligibilityPolicy (fast_iter_allocator)
    ↓ gates strategies by regime
AllocationGatedStrategy wrappers
    ↓
RiskEngine (daily cap / per-strategy cap / cooldown / kill switch)
    ↓
PaperExecutor (no real orders)
    ↓
LiveTradeManager (entry fill → stop/target → exit → P&L)
    ↓
Telegram notifications + daily JSON report
```

---

## Key Configs

| Config | Purpose |
|---|---|
| `config/live_paper_fast_iter.yaml` | Stage B live-paper (current) |
| `config/live_paper_stage_a.yaml` | Stage A (single strategy, strict limits) |
| `config/shadow_allocator_validation.yaml` | Shadow portfolios (all strategies enabled) |
| `config/base.yaml` | Backtest/research default |

---

## Risk Engine (live config)

```yaml
daily_loss_cap: -5000          # session halts
per_strategy_loss_cap: -3000
max_open_positions: 2
max_trades_per_session: 3
cooldown_after_losses: 3       # 30-min pause
kill_switch_losses: 10         # permanent session disable
```

---

## Allocation Logic

```
GOOD_ORB / NEUTRAL  →  VWAP_PULLBACK
BAD_ORB             →  VWAP_PULLBACK + VWAP_REVERSION
CHOPPY_BAD          →  VWAP_PULLBACK only
EXHAUSTION          →  VWAP_PULLBACK + OR_FAILURE_FADE
LOW_VOL_COMP        →  VWAP_PULLBACK + COMPRESSION_BREAKOUT (research only)
```

Compression is ADDITIVE — it doesn't replace the base regime. A BAD_ORB day that coils mid-session gets VWAP_REVERSION AND COMPRESSION_BREAKOUT simultaneously.

---

## Validated Findings (2025 real data)

1. **Regime complementarity confirmed**: strategies are anti-correlated. VWAP_PULLBACK wins on BAD_ORB; ORB wins on NEUTRAL/GOOD_ORB.
2. **ORB structurally weak**: loses on 84% of sessions (BAD_ORB dominant in 2025). Needs full-session range data to gate correctly — unavailable at 09:30 decision time.
3. **VWAP Reversion has correct direction** (58-59% win rate) but wrong geometry (avg loss 1.8× avg win). R:R fix pending live calibration.
4. **OR_FAILURE_FADE**: dramatically over-traded without filters (504→37 trades after gap filter + VWAP reclaim). Still low-confidence.
5. **Allocator architecture validated**: deterministic_no_orb beats VWAP_PULLBACK alone by +54% (+₹9,534) on 2025 NIFTY.

---

## Known Issues / Next Tasks

### COMPRESSION_BREAKOUT — architectural redesign needed
**Problem:** double-detection (allocator AND strategy both check compression independently) + VWAP drift in volatile sessions = zero trades.

**Root cause:**
- Allocator detects compression (20-bar window, range < 1.5 ATR) → `compression_detected = True`
- Strategy then independently checks compression on a FRESH 5-bar window (range < 1.0 ATR) → almost always fails because:
  1. Strategy's window starts AFTER allocator grants access (post-compression bars)
  2. Session VWAP is dragged by opening volatility; `|close - VWAP| < 0.5%` fails even when market is calm

**Fix (next iteration):**
```
Allocator: detect compression → store {comp_high, comp_low, comp_started_at}
Strategy: skip own detection → watch breakout from allocator's stored range only
```

### OR_FAILURE_FADE — still weak
30% win rate on 37 remaining trades. Gap filter + VWAP reclaim helped (504→37) but quality still poor. Status: tightly capped research data collection.

### Iron Fly — needs real option-chain data
Build via `record-chains fetch` CLI after each session. Needs 10-12 expiry cycles (~3 months of recording) before validation run. See `docs/zerodha_kite_setup.md`.

---

## Data

| Dataset | Location | Sessions | Coverage |
|---|---|---|---|
| 3-day sample | `data/raw/zerodha/3day/` | 3 | May 14-18, 2026 |
| 2.5-month sample | `data/raw/zerodha/2_5month/` | 50 | Mar-May 2026 |
| Full 2025 | `data/raw/zerodha/full_2025/` | 249 | Jan-Dec 2025 (NIFTY + BANKNIFTY) |
| Synthetic (old) | `data/raw/synthetic/` | 3 | Jan 2024 only |
| Iron Fly archive | `data/option_chain_snapshots/` | 0 | Recording in progress |

---

## Key CLI Commands

```sh
# Daily startup
python3 -m src.cli.main zerodha login
python3 -m src.cli.main live-paper run --config config/live_paper_fast_iter.yaml \
    --policy fast_iter_allocator \
    --shadow-config config/shadow_allocator_validation.yaml \
    --shadow vwap_pullback_only deterministic_no_orb fast_iter_allocator all_on

# Session monitoring
tmux attach -t live-paper           # watch live logs
python3 -m src.cli.main live-paper status   # today's summary

# Weekly review
python3 -m src.cli.main live-paper weekly-report --run-dir runs/live_paper --save

# Fetch more data
python3 -m src.cli.main zerodha fetch-history --symbol NIFTY --from 2026-01-01 --to 2026-05-20

# Option chain recording (post-session)
python3 -m src.cli.main record-chains fetch --date 2026-05-20

# Backtest
python3 main.py config/live_paper_fast_iter.yaml data/raw/zerodha/full_2025

# Analysis pipeline
python3 tools/run_segmentation.py --run-dir runs/<RUN> --data-dir data/raw/zerodha/full_2025
python3 tools/run_regime_analysis.py --run-dir runs/<RUN> --data-dir data/raw/zerodha/full_2025
python3 tools/run_counterfactual.py --run-dir runs/<RUN> --data-dir data/raw/zerodha/full_2025
python3 tools/run_allocation_simulation.py --run-dir runs/<RUN> --data-dir data/raw/zerodha/full_2025
```

---

## Promotion / Demotion Rules

| Metric | Threshold | Action |
|---|---|---|
| PF > 1.1, 10+ trades | Week | Keep — increase confidence |
| PF 0.9-1.1 | Any | Observe — continue |
| PF < 0.8, 10+ trades | Week | Disable candidate |
| Kill switch hit (10 losses) | Session | Auto-disabled; review before re-enabling |
| < 5 trades/week | — | Insufficient data |

---

## Important Files

| File | Purpose |
|---|---|
| `Iron_Fly_Strategy_Design.md` | Iron Fly design rationale |
| `Iron_Fly_Strategy_Spec_v2.md` | Iron Fly full implementation spec |
| `docs/zerodha_kite_setup.md` | Kite Connect setup guide |
| `config/live_paper_fast_iter.yaml` | Current live-paper config |
| `config/shadow_allocator_validation.yaml` | Shadow portfolio config |
| `.env.example` | Credentials template (copy to `.env`) |
