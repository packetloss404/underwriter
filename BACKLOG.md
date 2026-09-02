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

## Blocked on the operator — current

- [ ] **ANTHROPIC_API_KEY** in `.env` and in Railway. The catalyst veto is
      built and tested but runs unwired without it, and the agent logs a
      warning rather than pretending to screen.
- [ ] Flip `UNDERWRITER_DRY_RUN` to false once a market-hours cycle is watched.

## Deployment — decided 2026-08-29

Railway, ONE service, for the contest. Moving to owned hardware afterwards.

- [x] `.python-version` pinned to 3.12 — Railpack detects uv but defaults to
      3.13.2, and we would silently get an interpreter nothing here has run on.
- [x] `railway.json` with `restartPolicyType: ALWAYS` — the documented default
      is contradictory and a crashed deploy can stay crashed.
- [x] `underwriter-serve`: dashboard and agent loop in one container, because a
      volume binds to exactly one service and they must share the journal.
- [x] Static export as the fallback if Railway disappoints.
- [ ] Wire the agent loop to a live broker (the supervisor currently idles).
- [ ] Create the Railway project, attach a 1 GB volume at `/data`, set the
      secrets, generate the public URL.
- [x] **Assume an arbitrary restart mid-window** — the deployed-container probe
      proves Railway remounts the same volume; the deterministic four-deployment
      regression proves SQLite restores live and exploratory open state, refuses
      a duplicate close, drains the confirmed consequence once, and leaves no
      stranded order or position event. See `tests/test_restart_recovery.py` and
      `docs/submission/integration-probes.md`.
- [ ] Do NOT enable Serverless on the service; it sleeps after ~5-10 min idle
      and first requests may 502.

## 0.5. Calibration — run first, the moment credentials exist

- [x] `uv run calibrate` against the dev account. Preflight passes all ten checks;
      effective options level is 3; equity exactly $100,000.
- [x] Regime filter permits entry on 59% of 59 real SPY sessions. Well calibrated.
- [x] Removed ITA: monthly expiries only, nearest 20 days out, so nothing ever
      lands in the 5-14 day window.
- [ ] **Re-run at market open Monday.** Every liquidity number so far is an
      after-hours reading, where sector SPDR spreads run 40-67% and almost
      nothing is tradeable. These will tighten dramatically at the open and the
      provisional promote/drop calls cannot be made until they do.
- [ ] Decide USO / FXI / EWZ on market-hours data. SLV is a clear promote
      (6.0% spread, 54% of near-the-money strikes tradeable).
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
- [x] Restart recovery from the SQLite journal — combined Railway remount and
      deterministic open-state replay proof recorded in the submission evidence.
- [ ] Global no-entry kill switch.
