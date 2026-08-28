# August 28 kickoff checklist

Do not execute this checklist before the official kickoff unless the event schedule changes.

## 1. Revalidate the rules

- Reopen the official event page and record any changes to dates, eligibility, required technology, submission format, or judging.
- Recheck the linked Alpaca CLI, MCP, options, paper-trading, and market-data documentation.
- Confirm the exact interpretation of the competition account and performance window.

## 2. Establish the development environment

- Initialize version control and add an appropriate Python `.gitignore`.
- Create a Python 3.11+ virtual environment.
- Install pinned runtime and development dependencies.
- Install Alpaca CLI and inspect `alpaca --help-all`, relevant command help, and schemas.
- Install and read the relevant official Alpaca backtest and paper-trading skills.
- Add `.env.example`; never commit a real key or account identifier.
- Add formatting, linting, typing, unit-test, and pre-commit checks.

## 3. Verify paper-only access

- Use a development paper account initially; preserve the fresh competition account for the judged run.
- Run `alpaca doctor`.
- Verify the paper endpoint, account status, clock, buying power, and equity.
- Read both the approved and effective options trading levels; require effective Level 3 before spread execution.
- Detect the active equity and option data feeds and save the entitlement result.
- Prove that no configuration path can silently select live trading.

## 4. Build in thin vertical slices

1. Account, clock, and data-entitlement preflight.
2. Universe, news, and underlying features.
3. Typed AI catalyst classifier with schema validation and source attribution.
4. Option-chain selection and deterministic rejection reasons.
5. Central risk engine and kill switch.
6. Multi-leg paper-order adapter with client-order idempotency.
7. Trade-update stream, polling reconciliation, and durable journal.
8. Exit monitor and closing multi-leg orders.
9. Backtest/forward-validation artifacts and conservative shadow fills.
10. Dashboard, demo evidence, deployment, and submission assets.

## 5. Mandatory integration probes

- Submit and cancel a tiny, market-closed-safe paper order or otherwise use the least risky valid paper test available.
- Verify a two-leg debit spread payload and its explicit position intents.
- Confirm partial-fill, rejection, timeout, and duplicate-client-ID behavior.
- Reconcile REST order state, trade updates, positions, and activities.
- Verify that stale or missing quotes, Greeks, news, account state, and options permissions fail closed.
- Test restart recovery from the SQLite journal.
- Test the global no-entry kill switch.

## 6. Competition account transition

- Only when ready for the judged run, create the required brand-new paper account.
- Set and verify the starting balance is exactly $100,000 before any trade.
- Record its ID privately for submission and store credentials only in the deployment secret manager.
- Save an initial account snapshot and configuration fingerprint.
- Freeze the strategy/risk configuration before using judged performance as feedback.

## 7. Submission readiness

- Public repository with reproducible setup and architecture documentation.
- Hosted dashboard that does not expose secrets or privileged mutations.
- One-page AI logic/risk gates/Alpaca infrastructure write-up.
- Five-minute-or-shorter demo video.
- PDF slide deck and 16:9 cover.
- Performance report with official and shadow P&L plus limitations.
- Dedicated paper account ID, application URL, repository URL, descriptions, tags, and optional social links.

