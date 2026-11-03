# Changelog

Newest first.

## 2026-11-03 — Execution activity ingestion and reconciliation

- Added a bounded paper-only activity reader with real broker IDs/timestamps, exact
  numeric validation, complete cursor pagination, and nested child-order identity checks.
- Added atomic, idempotent raw leg ingestion and explicitly derived balanced spread
  summaries, including partial fills, late delivery, conflicting replays, and restart
  recovery. The normal cycle, post-submission path, and closing observation run the
  ingestor; failed or incomplete synchronization bars new entries while exits continue.
- Added a read-only reconciliation plan command and explicit `--apply`. The actual
  private rehearsal reconciled 60 raw leg activities with 26 filled parents/94 spread
  units and confirmed 13 historical position exits using exact source evidence.
  Reapplying produced zero new or changed execution records.
- Reports and Ledger distinguish raw broker leg activities, derived spread groups,
  cumulative parent fills, spread units, leg contracts, and latest reconciliation status.
- Existing schema-6 tables are reused. Historical P&L and closing artifacts remain
  recorded observations; genuine fills do not retroactively validate their old marks.
  See [execution ingestion](docs/execution-ingestion.md) for evidence and operations.

## 2026-11-03 — Feature audit and browser coverage

### Fixed
- Production wiring now bars unscreened entries without a model key; the broker's
  actual closed clock blocks new entries while observation and exit handling remain.
- Numeric risk/preflight gates reject non-finite inputs. Realized-volatility requests
  exclude the current incomplete Eastern daily bar.
- Web startup/runtime failure stops the supervisor and returns a failing process code;
  unexpected agent construction failures also stop the web thread.
- Public free-text fields redact credential-shaped values, and non-finite context JSON
  cannot break historical API serialization/queries.
- Static export now produces finite, self-contained snapshot pages with working local
  links, generation-time health labels, and explicit limits.
- Reports distinguish filled parent orders/cumulative spread units from execution-event
  rows, and expose incomplete confirmed-event coverage.
- Shadow P&L adjusts broker realized P&L rather than counting open premium twice; a
  $0.05-per-spread dollar cost no longer becomes $5. Incomplete event coverage withholds
  new shadow marks. Historical shadow snapshots are not rewritten or declared trusted.

### Added
- Real Chromium desktop/mobile tests for all four pages, dates/charts, archive scope and
  pagination, downloads, print invocation, specimens, empty/outage/recovery states,
  closing-receipt refresh, and static snapshots; separate read-only public smoke checks.
- A GitHub Actions verification workflow plus strategy-lifecycle and service-supervision
  regressions. [The dated audit](docs/verification-2026-11-03.md) records live evidence
  and distinguishes deterministic adapters from actual provider checks.

### Documentation and limits
- Corrected the enabled universe (15 plus four provisional), 10-session ranking window,
  20-session expansion context, six-position/3% aggregate default caps, advisory macro
  policy, exit timing, and historical stock-feed assumptions.
- Removed the console entry point referencing a nonexistent module. The supported
  commands remain `underwriter-serve`, `calibrate`, and `underwriter-export`.
- No execution-event/stream ingestor, automated rolling, baseline backtest comparisons,
  or dynamically adjusted early-close flatten cutoff is implemented. Missing execution
  evidence may prevent a closing job from finalizing both sources; this is surfaced as
  an incomplete observation rather than a fabricated result.

## 2026-10-17 — Dashboard and self-hosted operations

### End-of-day reporting
- The running supervisor performs a broker observation and report publication
  at 16:20 Eastern on trading days, with calendar-based holiday skips, durable
  completion markers, same-day restart catch-up, and 15-minute failure retries.
- History and Reports show closing-job status and pick up newly completed
  reports automatically; the main overview retains live journal polling.
- Closing observations do not scan, screen, open or close positions, or invent
  an opening-equity baseline. Incomplete book or P&L reads cannot be finalized.
