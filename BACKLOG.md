# Backlog

Roughly ordered. Kickoff was 2026-08-28 10:00 CDT; submission is due
2026-09-04 10:00 CDT, so the live-market window is about 4.5 sessions.

## Blocked on the operator

- [ ] Alpaca **development** paper account: create it and put its key/secret in `.env`.
- [ ] Anthropic API key for the catalyst classifier in `.env`.
- [ ] Public GitHub repository + SSH `origin` (no `gh` CLI installed on this box yet).
- [ ] Decide where the agent and dashboard are hosted for the judged run.
- [ ] Alpaca **competition** paper account — brand new, exactly $100,000, created
      only when the judged run is ready. Credentials never enter this repo.

## 0. Kickoff revalidation

- [ ] Confirm event rules, deadline, and judged performance window against the live page.
- [ ] Confirm Alpaca CLI install, env vars, and whether it can submit multi-leg orders.
- [ ] Confirm options level 3 semantics and the multi-leg REST payload shape.
- [ ] Install and read the official `alpacahq/alpaca-skills`.
- [ ] Amend `docs/research/` wherever the dossier is now wrong.

## 0.5. Calibration — run first, the moment credentials exist

- [ ] `uv run calibrate` against the dev account.
- [ ] Confirm the regime filter permits entry on a workable share of sessions.
- [ ] Confirm something clears the premium floor; lower it if nothing does.
- [ ] Promote or drop USO, SLV, FXI, EWZ on measured option liquidity.
- [ ] Record the observed term-structure ratio; the 1.0 threshold assumes
      contango sits near 0.85-0.95 and that has never been verified.

## 1. Preflight (thin slice 1)

- [ ] Typed settings loader; fail closed when credentials are absent.
- [ ] Assert the paper endpoint and prove no config path can select live trading.
- [ ] Account, clock, buying power, equity checks.
- [ ] Read approved vs effective options level; require effective level 3 for spreads.
- [ ] Detect active equity and option data feeds; persist the entitlement result.
- [ ] `alpaca doctor` wired into the same preflight report.

## 2. Signal path

- [ ] Universe construction: liquid, optionable, small enough for Basic-plan limits.
- [ ] Movers + news collection with persisted source identifiers.
- [ ] Underlying features: opening range, VWAP, relative volume, RS vs SPY.
- [ ] Typed AI catalyst classifier with schema validation and source attribution.
- [ ] Deterministic confirmation gate.

## 3. Execution path

- [ ] Option-chain selection with explicit, recorded rejection reasons.
- [ ] Central risk engine and global kill switch.
- [ ] Multi-leg paper-order adapter with client-order-ID idempotency.
- [ ] Trade-update stream + polling reconciliation + durable SQLite journal.
- [ ] Exit monitor and closing multi-leg orders.

## 4. Evidence

- [ ] Backtest / forward-validation artifacts with conservative shadow fills.
- [ ] Four-baseline comparison from the strategy spec.
- [ ] Live dashboard: state, candidates, rejections, thesis, gates, P&L, kill switch.

## 5. Submission package

- [ ] Public repo with reproducible setup and architecture docs.
- [ ] Hosted dashboard URL, secret-free and without privileged mutations.
- [ ] One-page AI logic / risk gates / Alpaca infrastructure write-up.
- [ ] Demo video, five minutes or less.
- [ ] Slide deck PDF and 16:9 cover image.
- [ ] Performance report: official P&L, shadow P&L, limitations.
- [ ] Competition paper account ID, app URL, repo URL, descriptions, tags, socials.

## Integration probes (must all pass before the judged run)

- [ ] Market-closed-safe tiny paper order submit + cancel.
- [ ] Two-leg debit spread payload with explicit position intents.
- [ ] Partial fill, rejection, timeout, duplicate client-order-ID behavior.
- [ ] Reconcile REST orders, trade updates, positions, activities.
- [ ] Stale/missing quotes, Greeks, news, account state, options permission all fail closed.
- [ ] Restart recovery from the SQLite journal.
- [ ] Global no-entry kill switch.
