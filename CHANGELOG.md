# Changelog

Newest first.

## [Unreleased]

### Changed
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
