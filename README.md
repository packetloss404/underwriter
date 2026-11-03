# Underwriter

An autonomous, paper-only options agent for the 2026 Alpaca AI Trading Agents Hackathon.

**Submission package:** [judge-facing artifacts, evidence, and the operator checklist](docs/submission/README.md)

**Self-hosted deployment:** [deployment and recovery instructions](docs/deployment-self-hosted.md).

**Open the dashboard:** [Overview](https://underwriter.ianlan.dev/) ·
[History](https://underwriter.ianlan.dev/history) ·
[Reports](https://underwriter.ianlan.dev/reports) ·
[Decision ledger](https://underwriter.ianlan.dev/ledger).

Selling an option is underwriting insurance: you collect a premium, take on a bounded
risk, and profit when nothing happens. Underwriter does exactly that, systematically —
and declines far more often than it writes.

## The edge

The strategy looks for **implied** option volatility above a trailing **realised**
volatility estimate. It treats that difference as a candidate premium for bearing tail
risk. A positive implied/trailing-realised ratio does not establish mispricing or a
profitable trade: future realised volatility, execution costs, and losses can erase it.
The thesis is volatility-risk-premium harvesting; this run is not a validation of it.

The agent ranks a fixed universe of 15 enabled ETFs (with four provisional
instruments excluded from live entries) by how rich implied volatility is
relative to realised, then sells defined-risk credit spreads where the premium is
richest. It cannot be done with stock; it exists only in options.

Time decay motivated the original four-session experiment. The strategy still has
directional and volatility exposure; a short judged window cannot establish its edge.

## How it works

```text
SCAN -> RANK -> REGIME -> VETO -> RISK -> EXECUTE -> MONITOR -> EXIT -> REVIEW
```

Nine stages with named refusal and failure reasons. Every refusal is recorded and displayed — a strategy whose rejections are
invisible is unexplainable.

- **Deterministic code owns every capital-affecting decision**: ranking, contract
  selection, pricing, sizing, risk gates, exits, and the kill switch.
- **The AI can only take risk off the table.** Its single job is to veto candidates
  whose elevated implied volatility has an identifiable cause — a scheduled event, a
  pending ruling. Broad macro announcements are advisory rather than veto triggers.
  It can remove candidates, never add them. A hallucinated catalyst
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

## Dashboard

The overview puts the account, entry posture, latest recorded book, and the full run
on one screen. History provides activity and equity charts, session and symbol
drilldowns, a filterable decision archive, and CSV exports. Reports explain strategy
refusals, operational failures, execution, reconciliation, regime decisions, and data
coverage with links back to the underlying records. The ledger provides the recent
decision tape and order evidence.

At **16:20 America/New_York on trading days**, the running service reads the
broker's final book/account state, reconciles orders, marks the official and
shadow series, and saves session reports. Failed refreshes retry every 15 minutes.
History and Reports show the completion date and automatically pick up a new
close report. The main overview keeps its four-second journal polling; confirmed
orders and fills appear as the agent records them.

Every dashboard route is read-only. Official broker figures, conservative shadow
marks, and exploratory observations retain their own sources. Stage approvals are
counted separately from orders and fills; absent observations remain unknown.

See [dashboard development](docs/dashboard-development.md) for local preview,
API contracts, deep links, data semantics, and browser acceptance checks.

## Status

The agent and dashboard run on owned infrastructure behind Caddy HTTPS. The journal
survives container rebuilds, and Compose restarts the service automatically. Railway
was retired after the September migration. The original hackathon submission material
remains in [the submission package](docs/submission/README.md) as a historical record.

The October 17 journal inspection covered August 31 through October 16: 47 calendar
days, 35 recorded days, 2,816 evaluation cycles, 32,592 decisions, and 6,951 P&L
snapshots. There were zero recorded orders or fills. Historical model connection
failures explain a substantial part of that inactivity; the blank model base URL
and intermittent API DNS failures were repaired on October 17. These are dated
observations, not a claim about the next market session or current account returns.

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
uv run pytest          # regression suite
uv run ruff check .    # lint
uv run mypy src tests  # strict type check
npm ci
npx playwright install chromium
npm run test:browser  # real Chromium against isolated FastAPI/SQLite fixtures
```

## Documentation

- [Submission package](docs/submission/README.md) — deck, cover, demo, evidence,
  performance-report template, and historical operator checklist
- [Dashboard development](docs/dashboard-development.md) — page ownership, local
  preview, data contracts, drilldowns, and visual acceptance checks
- [Self-hosted deployment](docs/deployment-self-hosted.md) — service operations,
  provider diagnostics, backups, release verification, and rollback
- [Strategy specification](docs/research/strategy-spec.md) — the active design, including
  an explicit "what is weak about this" section
- [Execution ingestion](docs/execution-ingestion.md) — real broker activities, historical
  reconciliation, provenance, and operator commands
- [Feature audit](docs/verification-2026-11-03.md) — the initial November 3 audit and
  the evidence that motivated execution ingestion
- [Gotchas](docs/GOTCHAS.md) — twenty documented Alpaca failure modes and tooling traps
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

The project targets Alpaca's free Basic plan. Historical equity-bar requests use
the SDK's default stock feed with a 20-minute embargo for historical SIP access,
and exclude the current Eastern session's incomplete daily bar. Option chains use
**indicative quotes rather than OPRA NBBO**; option trades are delayed on Basic. The ETF universe was chosen partly because penny-wide,
deeply liquid contracts are where an indicative quote is closest to the truth.

Paper multi-leg fills simulate against those modified indicative quotes, and the fill
model is undocumented. The official paper P&L is therefore a number we report, not a
number we trust. The shadow calculation applies repricing and execution-cost
adjustments to broker-derived P&L when confirmed fill-event coverage is complete.
The agent ingests genuine broker FILL activities and binds their child order IDs
to nested multileg orders. It preserves raw leg executions and derives explicitly
labelled balanced spread summaries; raw leg activity counts, derived groups, parent
orders, and spread units are separate quantities. Historical shadow snapshots remain
unvalidated observations, and incomplete new marks are withheld. See
[execution ingestion](docs/execution-ingestion.md) for reconciliation and provenance.

## Verification

Run the development checks above before a release. Dashboard coverage includes
read-only routes, credential redaction, complete historical counts, Eastern-time
and DST boundaries, stable archive pagination, and independent P&L sources.
The October 17 release recorded 1,160 passing tests; this audit initially reproduced
1,159 passes and one date-dependent retry test failure. The current reproducible
release checks and live observations are in the [November 3 feature audit](docs/verification-2026-11-03.md).
The real Chromium suite opens the application against isolated populated and empty
journals, exercises controls and downloads, and includes explicit failure scenarios.
`node tests/check_closeout_ui.cjs` remains a separate completion-race check.
Production recovery procedures live in the
[self-hosted runbook](docs/deployment-self-hosted.md).

The installed commands are `underwriter-serve` (dashboard **and trading supervisor**),
`calibrate` (read-only provider diagnostics), `underwriter-export` (finite static
snapshot), and `underwriter-ingest-executions` (read-only reconciliation plan by
default; `--apply` imports verified activity evidence). There is no generic `underwriter` command. The exporter is a labelled
snapshot and does not implement the live History/Reports query and pagination API.

The live integration record, including the accepted-and-cancelled multi-leg probe and
the composed Railway restart proof, is in
[docs/submission/integration-probes.md](docs/submission/integration-probes.md).

## Licence

MIT — see [LICENSE](LICENSE).