- Broker and market-data readers use finite network timeouts. Unreadable order
  lookups remain unresolved instead of being mistaken for confirmed absence.

### Added
- Full-history activity, account-equity, session, and symbol readouts with
  Eastern-time date ranges, CSV exports, and a stable paginated decision archive.
- A reports workspace covering strategy refusals, operational failure impacts,
  execution, reconciliation, regime decisions, and source-specific data coverage.
- A data-led overview with full-run context, coherent navigation across four
  dashboard destinations, and drilldowns into recorded evidence.
- Docker Compose deployment and recovery instructions for owned infrastructure,
  plus dashboard development and visual acceptance documentation.

### Fixed
- Blank model base URL settings no longer become invalid OpenAI client URLs;
  nonblank overrides require an HTTP(S) scheme and host.
- Container API lookups use explicit public DNS after intermittent LAN resolver
  failures affected Alpaca requests.
- Dashboard labels distinguish browser API connectivity, recorded observations,
  scheduled market closures, and current trading eligibility.
- The ledger reads current API envelopes and their actual field names.

### Changed
- README and operational documentation describe the active self-hosted service;
  contest plans and Railway records are retained with historical context.
- Historical analytics keep live-strategy and exploratory records distinct and
  distinguish stage decisions from broker orders and fills.

## [Unreleased]

### Added
- Complete non-secret submission package: judge-facing index, balanced eight-slide
  deck in editable PPTX and PDF, 16:9 cover, performance-report template,
  copy-ready portal text, and a single operator-only checklist.
- Current-main verification record: 1,107 tests pass, Ruff is clean, and strict
  mypy succeeds across 48 source files.
- Four more catalyst-veto providers: DeepSeek, OpenRouter, MiniMax and Featherless.
  All speak the OpenAI wire format, so each is a row in `COMPATIBLE_ENDPOINTS`
  (base URL, default model, key variable) rather than a client class. Anthropic
  and OpenAI stay wired direct to their own vendors.
- `UNDERWRITER_MODEL_BASE_URL`, an escape hatch for any OpenAI-compatible endpoint
  with no row in the table -- a self-hosted server, a proxy, a vendor added later.
  Refused for Anthropic, which cannot honour it: a startup error beats a setting
  that silently does nothing.
- `.env.example` documents all six providers, the `auto` preference order, and the
  default model name for each.

### Changed
- README and demo script now describe the completed execution, dashboard, and
  restart-recovery paths, use the real supervisor workflow, and report current
  evidence counts instead of the early-build 209/982-test figures.
- Test fixtures use precise typed argument shapes so strict mypy checks the whole
  suite without weakening runtime assertions.
- `auto` selection now follows a published order (`PROVIDER_PREFERENCE`) instead of
  an incidental one, so which model screened a trade is reproducible from the
  environment alone. Anthropic still wins when several keys are present.
- The chosen provider and model are logged by `build_veto`, which knows them for
  certain, rather than read back off the one-method `ModelClient` protocol.
- **Renamed to Underwriter.** Selling an option is underwriting insurance: collect a
  premium, take bounded risk, profit when nothing happens. The earlier name pointed at
  the Capitol, which stopped describing the strategy two revisions ago. Package
  `rotunda` -> `underwriter`, env prefix `ROTUNDA_` -> `UNDERWRITER_`. Archived specs
  keep their historical names.
- **Strategy re-specified again (2026-08-28): volatility risk premium harvesting.**
  Both disclosure concepts sought a directional edge on a four-session clock, and
  both died on the same arithmetic -- congressional PTRs yield ~9-10 filings across
  the window against a documented +12-18bps effect, and switching to Form 4 fixed
  density but not the multi-month horizon. The premium between implied and realised
  volatility decays daily, so the horizon problem disappears rather than being
  managed. v0 and v1 specs archived.
