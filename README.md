# Rotunda

An autonomous, paper-only options agent for the 2026 Alpaca AI Trading Agents Hackathon.

Members of Congress must disclose their securities transactions under the STOCK Act.
Individually those filings are too sparse and too stale to trade. In aggregate they are
a **sector rotation signal**. Rotunda turns them into a per-sector conviction score and
expresses it through defined-risk vertical spreads on liquid sector and index ETFs —
entering only when independent, deterministic price confirmation agrees.

The name is the Capitol Rotunda, and sector rotation.

## How it works

```text
SCAN -> TILT -> CONFIRM -> RISK -> EXECUTE -> MONITOR -> EXIT -> REVIEW
```

- **AI does the interpretation.** It reads disclosure filings and emits a typed,
  schema-validated sector thesis with source attribution back to individual filings.
- **Deterministic code owns every capital-affecting decision.** Pricing, contract
  selection, sizing, order construction, risk gates, exits, and the kill switch.
- The runtime recomputes the arithmetic behind any AI claim and rejects the thesis if
  they disagree. The model explains and weighs; it does not get to invent numbers.

The congressional signal is a **directional prior, not a timing trigger** — filings run
up to 45 days behind the trade. Opening-range and VWAP confirmation on the ETF does the
timing.

## Status

Kickoff was 2026-08-28 10:00 CDT; submission is due 2026-09-04 10:00 CDT. First
tradeable session is Monday 2026-08-31.

Currently building the trading path. See [BACKLOG.md](BACKLOG.md).

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-groups
cp .env.example .env     # then fill in your Alpaca paper credentials
```

The Alpaca CLI is used on the order path:

```bash
curl -fsSL -o /tmp/alpaca.tar.gz \
  https://github.com/alpacahq/cli/releases/download/v0.0.14/cli_0.0.14_linux_amd64.tar.gz
tar -xzf /tmp/alpaca.tar.gz -C /tmp && install -m755 /tmp/alpaca ~/.local/bin/alpaca
```

## Development

```bash
uv run pytest          # tests
uv run ruff check .    # lint
uv run mypy src tests  # types
```

## Research dossier

- [Strategy specification](docs/research/strategy-spec.md) — the active design
- [Hackathon brief](docs/research/hackathon-brief.md) — rules, revalidated at kickoff
- [Alpaca platform notes](docs/research/alpaca-platform.md)
- [Alpaca revalidation, 2026-08-28](docs/research/revalidation-alpaca-2026-08-28.md)
- [Tooling revalidation, 2026-08-28](docs/research/revalidation-tooling-2026-08-28.md)
- [Kickoff checklist](docs/research/kickoff-checklist.md)
- [Source index](docs/research/sources.md)
- [Superseded v0 spec](docs/research/strategy-spec-v0-rotunda.md)

## Guardrails

- **Paper trading only.** The trading host has no code branch that reaches the live
  API, and `ALPACA_LIVE_TRADE=true` refuses to start the process.
- Options are mandatory for this hackathon; execution is limited to defined-risk
  multi-leg positions. No naked short options, no averaging down, no 0DTE.
- Risk gates and the kill switch are deterministic and testable, and never depend on an
  LLM response being available.
- The judged run uses a brand-new Alpaca paper account with exactly $100,000 starting
  equity.
- No API keys, account IDs, or other secrets belong in this repository.

## Data honesty

This project runs on Alpaca's free Basic plan for the whole contest. That means IEX
equity data rather than consolidated SIP, option trades delayed 15 minutes, and
**indicative option quotes rather than OPRA NBBO**. The ETF universe was chosen partly
because penny-wide, deeply liquid contracts are where an indicative quote is closest to
the truth. The dashboard displays the active feed and never labels an indicative quote
as NBBO, and results are reported alongside a conservative shadow P&L that models
spread and slippage.

## Licence

MIT — see [LICENSE](LICENSE).
