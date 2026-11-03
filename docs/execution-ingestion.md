# Execution-activity ingestion and reconciliation

This release ingests Alpaca paper-account FILL activities through the read-only
account-activities endpoint and joins their child order IDs to nested broker orders.
The broker's activity ID and transaction timestamp identify each raw execution;
parent order status, cumulative quantity, and average price remain separate evidence.
See [Alpaca account activities](https://docs.alpaca.markets/us/docs/account-activities)
and the [activity API reference](https://docs.alpaca.markets/us/reference/getaccountactivities-2).

## Broker evidence inspected on November 3

The private pre-import capture contains 28 journalled/broker parent orders: 26 filled
and two expired. The filled parents comprise 13 opening and 13 closing orders, with
94 cumulative spread units across both directions. The broker returned 60 distinct
FILL activities attached to 52 nested child orders. All 60 match the journal's exact
option contracts, directions, and leg ratios. Their signed cash flows exactly match
all 26 broker parent average-price/filled-quantity totals.

Four parents have partial executions, so 60 raw leg activities must not be described
as 26 native parent execution events. The implementation retains every raw activity
and derives balanced spread summaries for accounting. A derived summary is explicitly
labelled with its constituent broker activity IDs and its latest component execution
time. It does not invent a broker-issued multileg execution identifier or assume
that two leg timestamps were identical.

The original journal also contains 13 inferred, unexplained position exits. Each has
one exact closing-order match using the source held-position snapshot, contracts,
opposite directions, quantities, and execution times within its disappearance window.
Confirmation preserves the original observation and attaches real activity evidence.
Matching by underlying symbol and date alone is insufficient.

Raw broker captures and SQLite backups are private ignored artifacts. No account
credentials, account identifiers, or unsanitized broker capture belongs in git.

## Normal operation

The production agent polls execution activities during normal reconciliation and
closing observations. It uses a separate paper-only SDK reader with bounded network
timeouts, genuine broker page cursors, and explicit completion checks. Activity query
date filters and execution timestamps have different meanings; late records retain
their actual execution date.

Raw leg prices are positive premiums and their quantities are contracts. Derived
parent prices are signed net premiums and their quantities are spread units. Matching
uses the broker's nested child-order identity plus the journal's intended symbols,
sides, and ratios. A symbol match by itself cannot establish ownership.

Duplicate deliveries cannot add contracts twice. Conflicting facts under the same
broker activity ID, missing legs, unbalanced quantities, incomplete pagination, or
broker/journal disagreement prevent a successful reconciliation. New entries require
successful accounting evidence; observation and exit handling continue during a read
failure. Check the latest ingestion receipt separately from a historical date filter.
Consumers assessing account-wide reconciliation need both selected record coverage
and the latest account-wide synchronization to be complete; a rejected read can leave
old recorded quantities internally consistent while the current broker check failed.

## Inspect and apply

The operator command defaults to a read-only plan. `--apply` persists the validated
records atomically. It reads broker endpoints and never submits, cancels, or changes
an order. Use the existing container's environment for the production paper account;
do not start a second `underwriter-serve` instance.

```bash
underwriter-ingest-executions --journal /data/underwriter.db
underwriter-ingest-executions --journal /data/underwriter.db --apply
```

The module form is equivalent:

```bash
uv run python -m underwriter.execution_ingestion --journal artifacts/preview/underwriter.db
```

For an isolated rehearsal, `--records` accepts the private broker capture used by the
reconciliation tests. Use a disposable copy of the online journal backup; never edit
the immutable original backup or publish its contents. Production reconciliation
reads fresh broker records rather than relying on the rehearsal capture.

Before applying to production, retain the current image, source revision, an online
SQLite backup, and table counts. Review the plan, apply it once, and repeat the plan
and application to verify idempotence. Check exact raw IDs/timestamps against the
broker, parent/leg quantities and net cash flows, resolved exit evidence, preserved
order/decision/P&L counts, and SQLite integrity. The
[self-hosted runbook](deployment-self-hosted.md) describes deployment and rollback.

## Accounting limits

Execution coverage establishes that the journal contains the retrieved broker
execution evidence. It does not retroactively certify old shadow snapshots or the
paper simulator's fill quality. This release preserves historical P&L observations;
it does not backdate a fresh account reading or silently replace historical marks.
The shadow series remains an adjustment to broker-derived daily equity observations,
using the recorded limit, execution-price differences, and an explicit dollar cost.

This is polling-based REST ingestion. A trade-update websocket is not required for
correctness and is not claimed as implemented. Assignments, exercises, expiration,
and other non-FILL activities require their own explicit evidence paths; a FILL
import must not relabel them as strategy-initiated closes.


## Rehearsal against the actual journal

An online production backup was copied privately and verified by SHA256 before
rehearsal. The read-only plan reported 28 verified parents, 26 filled parents,
60 new raw leg records, 26 new derived groups, 94 spread units, and no discrepancies.
Applying that plan to a disposable copy confirmed all 13 previously unexplained exits.
A second application inserted zero leg records/groups, updated zero groups, and
confirmed zero additional exits.

An independent comparison verified every raw broker activity ID, original transaction
timestamp, quantity, and price. Every source activity belongs to exactly one derived
group, the anchor is a genuine constituent ID, the summary time is its latest component
time, and all signed cash flows agree. Hashes of all rows in orders, intended legs,
decisions, P&L, position snapshots, and held positions were unchanged. Each exit retained
its original detection time, source snapshot, symbol, quantity, and exchange date.
SQLite integrity passed, and the immutable backup checksum remained unchanged.

The ingestor treats a zero-priced option leg as pending under the existing schema-6
positive-premium constraint. It retains unresolved checkpoint evidence and does not
advance past an unpersisted activity; it never replaces zero with a fabricated price.
Incomplete pages and conflicting identities likewise cannot advance the successful
checkpoint past unresolved evidence. The inspected production records contain no
zero-price or fractional-contract fills.


## Release checks

The final integrated local suite passed **1,315 Python tests**, Ruff, strict mypy
across **64 source/test files**, both closing-refresh race checks, and whitespace
checks. The real Chromium suite passed **50 cases** (25 desktop and 25 phone) with
zero retries, including production ingestion through SQLite/HTTP, duplicates, restart,
partial and pending records, source identities/times, and conflict handling. The clean
hosted CI workflow repeats the Python and browser checks before deployment.

Backend regressions cover strict nested intents and per-leg prices, 105 historical
parents, late earlier-sorting activity IDs retaining their aggregate anchor, partial
page restarts, unknown/conflicting cursor holds, transaction rollback, zero/missing
quantities, and never-submitted abandoned intents. The production-wiring lifecycle
uses the actual activity reader and ingestor, with GET-only broker fixtures rather
than manually inserting parent fill rows. It proves that a read outage blocks a
viable new entry while a protective exit still executes, and that a failed closing
report succeeds after genuine fixture activity delivery resumes.