- **Project re-specified as Rotunda** (2026-08-28): congressional-disclosure sector
  tilt expressed through defined-risk ETF verticals, replacing the v0 single-name
  news-catalyst concept. Reasoning in `docs/research/strategy-spec.md`; v0 archived.
- Python package renamed `catalyst` -> `rotunda`; env prefix `CATALYST_` -> `ROTUNDA_`.
- Event rules revalidated against the live page at kickoff. Prize pool is $6,300;
  Algo Trader Plus goes only to social-prize winners, so Basic market data is
  permanent for this build; MIT licence is a stated requirement.

### Added
- Aggregate net-delta cap in `rotunda.risk`. A put credit spread is net long
  delta, so individually compliant positions stack into one directional bet that
  per-position gates cannot see. The cap bounds magnitude rather than sign, nets
  opposing exposure off, and scales with equity.
- `rotunda.regime`: global no-entry gate for the correlated short-premium book.
  Benchmark trend, sharp-drawdown, universe-wide volatility-expansion, and
  scheduled-event checks. Blocks accumulate rather than short-circuit; missing
  history blocks rather than permits. Only ever blocks entries, never forces
  liquidation, since a forced exit into a disorderly tape is its own risk.
- `rotunda.volatility`: realised-vol measurement, implied-vs-realised premium
  ranking across the universe, and displayable skip reasons. Ranks on the ratio
  rather than the difference because the universe spans very different absolute
  vol levels. Never estimates a missing implied vol. The realised-expansion warning
  requires a configurable margin, since two sample deviations over different window
  lengths differ by a few percent on a stable series and a bare comparison would
  flag expansion at random.
- `rotunda.chain.select_credit_vertical`: short-premium vertical construction with
  inverted economics (credit is max profit, width minus credit is max loss), a
  credit-as-fraction-of-width band, and a conservative credit that assumes crossing
  half of each leg's quoted spread in the unfavourable direction on both legs.
- `rotunda.preflight`: fail-closed gate covering the paper-only guarantee, kill
  switch, account status and blocks, equity, effective options level, options buying
  power, market clock, and Alpaca CLI availability. Gates on `options_trading_level`
  (effective) rather than `options_approved_level`, and warns when configuration caps
  the approved level. An unreadable value is always FAIL, never a silent pass.
- `rotunda.universe`: 16 liquid index/sector ETFs with a sector map and a
  correlation map, so three "independent" positions cannot quietly be one bet.
- `rotunda.chain`: expiry-window construction that cannot omit a bound, contract
  screening with displayable rejection reasons, and defined-risk vertical
  construction with a conservative debit that assumes crossing half of each leg's
  quoted spread. Falls back to deterministic moneyness when Greeks are absent and
  refuses outright when neither delta nor spot is available; never fabricates a Greek.
- `rotunda.risk`: central risk engine. Per-trade sizing that floors rather than
  rounds, concurrent-position cap, duplicate and correlated-exposure gates,
  aggregate open-risk cap, options buying power, session entry cutoff, and a daily
  loss stop measured against session-open equity where unrealised losses count but
  unrealised gains cannot unlock it. Evaluates every applicable gate rather than
  short-circuiting, so the audit log shows all reasons a trade was refused.
- `docs/GOTCHAS.md`: seven verified failure modes that do not announce themselves.
- MIT `LICENSE`.
- Alpaca CLI v0.0.14 installed; `--legs`, `--dry-run`, `--client-order-id` confirmed
  present, so the CLI can sit on the real order path.
- Repository scaffold: git, `.gitignore`, `.env.example`, `pyproject.toml`
  (Python 3.12, ruff, mypy strict, pytest), `BACKLOG.md`, `CHANGELOG.md`.
- Python 3.12.14 virtual environment; `alpaca-py` 0.44.0 resolves cleanly.

### Notes
- 2026-08-28: hackathon kickoff. Implementation unblocked; research dossier
  from 2026-08-26 under revalidation.
