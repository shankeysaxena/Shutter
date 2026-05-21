# Intraday Anchored Iron Fly — Strategy Design Document

**Status:** Design / pre-spec
**Underlying:** NIFTY, BANKNIFTY (index options, weekly expiry)
**Style:** Non-directional, short premium, intraday
**Lab phase:** Targeted for Phase 4.6 (new) — requires option-chain infrastructure before implementation

---

## 1. What we are building

An intraday, defined-risk, non-directional options strategy that **sells time** on NIFTY / BANKNIFTY weekly options when the day's tape looks structurally range-bound, and exits on three independent signals: short-strike touch, vol expansion, or failure-to-decay within a time window.

The structure on each entry is a single **Iron Fly**:

```
                       Long Call (ATM + W)
                      /
   Short ATM Straddle
                      \
                       Long Put  (ATM - W)
```

- Sell 1× ATM Call, 1× ATM Put (the "body" — collects premium)
- Buy 1× OTM Call at ATM + W, 1× OTM Put at ATM − W (the "wings" — cap max loss)
- All four legs same expiry (weekly, 0–2 DTE)

Max loss is bounded at entry by W (wing width). Max profit is the net credit, realized only at expiry if spot pins ATM — but we never hold to expiry; we close intraday on the first exit signal that fires.

## 2. How we are building it

### 2.1 Entry conditions (all must pass)

| Filter | Purpose | Default |
|---|---|---|
| Underlying ∈ {NIFTY, BANKNIFTY} | Only liquid index options | — |
| DTE ∈ {0, 1, 2} | Theta is concentrated in the final days | — |
| Time-of-day window | Need OR to settle; need decay runway before close | 09:45–13:30 IST |
| Trend filter: \|spot − VWAP\| / spot | Non-directional strategies die in trending tape | < 0.25% |
| Range filter: OR width vs N-day median | Tight OR → contained day prior | OR < 1.0× median(N=20) |
| IV regime filter: ATM IV percentile | Avoid vol-crush days (no premium) and pre-event spikes (vega risk) | 25th–75th pct (trailing 60d) |
| Liquidity: ATM bid-ask spread | Slippage will eat the edge | < configured tolerance |

### 2.2 Position construction

- **Wing width W**: sized so that `max_loss_per_lot ≤ per_trade_risk_budget`. This is the only sizing knob.
- **Lots**: derived from `(capital × risk_per_trade_pct) / max_loss_per_lot`, rounded down.
- **Entry timing**: at the next bar after all filters pass; market orders on wings, limit orders on shorts (cross conservatively).

### 2.3 Exit conditions (layered, ordered by priority)

1. **Touch exit** — spot touches either short strike → flatten immediately. Don't let gamma compound.
2. **No-progress time exit** —
   - At T+45min: require ≥ 10% of max profit captured, else exit.
   - At T+90min: require ≥ 25% of max profit captured, else exit.
   This is the novel piece. Retail short-premium most often dies by *holding through a flat tape hoping for recovery*. We refuse to.
3. **Profit target** — exit at 40–50% of max profit. Don't grind for the last rupees of theta.
4. **Vol-expansion exit** — if the combined re-priced premium of the short legs ≥ 1.3× entry while spot has moved < 0.5×W, vega is moving against us → exit.
5. **Hard time stop** — flatten by 15:15 regardless of state.

### 2.4 New infrastructure required

| Component | Why |
|---|---|
| Option chain snapshot feed | Strikes, bid/ask, IV — required, no workaround |
| Multi-leg position model | 4 legs as one transactional unit (single P&L, single exit decision) |
| IV regime feature | ATM IV with trailing percentile band |
| Leg-level + structure-level trade ledger | Diagnostics on which leg drove the P&L outcome |

Reusable as-is from current lab: `IntradaySession`, `VWAP`, `OpeningRange`, `RejectionReason`, `BarEngine` reset hooks.

## 3. How it generates profit

Three independent profit drivers, in order of contribution:

### 3.1 Theta (the primary edge)

Index options near expiry decay non-linearly. Sold ATM premium loses ~30–50% of its extrinsic value in the final two days even with spot pinned. The strategy is structured to harvest this decay over a 1–3 hour holding window, not to expiration.

**Mechanism:** As long as spot stays within the breakeven band (ATM ± net premium), the position's mark-to-market value declines purely as a function of time. Closing at 40–50% of max profit means we typically realize this in the first half of the holding window, well before the tape has a chance to introduce gamma risk.

### 3.2 Implied volatility mean reversion (secondary)

The IV regime filter selects entries where ATM IV is in the middle of its trailing percentile band. From there, IV is more likely to revert (or drift flat) than to expand. When IV drifts down post-entry, the short legs cheapen faster than the long wings — the structure profits even without theta.

### 3.3 Negative skew of held losers (cut by the no-progress exit)

The no-progress exit converts the typical "high win-rate, fat-tail loss" profile of short premium into something closer to "high win-rate, *capped* loss." Days where the structure isn't decaying as expected are exactly the days where gamma or vega is silently building against us — exiting at T+45 / T+90 cuts those before they become large losers. This is risk reduction, but in expected-value terms it acts as a profit driver because it preserves the gains harvested on good days.

### 3.4 Expected profile (rough, to be validated by backtest)

- **Win rate:** ~65–75% (typical for filtered short premium with profit targets)
- **Avg win:** ~30–40% of max profit
- **Avg loss (with no-progress exits):** ~50–70% of max loss — *not* full max loss, because most losers exit early on time/touch
- **Expectancy:** positive iff the no-progress exit is genuinely cutting bad days, not just causing whipsaws on good days. This is the #1 thing to validate.

