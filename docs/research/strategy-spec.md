# Strategy specification: volatility risk premium harvesting

Version: 2.0  
Status: active, adopted 2026-08-28  
Supersedes: `strategy-spec-v1-rotunda-disclosure.md`, `strategy-spec-v0-catalyst-convexity.md`

## Thesis

Options are priced off *implied* volatility — what the market expects. What
subsequently happens is *realised* volatility. Implied exceeds realised the large
majority of the time across essentially every liquid underlying, because option sellers
demand compensation for bearing tail risk. That gap is the **volatility risk premium**:
a structural insurance premium, not a backtest artefact.

The agent ranks a fixed universe of liquid ETFs by how rich implied volatility is
relative to realised, and **sells defined-risk credit spreads where the premium is
richest** — subject to a market-regime filter and an AI catalyst veto.

It cannot be done with stock. It exists only in options.

## Why this replaced the disclosure tilt

Both earlier concepts sought a *directional* edge from disclosure data, and both died on
the same arithmetic:

- Congressional PTRs yield **~9–10 filings** across Mon 31 Aug – Thu 3 Sep, against a
  documented disclosure-date effect of **+12–18bps**. A single mega-cap's daily σ is
  ~130bps. Ten events cannot separate that from noise, and the window falls in the
  sparsest part of the monthly filing cycle.
- Switching to SEC Form 4 fixed density (~425 filings/day) but not the horizon: the
  documented insider-buy effect is a **multi-month drift**.

Both were slow signals on a four-session clock. The volatility risk premium is a
**daily** decay, so the horizon problem disappears rather than being managed.

| | Disclosure tilt | Volatility risk premium |
|---|---|---|
| Edge accrues over | months | days |
| Requires a directional call | yes | no |
| Expected trades in window | ~2 | 10+ |
| Options intrinsic to the edge | no | **yes** |

## Agent state machine

```text
SCAN -> RANK -> REGIME -> VETO -> RISK -> EXECUTE -> MONITOR -> EXIT -> REVIEW
```

- **SCAN:** refresh bars, chains, and snapshots for the universe.
- **RANK:** compute realised vol, implied vol, and the premium ratio per instrument.
- **REGIME:** apply the market-regime filter. Blocks *all* new premium selling in a
  hostile tape.
- **VETO:** AI screens each candidate for an identifiable catalyst justifying the
  elevated implied vol. It may only remove candidates.
- **RISK:** existing engine, plus the aggregate short-delta cap.
- **EXECUTE:** one atomic multi-leg limit order with a unique client order ID.
- **MONITOR:** reconcile fills, positions, premium decay, and regime state.
- **EXIT:** profit target, loss limit, regime break, or time stop.
- **REVIEW:** persist the trace; update official and shadow P&L.

## Measuring the premium

**Realised volatility.** Close-to-close log returns from daily IEX bars, annualised by
√252, over a trailing 20-session window. Also computed over 10 sessions so a
short-window spike is visible rather than averaged away.

**Implied volatility.** From near-the-money contracts in the target expiry, taken from
the option chain snapshot. When implied vol is missing — permitted on the Basic plan
whenever a bid or ask is zero, the underlying SIP price is unavailable, or the solver
fails — the instrument is **skipped with a recorded reason**. It is never estimated.

**The premium ratio.**

```text
vrp_ratio = implied_vol / realised_vol_20
```

A ratio rather than a difference, because the universe is heterogeneous — TLT and SMH
have very different absolute volatility levels, and a difference in vol points is not
comparable across them. A ratio is.

Practitioners normally rank by IV Rank (where implied sits in its own trailing range).
That needs an implied-vol history we will not have accumulated inside a seven-day
contest, so the ratio — which requires no history — is the honest choice here. This is a
constraint, and it is stated as one.

Entry requires `vrp_ratio` above a configured floor. The floor is a hypothesis to test,
not a constant to tune on the judged window.

## Structure

**Default: put credit spreads.** Sell the nearer strike, buy the further-out strike as
protection, same expiry.

Two reasons, both principled:

1. **Put skew.** Equity index puts are systematically richer than equidistant calls.
   Selling put spreads harvests the volatility premium *and* the skew premium in one
   structure — we are selling the expensive part of the surface.
2. **Two legs, not four.** An iron condor is the purer delta-neutral expression, but
   doubles the legs, the fill risk, and the effective spread crossed. On indicative
   quotes with an undocumented multi-leg paper fill model, fewer legs is materially
   safer. Condors are a documented extension, not the starting point.

Parameters:

- Short leg: roughly 0.15–0.30 absolute delta — out of the money, high probability of
  expiring worthless.
- Long leg: one to three strikes further out, defining the risk.
- Expiry: 5–14 days. Never 0DTE.
- Credit received must be a meaningful fraction of the width, after assuming we cross
  half of each leg's quoted spread.
- One atomic multi-leg limit order. Never legged in.
- Max loss = width − credit, known before entry and bounded.

## The AI's role: veto only

Selling premium into a real catalyst is the classic way to be run over. Elevated implied
vol is sometimes mispricing and sometimes the market correctly pricing a known event.
The ratio alone cannot distinguish them.

