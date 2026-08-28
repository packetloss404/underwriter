# Alpaca platform research

Research date: August 26, 2026

## Recommended implementation stack

- Python 3.11 or newer.
- Official `alpaca-py` SDK for the agent's durable market-data, order, account, and stream integrations.
- Alpaca CLI to satisfy the required CLI/MCP component and for preflight, diagnostics, inspection, and reproducible operator actions.
- Direct Trading API calls only where the SDK or CLI lacks a stable multi-leg operation.
- SQLite for decisions, risk state, orders, fills, position snapshots, and P&L history.
- A lightweight web dashboard for the live demo.
- Alpaca MCP is optional for interactive research and demonstration, not the initial critical order path.

## API and SDK capabilities

The Python SDK exposes the important building blocks:

- `TradingClient` for accounts, positions, orders, and option contracts.
- `OptionHistoricalDataClient` for historical option data.
- `OptionDataStream` for option market-data streaming.
- `TradingStream` for order and fill updates.
- `OptionChainRequest`, `GetOptionContractsRequest`, and multi-leg order models such as `OptionLegRequest`.

The agent should subscribe to `trade_updates` and persist fills, partial fills, cancellations, and rejections. Assignment events are not delivered through that stream, so account activities must also be polled.

## Alpaca CLI

The CLI is currently labeled Alpha Preview. Its installed help and schema output are the source of truth:

```text
alpaca --help-all
alpaca <command> --help
alpaca <command> --schema
alpaca doctor
```

Useful characteristics:

- Paper trading is the default.
- Live trading requires the explicit `ALPACA_LIVE_TRADE=true` opt-in.
- Common configuration includes `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PROFILE`, and `ALPACA_OUTPUT`.
- Normal output is structured JSON; API failures use structured JSON on stderr.
- Exit code `0` means success, `1` means an API error, and `2` means authentication failure.
- It covers accounts, orders, positions, bars, movers, news, option chains, snapshots, and quotes.
- `alpaca api METHOD /path` is available as a lower-level escape hatch.
- CLI mutations do not provide an interactive confirmation prompt.

Use a unique client order ID for idempotency. If an order request times out, look it up by client order ID before retrying.

### Environment-variable correction

`ALPACA_PAPER=true` is not a documented Alpaca CLI setting. The CLI uses paper trading by default and requires `ALPACA_LIVE_TRADE=true` for live trading. Alpaca MCP uses the separate `ALPACA_PAPER_TRADE=true` setting.

## Alpaca MCP server

The official MCP server advertises 65 tools across account, trading, assets, stock-data, options-data, and news toolsets. It can inspect portfolios and market data and submit option orders, including multi-leg orders.

Relevant settings include:

- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `ALPACA_PAPER_TRADE`, which defaults to paper mode
- `ALPACA_TOOLSETS`, for limiting exposed tool groups

MCP is attractive for long-lived AI sessions and a visible agent demonstration. A July 2026 open issue reports that some MCP clients fail to serialize the multi-leg `legs` array correctly, so initial multi-leg execution should go through the SDK, CLI raw API command, or direct REST request and be covered by a paper-mode integration test.

## Options account and order constraints

- Paper options are generally enabled, but the effective `options_trading_level` must be checked at runtime.
- The effective level is the lower of the approved account level and the configured maximum level.
- Level 1 covers covered calls and cash-secured puts; Level 2 adds long calls and puts; Level 3 is required for spreads and straddles.
- Multi-leg (`mleg`) orders support two to four option legs.
- Multi-leg quantity must be a whole number. Option orders do not support fractional or notional sizing.
- Multi-leg orders support market or limit pricing and day or GTC time in force, but not extended hours.
- Each leg needs an explicit position intent.
- Leg ratios must be simplified by their greatest common divisor.
- Equity legs are not supported in an option multi-leg order, and uncovered short legs are rejected.
- Stop and stop-limit orders are single-leg only. Bracket/OCO/OTO order classes are equity features, so spread exits require a monitored closing multi-leg order.
- Query Alpaca's option-contract endpoint rather than constructing OCC symbols manually.
- Contract searches need explicit expiry filters; the default query window may end at the upcoming weekend.

In-the-money options by at least $0.01 are eligible for automatic exercise. Alpaca can sell out positions during the final hour before expiry if buying power is insufficient. To remove avoidable expiry risk, this strategy closes positions before expiration and avoids 0DTE contracts.

## Market-data constraints

The free Basic Trading API plan is a material strategy constraint:

- Equity real-time data is IEX rather than the consolidated SIP feed.
- Options trades are delayed by 15 minutes.
- Options quotes are indicative derivatives rather than true OPRA NBBO quotes.
- The plan permits about 200 option quote subscriptions and about 200 historical requests per minute.
- OPRA data is associated with the paid Algo Trader Plus plan.
- Historical option data is available from February 2024 onward.
- The option WebSocket uses MessagePack; the official SDK handles this encoding.

Greeks and implied volatility may be absent when a bid or ask is zero, the underlying SIP price is unavailable, the contract expires that day, or the IV solver fails. The strategy must not depend on complete Greeks or 0DTE data.

Design response:

- Generate signals primarily from the underlying asset and catalyst data.
- Trade only liquid contracts with conservative spread filters.
- Prefer roughly 5–14 days to expiry.
- Use marketable limit orders and record the quote/feed used for every decision.
- Detect and display the active data entitlement.
- Never label indicative quotes as NBBO.

## Paper-trading realism

Alpaca paper trading simulates fills using the current quote, but it does not model market impact, information leakage, latency slippage, queue position, price improvement, regulatory fees, or dividends. Displayed quote size does not restrict simulated order quantity. Partial fills have a simplified randomized behavior.

Therefore the evaluation should report:

1. Official Alpaca paper P&L.
2. Conservative shadow P&L with explicit bid/ask and slippage assumptions.
3. Rejected and unfilled signals, rather than silently excluding them.
4. Data-feed and fill-model limitations beside every performance claim.

## Official Alpaca skills worth using at kickoff

The `alpacahq/alpaca-skills` repository contains these relevant skills:

- `alpaca-trading-backtest`
- `alpaca-trading-paper-trading`
- `alpaca-trading-paper-trading-cli`
- `alpaca-trading-paper-trading-mcp`

The backtest skill emphasizes formal rules, explicit fill timing, no lookahead, spread/slippage/fees, benchmarks, out-of-sample or walk-forward validation, a data fingerprint, warnings, and reproducible artifacts. Historical option tests additionally need explicit contract-selection and fill logic.

The paper-trading guidance emphasizes verifying the paper endpoint, account status, buying power, effective options level, risk limits, client order IDs, and persisted order/position review artifacts. Install and inspect the relevant skills at kickoff rather than changing the environment before the event starts.