## 4. How it is risky

### 4.1 Structural risks (known, accepted)

| Risk | What it looks like | Mitigation |
|---|---|---|
| **Gamma risk near short strikes** | Spot drifts to ATM ± small move; short leg P&L explodes non-linearly | Touch exit at short strike |
| **Vol expansion (vega risk)** | News / event during the trade; IV jumps, shorts re-price up even at unchanged spot | Vol-expansion exit; IV regime filter at entry |
| **Trend day** | Spot leaves VWAP early and never comes back; structure is on the wrong side from the open | Trend filter at entry; touch exit if short strike breached |
| **Liquidity dry-up on wings** | Wide spreads at exit make the exit costlier than the SL implies | Liquidity filter at entry; flatten by 15:15 hard stop |
| **Max-loss day** | All exit signals fail or fire too late; structure goes to full wing-width loss | Wings *guarantee* max loss is bounded. This is the floor — not avoidable, but capped. |

### 4.2 Strategy-design risks (these are the ones to watch in validation)

These are the failure modes that *won't* show up in a naive backtest but will surface in replay and live paper:

1. **No-progress exit is the load-bearing assumption.** The whole edge depends on the exits being well-calibrated. If T+45 / T+90 thresholds are too tight, we cut winners; too loose, we hold losers. This needs careful sensitivity analysis — *not* a single parameter setting from intuition.

2. **Backtest will look beautiful — be suspicious.** Short premium strategies have very high in-sample Sharpe because losers are rare in any sample window. The real test is the **3–5 days per quarter** when realized vol exceeds implied. The backtest must be stress-tested on:
   - Budget day, RBI policy day, Fed decision spillover, US CPI spillover
   - Expiry-week Mondays (unusual flow)
   - Post-long-weekend gaps
   - Any day with > 2σ realized move on the underlying

3. **Synthetic option pricing in backtest will mislead.** If the option chain feed reconstructs prices from a vol model (Black-Scholes + smile), the model is necessarily *too smooth* — real markets have liquidity premia and skew kinks that the backtest will under-represent. Validation against real option chain snapshots is required before sizing this up.

4. **Multi-leg execution slippage.** All 4 legs filling at "expected" prices is an idealization. In practice, the shorts fill before the wings on a fast move, leaving a window of naked risk. The execution model must simulate leg-by-leg fills with realistic delays.

5. **Adverse selection in IV regime filter.** The filter selects "middle of the IV percentile band" — but this band is *trailing*. On the rare day when IV is about to break the band upward (regime change), the filter still admits the trade and we get caught in the move. There is no clean fix; this is the residual tail.

### 4.3 What we are NOT protected against

- **Black-swan gap on hold.** We don't hold overnight, so this is structurally avoided.
- **Index-component circuit / halt.** Rare for NIFTY/BANKNIFTY, but if the underlying halts, the wings may not be tradable. Max loss could exceed the modeled W in this case. Logged as a known unprotected tail.
- **Broker / connectivity failure during a touch exit.** Operational risk, not strategy risk. Mitigated by Phase 5 monitoring (heartbeat, alerts), not by strategy logic.

## 5. Why this strategy (vs. alternatives considered)

| Alternative | Reason rejected |
|---|---|
| Naked short straddle | Tail risk incompatible with "conservative SL" — no exit logic saves you from a true gap |
| Iron condor | OTM premium too thin for intraday weekly — decay doesn't compensate for trade frequency |
| Calendar spread | Requires IV term-structure modeling; harder to backtest cleanly; sensitive to IV shape changes |
| 0DTE naked premium | Gamma dominates everything; exit logic can't outrun a single tick at the wrong moment |
| Jade Lizard (skewed) | Viable variant — defer as a v2 once base iron fly is validated; introduces directional bias which is a separate edge to verify independently |
| Adding another directional intraday strategy | Lab already has ORB + VWAP Pullback + Gap — diminishing returns on adding a 4th directional family; this fills the **non-directional gap** in the book |

## 6. Open questions (to resolve before spec)

1. **Wing width W**: fixed in points, fixed as % of spot, or dynamic (ATR-based)? Tradeoff: fixed is simpler to reason about; dynamic adapts to vol regime.
2. **Profit target band (40% vs 50% vs scaled)**: scaled (exit 1/2 at 30%, rest at 50%) reduces variance but adds execution complexity.
3. **DTE selection**: always trade closest weekly, or filter out 0DTE on event days? 0DTE has the most theta but also the most gamma — possibly too sharp.
4. **One trade per day, or multiple?** Re-entry after a no-progress exit is tempting but risks over-trading on bad days. Default: one per underlying per day.
5. **Should the touch exit be on bar-close or intra-bar?** Intra-bar is more realistic but harder to model in the backtest. This is partly an infrastructure decision.

## 7. Validation plan (to be expanded in spec)

Before live paper, the strategy must pass:

1. **Backtest on N-month synthetic chain** — sanity check on logic
2. **Backtest on real chain snapshots** — N expiry cycles minimum
3. **Replay parity** — backtest vs. replay must match trade-for-trade (lab already supports this)
4. **Event-day stress test** — explicit replay on budget / RBI / Fed-spillover days
5. **Parameter sensitivity** — sweep over no-progress thresholds, profit targets, wing widths; reject if Sharpe is only positive in a narrow parameter slice (overfitting signature)
6. **Live paper N weeks** — only after all above pass

---

**Next step (post-doc approval):** decide whether to first build the option-chain infrastructure (Phase 4.6) or first prototype the strategy against synthetic chain data to de-risk the design. Recommend the latter — fail fast on the strategy logic before committing to feed work.
