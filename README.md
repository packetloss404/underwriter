# Underwriter

An autonomous, paper-only options agent for the 2026 Alpaca AI Trading Agents Hackathon.

Selling an option is underwriting insurance: you collect a premium, take on a bounded
risk, and profit when nothing happens. Underwriter does exactly that, systematically —
and declines far more often than it writes.

## The edge

Options are priced off **implied** volatility, what the market expects. What follows is
**realised** volatility. Implied exceeds realised the large majority of the time across
liquid underlyings, because sellers demand compensation for bearing tail risk. That gap
is the **volatility risk premium** — a structural insurance premium rather than a
backtest artefact.

The agent ranks a fixed universe of 16 liquid ETFs by how rich implied volatility is
relative to realised, then sells defined-risk credit spreads where the premium is
richest. It cannot be done with stock; it exists only in options.

Unlike a directional signal, the premium decays **daily**, which is why it suits a
four-session judged window.

## How it works

```text
SCAN -> RANK -> REGIME -> VETO -> RISK -> EXECUTE -> MONITOR -> EXIT -> REVIEW
```

Eight stages, each able to stop the trade, with **39 distinct refusal reasons** between
them. Every refusal is recorded and displayed — a strategy whose rejections are
invisible is unexplainable.

- **Deterministic code owns every capital-affecting decision**: ranking, contract
  selection, pricing, sizing, risk gates, exits, and the kill switch.
- **The AI can only take risk off the table.** Its single job is to veto candidates
  whose elevated implied volatility has an identifiable cause — a scheduled event, a
  pending ruling. It can remove candidates, never add them. A hallucinated catalyst
  costs an opportunity; it cannot cost money. A missing or malformed response is treated
  as a veto, not an approval.

## Risk

The strategy's central structural risk is that **every short put in the book loses
together**. Defined risk bounds each position, but they are not independent, so
per-position gates are insufficient by construction. Three gates address that at
different scales:

| Gate | Stops |
|---|---|
| Regime filter | Opening anything at all in a hostile tape |
| Correlation gate | Two positions being one bet |
| Aggregate net-delta cap | Six positions being one bet |

Standing limits: 0.5% risk per trade, 6 concurrent positions, 3% aggregate open risk,
1.5% daily loss stop measured against session-open equity, 15:00 ET entry cutoff, and a
global kill switch.

## Status

Kickoff was 2026-08-28 10:00 CDT; submission is due 2026-09-04 10:00 CDT. First
tradeable session is Monday 2026-08-31.

Built and tested: configuration, preflight, universe, volatility ranking, regime filter,
contract selection, risk engine. Pending live credentials: market-data client, catalyst
veto, execution, journal, exit monitor. See [BACKLOG.md](BACKLOG.md).

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-groups
cp .env.example .env     # then fill in your Alpaca paper credentials
```

The Alpaca CLI sits on the order path:

```bash
curl -fsSL -o /tmp/alpaca.tar.gz \
  https://github.com/alpacahq/cli/releases/download/v0.0.14/cli_0.0.14_linux_amd64.tar.gz
tar -xzf /tmp/alpaca.tar.gz -C /tmp && install -m755 /tmp/alpaca ~/.local/bin/alpaca
```

## Development

```bash
uv run pytest          # 209 tests
uv run ruff check .    # lint
uv run mypy src tests  # strict type check
```

## Documentation

- [Strategy specification](docs/research/strategy-spec.md) — the active design, including
  an explicit "what is weak about this" section
- [Gotchas](docs/GOTCHAS.md) — seven verified Alpaca failure modes that do not announce
  themselves
- [Hackathon brief](docs/research/hackathon-brief.md) — rules, revalidated at kickoff
- [Alpaca platform notes](docs/research/alpaca-platform.md) ·
  [revalidation](docs/research/revalidation-alpaca-2026-08-28.md) ·
  [tooling](docs/research/revalidation-tooling-2026-08-28.md)

Superseded designs are kept rather than deleted, because why an idea was rejected is
worth as much as why one was chosen:
[v0 Catalyst Convexity](docs/research/strategy-spec-v0-catalyst-convexity.md) ·
[v1 Rotunda disclosure tilt](docs/research/strategy-spec-v1-rotunda-disclosure.md) ·
[congressional disclosure research](docs/research/signal-sources-congress.md) ·
[SEC Form 4 research](docs/research/signal-sources-form4.md)

## Guardrails

- **Paper trading only.** The trading host is a property with no branch that reaches the
  live API, and `ALPACA_LIVE_TRADE=true` refuses to start the process.
- Execution is limited to defined-risk multi-leg positions. No naked short options, no
  averaging down, no 0DTE.
- Risk gates and exits are deterministic and never depend on an LLM response.
- The judged run uses a brand-new Alpaca paper account with exactly $100,000 equity.
- No API keys, account IDs, or other secrets belong in this repository.

## Data honesty

This project runs on Alpaca's free Basic plan for the whole contest: IEX equity data
rather than consolidated SIP, option trades delayed 15 minutes, and **indicative option
quotes rather than OPRA NBBO**. The ETF universe was chosen partly because penny-wide,
deeply liquid contracts are where an indicative quote is closest to the truth.

Paper multi-leg fills simulate against those modified indicative quotes, and the fill
model is undocumented. The official paper P&L is therefore a number we report, not a
number we trust — results are published beside a conservative shadow P&L with explicit
slippage assumptions.

## Licence

MIT — see [LICENSE](LICENSE).
