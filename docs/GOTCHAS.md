# Gotchas

Failure modes verified during kickoff research that do **not** announce themselves.
Each one looks like a different problem than it is. Ordered by how much time they cost
if missed.

## 1. `expiration_date_lte` silently truncates the contract universe

`GET /v2/options/contracts` **does not error** when expiry bounds are omitted. It
returns only contracts expiring before the coming weekend. The chain simply comes back
thin.

This presents as a liquidity problem, a bad underlying, or a broken filter — never as a
missing query parameter. You will debug the wrong thing.

**Rule:** always send explicit `expiration_date_gte` *and* `expiration_date_lte`.
`underwriter.chain` refuses to build a request without both.

## 2. `options_trading_level` is not `options_approved_level`

```
options_trading_level = min(options_approved_level, max_options_trading_level)
```

A fresh paper account can report `options_approved_level: 3` while the effective level
is lower, because the account configuration caps it. Every multi-leg order then rejects
with a level error that reads like an *approval* problem, so you go looking at the
account approval rather than at the configuration.

**Rule:** gate on `options_trading_level`. `underwriter.preflight` does, and warns
explicitly when the two disagree. `max_options_trading_level` can be raised via
`PATCH /v2/account/configurations` — do this at setup, not mid-session.

## 3. Paper multi-leg fills are simulated against *modified* indicative quotes

We are on the Basic plan permanently, so option quotes are indicative derivatives, not
OPRA NBBO. Alpaca's paper engine checks marketability against those altered quotes, and
**the multi-leg fill model is entirely undocumented** — whether the documented ~10%
random partial-fill behaviour applies to a spread is unknown, and a partially filled
vertical contradicts all-or-nothing execution.

**Rule:** the official paper P&L is a number we report, not a number we trust. The
conservative shadow P&L modelling explicit bid/ask and slippage is the honest one, and
both belong in the submission side by side.

## 4. Open interest is not in the option chain snapshot

`OptionsSnapshot` carries `symbol`, `latest_trade`, `latest_quote`,
`implied_volatility` and `greeks` — and **no open interest**. It lives only on
`OptionContract`, from the separate `get_option_contracts` endpoint, so obtaining it
means a second call per underlying and a join on symbol.

A liquidity filter that treats missing open interest as a failure therefore rejects the
entire chain, for a reason that is not true.

**Rule:** `LiquidityPolicy.require_open_interest` defaults to `False`, so a missing value
is tolerated and recorded while an explicit low value still rejects. Calibration performs
the join deliberately, because measuring open interest is one of the things it exists to
do.

## 5. The Basic plan refuses recent SIP data outright

A bars query ending at `now` fails with:

```
403 Forbidden
{"message":"subscription does not permit querying recent SIP data"}
```

It does **not** silently degrade to a permitted feed — it errors, so a first live run
dies on the very first call.

Two fixes, and they are not equivalent. Passing `feed="iex"` succeeds but returns only
IEX's share of volume, and the prices differ (SPY closed 769.35 on SIP against 769.28 on
IEX). Backing the query's `end` off past the fifteen-minute embargo keeps us on
consolidated SIP, which is strictly better data.

**Rule:** `data.SIP_EMBARGO` is 20 minutes and `daily_closes` ends its window there. This
costs nothing, because realised volatility is computed from *completed* daily bars and
today's partial bar does not belong in it.

## 6. `alpaca order submit` defaults to `--type market`

For a multi-leg spread you must pass `--type limit` explicitly. Forgetting it submits a
market order on a spread — the exact thing the strategy spec forbids.

**Also:** omit `--symbol` entirely for `mleg`. The OAS says symbol is *"required for all
order classes except for `mleg`"*, where it lives on each leg instead.

## 7. Time in force for options: `day` only

The docs prose says `day` or `gtc`; the OpenAPI spec says `day`. Unresolved
contradiction — code against `day`. This means spread exits need an actively monitored
closing order, since nothing rests overnight.

## 8. MCP multi-leg `legs` serialization is still broken

Issue #97 is untouched since 2026-07-01; fix PR #107 is open and unmerged; `overrides.py`
on `main` is still unpatched. **Do not put the MCP server on the order path.** The CLI's
`--legs` flag (v0.0.14) is verified working and satisfies the hackathon's MCP-or-CLI
requirement.

## 9. `alpaca-py` upstream only CI-tests Python 3.10 and 3.11

The 3.12/3.13/3.14 classifiers are auto-generated from a `^3.10.0` constraint, not a
tested matrix. We run 3.12. Also, alpaca-py floats on `pandas>=1.5.3`, so it will happily
resolve onto pandas 3.x that upstream has never exercised — **pin pandas explicitly**.