So the AI answers exactly one question per candidate: **is there an identifiable reason
this instrument's implied volatility is elevated?** Scheduled earnings for major
constituents, a central bank decision, an OPEC meeting, a pending regulatory ruling, an
unfolding geopolitical event.

```json
{
  "symbol": "XLE",
  "catalyst_found": true,
  "catalyst": "OPEC+ ministerial meeting scheduled within the holding period",
  "confidence": 0.0,
  "source_ids": ["persisted news identifier"]
}
```

**The model can only remove candidates, never add them.** A hallucinated catalyst costs
an opportunity; it cannot cost money. A missing or malformed response is treated as a
veto, not as an approval — the failure mode of an unavailable model is that the agent
trades less, never that it trades unguarded.

## The regime filter

**This is the strategy's central risk.** Every short put position is correlated. In a
sharp selloff they all lose together. Defined risk bounds each position, but they move
as a block, so per-position gates are insufficient by construction.

All new premium selling is blocked when any of:

- SPY is below its 20-session moving average.
- SPY has fallen more than a configured percentage over the trailing 3 sessions.
- Realised volatility across the universe is expanding rather than contracting.
- **The implied volatility term structure is inverted.**
- A scheduled macro event falls inside the intended holding period.

Plus an **aggregate short-delta cap** across all open positions, so the book cannot
accumulate into a single large directional bet through many individually-compliant
trades.

### The volatility term structure

Every other regime check is backward-looking: trend, drawdown and realised expansion all
describe what already happened. The term structure is the only **forward-looking** input,
and it is measured on exactly the surface we sell into.

Near-dated at-the-money implied volatility is compared against far-dated. In a calm
market the near expiry prices *below* the far one — near-term uncertainty is genuinely
lower — so a healthy ratio sits around 0.85 to 0.95. When the ratio crosses 1.0 the curve
is inverted: the market is pricing more risk into the next week than the next two months.
**That is the condition under which short-volatility books take their worst losses**, and
it typically appears before realised volatility expands.

This is computed from SPY's own option chain rather than from VIX. Index data is not
available on the Basic plan, and the VIX-tracking ETNs carry roll decay that makes their
levels misleading even where their direction is not. The option surface we actually trade
is both more direct and more honest.

A missing or unusable curve blocks entries. The two sampled expiries must also be at
least 14 days apart, or both readings sit at effectively the same point on the curve and
the ratio measures noise.

### Hard flatten cutoff before expiry

If buying power is insufficient for an in-the-money exercise, **Alpaca will sell out the
position within one hour before expiry**. That is a broker action we do not control, and
a forced liquidation at whatever price the book offers is exactly the uncontrolled
outcome defined risk exists to prevent.

The strategy therefore carries a hard flatten cutoff comfortably before 15:00 ET on any
expiration day, enforced as a rule rather than left to the ordinary exit logic to reach
in time. Being early costs a few hours of theta; being late surrenders price discovery to
the broker.

### Known event in the window

**Non-farm payrolls, Friday 4 September 2026, 08:30 ET** — roughly ninety minutes before
the submission deadline. A scheduled volatility event inside the judged window. The
agent must be flat or deliberately positioned into it, by rule rather than by accident.

## Risk gates

Inherited unchanged from the existing engine: per-trade risk 0.5% of equity, max 3
concurrent positions, 2% aggregate open risk, correlated-exposure gate, options buying
power, 15:00 ET entry cutoff, daily loss stop of 1.5% measured against session-open
equity, global kill switch.

Added for this strategy:

- Aggregate short-delta cap across the book.
- Regime filter as a hard, global no-entry gate.
- Mandatory flat before a scheduled macro event inside the holding period.

## Exits

- Profit target: buy the spread back at a configured fraction of the credit received.
  Taking profit early is how premium selling actually works — the last portion of the
  credit takes the longest to earn and carries the most gamma risk.
- Loss limit as a multiple of the credit received.
- Regime break: close or stop rolling when the filter turns hostile.
- Time stop well before expiry; never hold into expiration week gamma.

Exits are pure code and never depend on an LLM response.

## Validation

Baselines, so each layer must earn its place:

1. Sell premium on every instrument every day, no ranking — is the ranking adding value?
2. Ranked premium selling with no regime filter — is the filter adding value?
3. Ranked premium selling with regime filter, no AI veto — is the veto adding value?
4. The full strategy.
5. Buy and hold SPY over the same window.

Report net P&L, win rate, profit factor, max drawdown, average credit captured as a
fraction of credit received, rejected candidates and why, unfilled orders, and slippage
sensitivity. Official Alpaca P&L is reported beside conservative shadow P&L; paper
multi-leg fills simulate against modified indicative quotes and the fill model is
undocumented, so the shadow figure is the honest one.

## What is honest about this

- The volatility risk premium is real and well documented, but it is **short volatility**:
  many small wins and occasional larger losses. Four sessions is far too short to
  demonstrate the loss side. A green result does not validate it; a red one does not
  refute it. This will be stated plainly in the submission.
- Defined-risk spreads bound the tail, but correlated positions mean a single bad
  session can hit the whole book. That is what the regime filter and short-delta cap
  exist for, and they should be judged on whether they fire, not on the P&L.
- We cannot compute IV Rank without an implied-vol history, so the ranking uses the
  implied/realised ratio instead. That is a weaker instrument than a practitioner would
  use, and it is disclosed.
