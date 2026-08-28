# Strategy specification: Rotunda

Version: 1.0  
Status: active, adopted 2026-08-28 at kickoff  
Supersedes: `strategy-spec-v0-rotunda.md`

## Thesis

Members of Congress are legally required to disclose their securities transactions
under the STOCK Act. Individually those filings are too sparse and too stale to trade.
**In aggregate they are a sector rotation signal.**

Rotunda aggregates congressional disclosures into a per-sector conviction score, then
expresses that view through defined-risk vertical spreads on liquid sector and index
ETFs — entering only when independent, deterministic price confirmation agrees.

The name is the Capitol Rotunda and sector rotation.

## Why this replaced Catalyst Convexity

The v0 concept traded single-name option verticals on news catalysts. Three kickoff
findings moved us:

1. **Market data is permanently degraded.** Algo Trader Plus turned out to be a prize
   for the two social-prize winners, not a participant benefit. We are on the Basic
   plan for the whole contest: IEX equities, 15-minute-delayed option trades, and
   *indicative* option quotes rather than OPRA NBBO. On a mid-cap single name an
   indicative quote for a thin contract is close to fiction, and every spread and
   liquidity filter we write would be guessing. On SPY, QQQ, and the sector SPDRs the
   options are penny-wide with deep open interest, so the indicative quote is close to
   the truth. **ETFs are the instrument where our degraded data hurts least.**
2. **Congressional filings are too sparse per name to trade directly**, and members
   rarely disclose options at all. Aggregating their *stock* disclosures into a sector
   tilt makes the sparsity irrelevant: many weak filings collapse into a few sector
   views, and we never need a filing on the ticker we actually trade.
3. **Single names carry gap risk we cannot manage in a four-session window.** Early
   September has real earnings landmines. An ETF book steps around all of them.

The architecture did not change. AI converts unstructured disclosure text into a typed,
schema-validated thesis; deterministic code owns pricing, sizing, order construction,
risk gates, and the kill switch.

## What is honest about this

Stated plainly here because it belongs in the submission write-up too:

- **The congressional signal is slow.** PTRs are due within 45 days of the trade and
  members routinely file at the deadline. The sector tilt barely moves across four
  sessions. It is a **directional prior, not a timing trigger.**
- Therefore the deterministic confirmation layer does the timing work. If the slow
  signal also drove entry timing, the agent would form one view on Monday and sit,
  producing two trades and nothing to demonstrate.
- We do not claim the congressional tilt is proven alpha. We claim it is a transparent,
  auditable, public-data prior, and we report its contribution against baselines that
  strip it out.

## Agent state machine

```text
SCAN -> TILT -> CONFIRM -> RISK -> EXECUTE -> MONITOR -> EXIT -> REVIEW
```

- **SCAN:** refresh disclosure feeds; refresh ETF universe quotes, bars, and chains.
- **TILT:** AI maps disclosures to sectors and emits a typed per-sector conviction score.
- **CONFIRM:** deterministic price confirmation on the candidate ETF.
- **RISK:** permissions, buying power, exposure, freshness, liquidity, kill switch.
- **EXECUTE:** one atomic multi-leg limit order with a unique client order ID.
- **MONITOR:** reconcile order events, quotes, positions, and tilt invalidation.
- **EXIT:** closing multi-leg order on target, stop, time rule, or invalidation.
- **REVIEW:** persist the full trace; update official and shadow P&L.

## Universe

Fixed and small — it fits the Basic plan's ~200 option-subscription cap with room to
spare, and every member has penny-wide weekly options.

| Ticker | Exposure |
|---|---|
| SPY | S&P 500 |
| QQQ | Nasdaq 100 |
| IWM | Russell 2000 |
| XLE | Energy |
| XLF | Financials |
| XLK | Technology |
| XLV | Health care |
| XLI | Industrials |
| XLY | Consumer discretionary |
| XLP | Consumer staples |
| XLU | Utilities |
| XLB | Materials |
| SMH | Semiconductors |
| ITA | Aerospace and defence |
| TLT | Long Treasuries |
| GLD | Gold |

SPY and QQQ carry Mon/Wed/Fri expiries with $1-wide strikes, so the target DTE window
is always densely populated. The sector SPDRs are Friday-weekly with $1-wide strikes.

## Disclosure ingestion

Sources, in priority order. Every persisted record keeps a source identifier and a
retrieval timestamp so the dashboard can attribute any thesis back to a filing.

1. **House Clerk** periodic transaction reports.
2. **Senate eFD** periodic transaction reports.
3. **SEC Form 4** insider transactions — denser and far fresher (two business days
   rather than 45), used as a corroborating signal on the same sector map.

Ingestion is deliberately fail-soft: a source that is unreachable or stale degrades the
conviction score and is displayed as degraded. It never fabricates a tilt.

Normalised record:

```json
{
  "filing_id": "persisted unique identifier",
  "source": "house|senate|sec_form4",
  "filer": "name as disclosed",
  "ticker": "AAPL",
  "sector": "XLK",
  "action": "buy|sell",
  "amount_low": 1001,
  "amount_high": 15000,
  "transaction_date": "2026-07-15",
  "disclosure_date": "2026-08-26",
  "lag_days": 42,
  "retrieved_at": "2026-08-28T20:00:00Z"
}
```

