# Strategy specification v0: Catalyst Convexity (SUPERSEDED)

Version: research draft 0.1  
Status: **superseded on 2026-08-28 by `strategy-spec.md` (Rotunda).**

> Retained for the record. The architecture in this document survived the
> revision -- AI produces a typed thesis, deterministic code owns every
> capital-affecting decision. What changed at kickoff was the *instrument* and
> the *signal source*: single-name news catalysts became ETF sector verticals
> driven by a congressional-disclosure sector tilt. The reasoning is in
> `strategy-spec.md`, section "Why this replaced Catalyst Convexity".

## Thesis

Trade defined-risk option verticals only when four independent conditions agree:

1. A fresh, directional catalyst exists.
2. The underlying confirms the direction after the open.
3. The option structure offers acceptable defined-risk payoff.
4. The contracts are liquid enough for credible execution.

This is deliberately not an LLM that improvises trades. AI converts unstructured news into a typed thesis; deterministic code owns every capital-affecting decision.

## Agent state machine

```text
SCAN -> ANALYZE -> RISK -> EXECUTE -> MONITOR -> EXIT -> REVIEW
```

- **SCAN:** build a liquid, optionable universe and collect Alpaca movers and news.
- **ANALYZE:** classify the catalyst category, direction, confidence, expected half-life, and invalidation conditions.
- **RISK:** require technical confirmation, contract liquidity, account permissions, buying power, exposure limits, and fresh data.
- **EXECUTE:** construct and submit a defined-risk multi-leg limit order with a unique client order ID.
- **MONITOR:** reconcile order events, quotes, positions, risk, and thesis invalidation.
- **EXIT:** submit a closing multi-leg order when a target, stop, time rule, thesis invalidation, or global risk gate triggers.
- **REVIEW:** persist the complete trace and update official and shadow P&L.

## Candidate universe

- Liquid US stocks and ETFs with listed options.
- Require adequate underlying dollar volume and recent trading activity.
- Require valid contracts in the target expiry window.
- Exclude contracts with zero quotes, stale timestamps, excessive spreads, insufficient open interest/volume when available, or unsupported position intent.
- Keep the initial universe small enough for the Basic plan's option subscription and request limits.

## Catalyst analysis

The AI classifier should return validated structured data, not prose-only advice:

```json
{
  "symbol": "XYZ",
  "category": "earnings|guidance|regulatory|product|macro|analyst|other",
  "direction": "bullish|bearish|neutral",
  "confidence": 0.0,
  "half_life_hours": 0,
  "summary": "short factual explanation",
  "invalidation": ["observable condition"],
  "source_ids": ["persisted news identifier"]
}
```

The runtime rejects malformed output, unsupported claims, neutral direction, weak confidence, stale news, and theses without attributable source identifiers.

## Deterministic confirmation

Initial entry conditions:

- Wait for the first 30-minute opening range.
- Bullish: break above the range with price above VWAP.
- Bearish: break below the range with price below VWAP.
- Relative volume greater than approximately 1.5.
- Directional relative strength or weakness versus SPY.
- Catalyst remains within its expected half-life.
- No global, symbol-level, account, data-quality, or liquidity gate is active.

Thresholds are hypotheses to test, not constants to tune on the judged window.

## Option structure

- Bullish signal: call debit spread.
- Bearish signal: put debit spread.
- Target expiry: approximately 5–14 calendar days.
- Research starting point for the long leg: roughly 0.55–0.70 absolute delta.
- Research starting point for the short leg: roughly 0.20–0.35 absolute delta.
- Use a width and debit that produce an acceptable maximum-loss/maximum-profit ratio after conservative execution costs.
- Use one atomic multi-leg limit order; never leg into a spread.
- No naked short options, martingale sizing, averaging down, or expiry-day dependence.

If Greeks are missing, contract selection must fall back to deterministic moneyness, expiry, spread, and liquidity rules or reject the candidate. It must never fabricate a Greek.

## Risk gates

Research defaults for a $100,000 account:

- Maximum risk per trade: 0.5% of current equity.
- Maximum concurrent positions: 3.
- Maximum total open defined risk: 2% of current equity.
- Daily realized plus conservative unrealized loss stop: 1.5%.
- No new positions late in the session.
- No averaging into a losing spread.
- No order when account state, options level, quote freshness, clock, or position reconciliation is uncertain.
- Close before expiration; do not rely on automatic exercise or assignment handling.
- A global kill switch cancels open orders and prevents new entries. Any automated liquidation behavior must be separately tested in paper mode.

All thresholds belong in versioned configuration and must appear in the one-page submission write-up.

## Exit research plan

Test combinations of:

- Profit target as a percentage of maximum profit or initial debit.
- Loss limit as a percentage of initial debit.
- Thesis invalidation: VWAP reversal, return through the opening range, or catalyst contradiction.
- Time stop when momentum fails to continue.
- Mandatory exit sufficiently before expiration and before the submission cutoff.

The agent must evaluate exits independently of whether the AI classifier is available. Risk management cannot depend on an LLM response.

## Validation plan

Compare at least four baselines:

1. Opening-range confirmation only.
2. Catalyst classification only.
3. Catalyst plus confirmation.
4. Equivalent directional stock exposure versus the vertical-spread structure.

Report net P&L, win rate, profit factor, maximum drawdown, average R, turnover, rejected signals, unfilled orders, and slippage sensitivity. Use explicit decision timestamps, next-observable fill assumptions, no lookahead, data fingerprints, and walk-forward or held-out periods where the data permits.

Historical options research is a selection-and-execution simulation, not merely a signal backtest. It must model which contracts were observable and tradable at each timestamp.

## Live dashboard and audit trail

Show:

- Current state and last successful cycle.
- Candidate symbols and rejection reasons.
- Catalyst thesis with source attribution.
- Confirmation values and risk-gate results.
- Selected legs, limit price, maximum loss, maximum profit, and sizing calculation.
- Open orders, fills, positions, realized/unrealized P&L, account equity, and drawdown.
- Official Alpaca P&L and conservative shadow P&L.
- Active market-data feed and known limitations.
- A complete chronological decision log and kill-switch status.

## Internal research subsystem

The earlier **AlphaProof** concept becomes an internal validation layer: it produces reproducible backtest artifacts, challenges fill assumptions, measures sensitivity, and flags claims that the evidence does not support. The externally visible product remains Catalyst Convexity.