Amounts are disclosed as ranges, never exact figures. Sizing uses the range midpoint
and records that it is an estimate.

## The AI tilt layer

The classifier receives normalised disclosure records and returns validated structured
data, never prose-only advice:

```json
{
  "sector_etf": "XLE",
  "direction": "bullish|bearish|neutral",
  "conviction": 0.0,
  "filing_count": 0,
  "net_dollar_estimate": 0,
  "distinct_filers": 0,
  "dominant_lag_days": 0,
  "summary": "short factual explanation",
  "invalidation": ["observable condition"],
  "filing_ids": ["persisted filing identifier"]
}
```

The runtime rejects malformed output, neutral direction, weak conviction, theses whose
`filing_ids` do not resolve to persisted records, and any claim whose
`net_dollar_estimate` disagrees with the deterministic recomputation from those records.
**The AI explains and weighs; it does not get to invent the arithmetic.**

Conviction is scaled down by disclosure lag, by concentration in a single filer, and by
low distinct-filer counts. A tilt from one member is weak by construction.

## Deterministic confirmation

The prior says *which* sector and *which* direction. This layer decides *whether* and
*when*. All of it is computable from IEX bars without any option data.

- Wait for the first 30-minute opening range on the candidate ETF.
- Bullish: break above the range with price above VWAP.
- Bearish: break below the range with price below VWAP.
- Relative volume above approximately 1.2 (ETFs are steadier than single names, so the
  v0 threshold of 1.5 is too strict here).
- Directional relative strength or weakness versus SPY — skipped when the candidate
  *is* SPY.
- No global, sector-level, account, data-quality, or liquidity gate active.

Thresholds are hypotheses to test, not constants to tune on the judged window.

## Option structure

- Bullish tilt plus bullish confirmation: **call debit spread**.
- Bearish tilt plus bearish confirmation: **put debit spread**.
- Target expiry: 5–14 calendar days. Never 0DTE.
- Long leg: roughly 0.55–0.70 absolute delta. Short leg: roughly 0.20–0.35.
- Width and debit must give an acceptable max-loss to max-profit ratio after
  conservative execution costs.
- One atomic multi-leg limit order. Never leg into a spread.
- No naked short options, martingale sizing, averaging down, or expiry-day dependence.

If Greeks are missing — which the Basic plan permits — selection falls back to
deterministic moneyness, expiry, spread, and liquidity rules, or rejects the candidate.
**It never fabricates a Greek.**

## Risk gates

Defaults for a $100,000 account, versioned in configuration and quoted in the
submission write-up:

- Max risk per trade: 0.5% of current equity.
- Max concurrent positions: 3.
- Max total open defined risk: 2% of current equity.
- **Max one open position per sector ETF, and max two positions with correlated
  exposure** — new for Rotunda. SPY, QQQ, and XLK overlap heavily; without this gate
  three "independent" positions can be one bet.
- Daily realised plus conservative unrealised loss stop: 1.5%.
- No new entries after 15:00 ET.
- No averaging into a losing spread.
- No order when account state, options level, quote freshness, clock, or position
  reconciliation is uncertain.
- Close before expiration; never rely on automatic exercise or assignment.
- A global kill switch cancels open orders and blocks new entries.

Gate on the account's **`options_trading_level`** (the effective level), never
`options_approved_level`. Require effective level 3 before any spread.

## Exit rules

- Profit target as a percentage of max profit or of initial debit.
- Loss limit as a percentage of initial debit.
- Tilt invalidation: VWAP reversal, return through the opening range, or a contradicting
  disclosure batch.
- Time stop when momentum fails to continue.
- Mandatory exit before expiration and before the submission cutoff.

**Exits must evaluate without the AI.** Risk management cannot depend on an LLM
response being available or well-formed.

## Validation plan

Baselines, so the congressional layer has to earn its place:

1. Opening-range confirmation only, no tilt.
2. Tilt only, no confirmation.
3. Tilt plus confirmation — the live strategy.
4. Equivalent directional ETF exposure versus the vertical-spread structure.
5. Buy-and-hold SPY over the same window.

Report net P&L, win rate, profit factor, max drawdown, average R, turnover, rejected
signals, unfilled orders, and slippage sensitivity. Explicit decision timestamps,
next-observable fill assumptions, no lookahead, data fingerprints.

## Dashboard and audit trail

- Current state and last successful cycle.
- Per-sector tilt with contributing filings and source attribution.
- Confirmation values and every risk-gate result, including rejections.
- Selected legs, limit price, max loss, max profit, and the sizing calculation.
- Open orders, fills, positions, realised and unrealised P&L, equity, drawdown.
- Official Alpaca P&L alongside conservative shadow P&L.
- Active market-data feed, entitlement, and known limitations — never label an
  indicative quote as NBBO.
- Chronological decision log and kill-switch status.

## Execution path

The hackathon requires the Alpaca MCP server or CLI. CLI v0.0.14, released on kickoff
morning, supports multi-leg natively via `alpaca order submit --legs`, with `--dry-run`
for payload verification and `--client-order-id` for idempotency.

**The CLI is on the real order path**, with the `alpaca-py` SDK as a tested fallback
behind one interface. The CLI is stamped "Alpha Preview — do not depend on current
behavior in production", and v0.0.14 was regenerated from a new OpenAPI spec hours
before the build, so the fallback is not optional.
