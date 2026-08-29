# Adversarial review — `src/underwriter/journal.py`

**Reviewed:** 2026-08-28
**Scope:** `src/underwriter/journal.py` (2505 lines), `tests/test_journal.py` (1457 lines)
**Cross-read:** `docs/GOTCHAS.md` (#8, #9, #10, #12, #13, #14), `docs/research/reconciliation-reference.md`
(§3, §6, §7, §8, §9), `src/underwriter/risk.py`, `src/underwriter/execution.py`
**Baseline:** all 130 tests in `tests/test_journal.py` pass. No files were modified.

Every claim below was reproduced against the running code. Reproduction scripts are in the session
scratchpad (`probe1.py` … `probe5.py`); each finding carries the snippet that produced it.

---

## Summary

| Rank | Count | Findings |
|---|---|---|
| CRITICAL | 3 | C1 one-shot position diff, C2 unguarded fill ledger, C3 day-scoped recovery gaps |
| HIGH | 7 | H1–H7 |
| MEDIUM | 8 | M1–M8 |
| LOW | 1 | L1 (test coverage) |

The module's design reasoning is sound and unusually well documented, and the parts that are guarded
are guarded correctly. The pattern in the failures is consistent: **the guards are applied to the
audit-trail columns and not to the columns that feed risk and P&L.** `mark_status` refuses a leg
contract count; `record_spread_fill` accepts one. `mark_status` is monotonic; `record_position_snapshot`
validates nothing. Recovery refuses to invent a realised P&L; it will happily hand back a three-day-old
book with no complaint.

---

## Verified correct — hand-checked, not assumed

These are worth stating plainly because they are the load-bearing claims and they hold.

### Sign-correct P&L

`spread_realised_pnl` (`journal.py:323`) computes `-(open_net + close_net) * 100 * spreads`. Checked by
hand and by execution across every case in the brief plus two more:

| Case | open | close | spreads | Result | Expected |
|---|---|---|---|---|---|
| Credit spread closed for a profit | −1.20 | +0.40 | 1 | **+80.00** | +80.00 ✅ |
| Credit spread expires worthless | −1.20 | 0.00 | 1 | **+120.00** | +120.00 ✅ |
| Credit spread closed at a loss | −1.20 | +3.00 | 1 | **−180.00** | −180.00 ✅ |
| **Debit** spread sold back higher | +2.00 | −3.50 | 1 | **+150.00** | +150.00 ✅ |
| **Debit** spread expires worthless | +2.00 | 0.00 | 1 | **−200.00** | −200.00 ✅ |
| Credit spread, 5 spreads | −1.20 | +0.40 | 5 | **+400.00** | +400.00 ✅ |

The docstring's reasoning is correct: the negated-sum form applies the sign convention once, to both
sides identically, and survives a debit spread where a `credit - debit` form would silently invert.
`SpreadFill.credit_received` (`journal.py:445`) and `realised_pnl_from_fills` (`journal.py:340`) use the
same convention consistently, and `realised_pnl_from_fills` multiplies per-fill `spreads` before summing,
so partial fills at different net prices aggregate correctly.

One caveat, not a bug: the expiry case is only right if the caller passes `close_net_price=0.0`
explicitly. There is no expiry helper, and `realised_pnl_from_fills` over an expired position sees only
the opening fill and returns `+120` — which happens to be the right answer for an expiry and the wrong
answer for a position still open, with nothing in the data distinguishing the two. The docstring warns
about this; nothing enforces it.

### Durability pragmas

Read back live from a file-backed database:

```
journal_mode      = wal
synchronous       = 2      (FULL)
foreign_keys      = 1
busy_timeout      = 5000
wal_autocheckpoint = 1000
```

`synchronous=FULL` in WAL mode fsyncs the WAL on every commit, so a power cut cannot lose the most
recent committed transaction — which is exactly the order intent written moments before submission.
This is the right choice and the docstring's reasoning at `journal.py:1096-1109` is correct. The WAL-mode
assertion at `journal.py:1126` correctly refuses a file-backed database that will not enter WAL (network
filesystems), rather than running on a guarantee it does not have.

### Transactions

`_transaction` (`journal.py:1134`) uses `BEGIN IMMEDIATE` with `isolation_level=None`, taking the write
lock up front. Every multi-statement write that matters is inside one — including the snapshot write
(`INSERT` into `position_snapshots` + `executemany` into `snapshot_positions`), which I confirmed rolls
back atomically when the `executemany` fails partway. `_create_schema` (`journal.py:1179`) correctly
relies on Python 3.12's `executescript` not performing implicit transaction control, so the schema and
its version row commit together — a half-created database that could not state its version is not
reachable.

### Write-ahead crash windows

Three of the four windows in the brief reconstruct the truth:

- **After intent write, before submit** — order sits at `INTENT` with `submitted_at=NULL` and
  `reconciled_at=NULL`, returned by `unreconciled_orders()` on every restart. Verified across a real
  close-and-reopen of a file-backed database.
- **During submit** (broker received it, we died) — indistinguishable from the above at the journal
  level, which is the point: recovery looks the order up by `client_order_id` (GOTCHAS #9, #16) and
  learns the truth from the broker.
- **After submit, before the status write** — same.

The fourth window (fill arrives, process dies before it is recorded) is **not** clean; see H1.

### Idempotency of the keyed writers

- `record_intent` with the same id and payload returns the original without a second row; with a
  different payload, symbol, or size it raises `ConflictingIntentError` (`journal.py:1382`). Key ordering
  in the payload dict does not create a false conflict (`_dumps_obj` sorts keys).
- `record_spread_fill` and `record_leg_fill` are keyed on the broker's execution id; a repeat returns
  `False` and does not double-count.
- `confirm_position_event` is idempotent on `activity_id` (UNIQUE at `journal.py:755`), so an activity
  feed can be replayed freely.
- All three hold **across separate connections** — verified with two live `Journal` objects on one file,
  which is the realistic shape (stream handler and reconciling poller).

### Status mapping fails closed

`OrderStatus.from_broker` (`journal.py:197`) maps every ambiguous Alpaca status (`replaced`,
`done_for_day`, `stopped`, `calculated`) to `UNKNOWN`, which is deliberately **not** in
`_TERMINAL_STATUSES`, so an ambiguous order stays on the reconciliation list. Correct.

---

## CRITICAL

### C1 — `vanished_positions()` is transient state with no cursor; one missed consumption loses an assignment permanently

**Location:** `journal.py:1962-1994` (`vanished_positions`), `journal.py:1974`
(`books = self.recent_position_books(2)`)

**What breaks.** The diff exists only while the disappearance sits between the two *newest* snapshots.
There is no persisted "last diffed snapshot id" cursor, nothing marks a snapshot as consumed, and
`recover()` (`journal.py:2350-2472`) never calls `vanished_positions()` at all. Record one more snapshot
before the diff is consumed and the event is gone from the journal forever.

**Reproduction.**

```python
j = Journal()
j.record_position_snapshot([PositionRecord(symbol="SPY", spreads=5.0, max_loss=1500.0)], at=T(0))
j.record_position_snapshot([], at=T(1))   # SPY assigned — the position stops being listed
j.record_position_snapshot([], at=T(2))   # next monitoring cycle, before the diff was consumed

j.vanished_positions()
# -> ()          <-- the assignment is now unrecoverable from the journal
```

**Concrete scenario.** The monitor records snapshot N+1 at 14:01, and the process is killed (OOM,
deploy, `docker stop`, the 3am restart this module is written for) before it writes the `PositionEvent`.
On restart the agent records snapshot N+2 and sees nothing wrong. An assigned short put — now a real
equity position with unbounded risk and no defined-risk hedge — is invisible to the journal, to
`unexplained_position_events()`, and to `recover()`.

This is the single most serious finding because it defeats the module's own stated primary defence.
`GOTCHAS.md` #12 is explicit: *"the only same-day signal is the position quietly disappearing from
`GET /v2/positions`"*, and `journal.py:33-40` builds the whole position subsystem on that sentence. That
signal is the one piece of state in the module that is not durable.

**Suggested fix.** Make the diff a durable, cursor-driven operation rather than a view over the newest
two rows:

1. Add `position_snapshots.diffed_at TEXT` (or a `diff_cursor` single-row table holding the last
   snapshot id that was successfully diffed and acted on).
2. `vanished_positions()` diffs *from the last undiffed snapshot forward*, not from `LIMIT 2`, and
   returns everything found across the whole undiffed run.
3. Add an explicit `mark_snapshots_diffed(up_to_id)` that the caller invokes only after the resulting
   `PositionEvent` rows are committed — ideally in the same transaction as those writes.
4. Add a `RecoveryGap.UNDIFFED_SNAPSHOTS` and have `recover()` report any snapshot recorded but never
   diffed.

---

### C2 — The unit-confusion guard does not exist on the fill ledger

**Location:** `journal.py:1614-1660` (`record_spread_fill` validation), versus
`journal.py:1548-1578` (`_checked_filled`)

**What breaks.** The three guards claimed in the module docstring (`journal.py:42-55`) protect the
`orders` row and the `leg_fills` row. The `spread_fills` row — the one every P&L read goes through — is
validated only for finite-and-positive. The exact numbers `mark_status` refuses are accepted one call
earlier by `record_spread_fill`.

**Reproduction.**

```python
j.record_intent(client_order_id="A", cycle_id="c", symbol="SPY", spreads=5.0, payload={"q": 5})

j.record_spread_fill(
    fill_id="f1", symbol="SPY", trading_day=date(2026, 8, 28),
    spreads=10.0,                # <-- CONTRACTS (2 legs x 5 spreads), not spreads
    net_price_per_spread=1.20,   # <-- a LEG premium: positive, on a credit OPEN
    occurred_at=..., source=FillSource.REST, client_order_id="A")
# -> True.  attribution = journalled.  credit_received = -1200.00

j.mark_status("A", OrderStatus.FILLED, spreads_filled=10.0)
# -> UnitConfusionError: 'A' ordered 5 spread(s) but was reported filled for 10.
```

The truth is **+600 credit received on 5 spreads**. The journal records **−1200**, a $1,800 error in the
wrong direction, attributed to a legitimate order of ours, and marked confirmed.

**Concrete scenario.** A reconciliation pass reads `GET /v2/orders?nested=true` and iterates the `legs`
array instead of the parent — the exact confusion `GOTCHAS.md` #8 exists to warn about, and the reason
`nested=true` is called out there as mandatory. Every leg-derived figure lands in `spread_fills` without
complaint. `realised_pnl_from_fills` then doubles the quantity and inverts the sign, and
`SpreadFill.credit_received` reports a $1,200 debit where $600 was received.

Three specific holes, all in the same writer:

1. **No cross-check against the named order.** `_attribute` (`journal.py:1690`) already does a
   `SELECT` on `orders` for this exact `client_order_id`; `spreads_ordered` is right there in the row and
   is not compared against.
2. **No sign or magnitude check on `net_price_per_spread`.** The order's `payload` carries the signed
   `limit_price` we submitted (`-1.20`); nothing compares the fill's net against it, so a sign inversion
   or a 10× magnitude error passes silently.
3. **No cumulative check.** Nothing prevents `sum(spread_fills.spreads)` for an order exceeding its
   `spreads_ordered`.

**Suggested fix.** Move the `_checked_filled` logic behind a shared validator and apply it in
`record_spread_fill` whenever `client_order_id` resolves to a journalled order:

- Refuse `spreads` when `sum(existing fills) + spreads > order.spreads_ordered`, with the same
  contracts-vs-spreads message.
- Warn (into `detail`) or refuse when `sign(net_price_per_spread)` contradicts the sign of the
  submitted `limit_price` in the order payload for an opening action.
- For `attribution != JOURNALLED` fills, where no order exists to check against, keep the current
  permissiveness — that is correct per GOTCHAS #14 — but record the absence of a check in `detail`.

---

### C3 — Recovery scopes unconfirmed and unattributed fills to today, so an overnight restart reports clean

**Location:** `journal.py:2431` (`unconfirmed = self.unconfirmed_fills(trading_day=trading_day)`) and
`journal.py:2440` (`unattributed = self.unattributed_fills(trading_day=trading_day)`)

**What breaks.** Both recovery gaps are filtered to the trading day being recovered. Anything left
half-known from a previous day is invisible to `recover()`, and `is_clean` returns `True`.

**Reproduction.**

```python
j.record_session_open_equity(trading_day=date(2026, 8, 28), equity=100_000.0)
j.record_reconciliation(scope=ReconciliationScope.FULL, ok=True, at=now)
j.record_position_snapshot([], at=now)

# yesterday: a stream-only fill nobody ever confirmed, and a broker sell-out
j.record_spread_fill(fill_id="y1", trading_day=date(2026, 8, 27), source=FillSource.STREAM, ...)
j.record_spread_fill(fill_id="y2", trading_day=date(2026, 8, 27), source=FillSource.REST,
                     client_order_id=None, ...)   # broker-initiated

j.recover(date(2026, 8, 28), now=now)
# -> gaps: ()          is_clean: True
# while  j.unconfirmed_fills()  == 1 fill
#        j.unattributed_fills() == 2 fills
```

**Concrete scenario.** The stream dropped near yesterday's close (GOTCHAS #13: no cursor, every
disconnect is a definite gap), leaving a fill recorded but unverified, and Alpaca's pre-expiry sell-out
(GOTCHAS #10) landed on an order id we never created. The agent restarts the next morning. `recover()`
declares a clean bill of health and the agent starts trading with an unverified fill and an unexplained
broker liquidation in its book.

This directly contradicts `journal.py:26-28`: *"`unconfirmed_fills()` and the `UNCONFIRMED_FILLS`
recovery gap exist so that 'we heard about this on a socket and never checked' cannot pass for
knowledge."* The first restart of a session is precisely when yesterday's unverified state matters most,
and it is precisely the restart that hides it.

**Suggested fix.** Drop the `trading_day` filter in `recover()` for both calls, matching
`unexplained_position_events()` (`journal.py:2160`), which is correctly not day-scoped. If unbounded
growth is a concern, scope to "not confirmed and older than N days" as a separate, louder gap rather
than dropping them. The day-scoped variants of `unconfirmed_fills()` / `unattributed_fills()` are still
useful for the dashboard; recovery should not use them.

---

## HIGH

### H1 — The order ledger and the fill ledger can diverge permanently, undetected

**Location:** `_advance` at `journal.py:1484-1541` (writes `orders.spreads_filled`, never writes a
`spread_fills` row); no invariant check anywhere in the module.

**What breaks.** Two independent ledgers record the same event and nothing reconciles them.

- **Direction A** — fill recorded, order status not. Process dies between `record_spread_fill` and
  `mark_status`. The order stays non-terminal, so it is chased on restart and the order ledger
  self-heals. Acceptable.
- **Direction B** — order status recorded, fill not. Restart reconciliation calls
  `mark_status(FILLED, spreads_filled=5, net_price_per_spread=-1.20)` from a REST read. This writes
  the order row and **creates no `spread_fills` row**. Verified: `order.spreads_filled == 5.0` while
  `sum(f.spreads for f in j.spread_fills_for("A")) == 0.0`, with nothing detecting it.

**Concrete scenario.** The stream drops during the fill (GOTCHAS #13), so the fill is never streamed;
restart reconciliation learns the order filled from REST and marks it. Consequences compound:

- `_fills_between` (`journal.py:1995`) finds no closing fill, so a close we initiated later reads as an
  unexplained disappearance and a false `PositionEvent(cause=UNKNOWN)` is filed.
- `_realised_today`'s staleness check (`journal.py:2481`, `MAX(occurred_at) FROM spread_fills`) does not
  see the fill, so a P&L snapshot taken *before* it is blessed as current and a stale realised figure is
  handed to the daily loss stop.
- `spread_fills_on(day)` — the natural source for the submission's trade list — silently omits it.

**Suggested fix.** Either (a) have `_advance` synthesise a `spread_fills` row when a status carries fill
figures that the fill ledger does not already account for, keyed on a deterministic id such as
`f"{client_order_id}:reconciled:{status_at}"` with `source=REST`; or (b) add an explicit
`fill_ledger_divergences()` read returning orders where `spreads_filled != sum(spread_fills.spreads)`,
and a `RecoveryGap.LEDGER_DIVERGENCE`. (b) is the smaller change and preserves the module's refusal to
invent facts; (a) is more useful operationally. Doing (b) is the minimum.

---

### H2 — `vanished_positions()` false-positives on ordinary broker lag, and the false positive is permanent

**Location:** `journal.py:1995-2010` (`_fills_between`), window
`occurred_at > previous.taken_at AND occurred_at <= latest.taken_at`

**What breaks.** The window is anchored to *snapshot* times, but `GET /v2/positions` is eventually
consistent with respect to fills. The realistic sequence puts our own closing fill *before* the snapshot
that still shows the position.

**Reproduction.**

```python
j.record_intent(client_order_id="A", symbol="SPY", spreads=5.0, ...)
j.record_spread_fill(fill_id="f", symbol="SPY", spreads=5.0, net_price_per_spread=0.40,
                     occurred_at=T(14, 0, 0),        # fill executes at 14:00:00
                     source=FillSource.REST, client_order_id="A")
j.record_position_snapshot([SPY_position], at=T(14, 0, 5))   # positions endpoint still lists it
j.record_position_snapshot([],             at=T(14, 1, 0))   # now gone

v = j.vanished_positions()[0]
v.explained       # -> False
v.closing_fills   # -> ()   <-- our own close, reported as unexplained
```

The same outcome occurs whenever a streamed fill lands after the diff runs, which is a routine ordering.

**Why it is permanent.** The caller then writes `PositionEvent(cause=UNKNOWN,
evidence=INFERRED_FROM_SNAPSHOT)`. There is **no path to correct it**: `confirm_position_event`
(`journal.py:2068`) requires an `activity_id`, and no non-trade activity will ever arrive for a trade we
made ourselves. The row is returned by `unexplained_position_events()` forever and blocks
`RecoveryState.is_clean` on every subsequent restart — which trains whoever is on call to ignore the one
gap that matters most.

**Suggested fix.** Two changes, both small:

1. Widen the window's lower bound. Anchor it to the fill horizon rather than the snapshot:
   `occurred_at > previous.taken_at - LAG_GRACE`, with `LAG_GRACE` a named constant (30–60s) documented
   as covering positions-endpoint lag.
2. Match on identity, not just symbol and time. `PositionRecord.client_order_id` already exists
   (`journal.py:492`) and `_fills_between` ignores it. A fill whose `client_order_id` matches the
   vanished position's is our close regardless of the window.

Additionally, add a `reclassify_position_event(id, cause, detail)` so a late-arriving fill can retire a
false `UNKNOWN` — without it, the "fail closed" design has no release valve and degrades into noise.

---

### H3 — Quantity reductions are invisible; a partially assigned or half-closed spread reads as healthy

**Location:** `journal.py:1979` —
`gone = [p for symbol, p in previous.by_symbol.items() if symbol not in held_now]`

**What breaks.** Only whole-symbol disappearance is detected. A position whose size drops produces
nothing.

**Reproduction.**

```python
j.record_position_snapshot([PositionRecord(symbol="SPY", spreads=5.0, max_loss=1500.0)], at=T(0))
j.record_position_snapshot([PositionRecord(symbol="SPY", spreads=2.0, max_loss=600.0)],  at=T(1))
j.vanished_positions()
# -> ()
```

**Why this matters here specifically.** `docs/research/reconciliation-reference.md:495` states the
requirement in full: *"Any symbol you expected but do not see, **or any qty that differs from your
book**, is an unexplained change."* Only the first clause is implemented. And §6 "Partial-leg risk" names
the exact failure this creates:

> a spread can end up half-closed — assignment on the short leg leaves you long the other leg plus a
> stock position, and the positions endpoint will show exactly that with nothing marking it as a broken
> spread.

Because `PositionRecord` is keyed on the underlying (it must be — `risk.py` calls `is_tradeable(symbol)`
and `are_correlated(symbol, ...)` on it, which are underlying tickers), a short leg being assigned leaves
the underlying still present in the book with an unchanged `max_loss`. The journal reports a healthy
defined-risk position where the actual exposure is a naked long option plus an unhedged equity position.

**Suggested fix.**

1. Extend the diff to size: add a `ReducedPosition` (or a `spreads_before`/`spreads_after` pair on
   `VanishedPosition`) for any symbol whose `spreads` decreased between snapshots, and surface it
   through the same `PositionEvent` machinery.
2. Separately, persist the leg→strategy mapping the reference doc asks for (see Q2 below), so a broken
   spread is detectable by comparing the observed leg set against the recorded one. Today the leg OCC
   symbols exist only inside the opaque `orders.payload` JSON and in `leg_fills.occ_symbol`, with no
   queryable mapping.

---

### H4 — `RecoveryState` has no book-age gap; a stale book recovers clean and feeds the daily loss stop

**Location:** `journal.py:2394-2400` (`book = self.latest_positions()`; the only gap is
`POSITIONS_UNOBSERVED`, which fires solely when the book was *never* observed)

**Reproduction.**

```python
j.record_session_open_equity(trading_day=date(2026, 8, 28), equity=100_000.0)
j.record_position_snapshot([SPY_position], at=now - timedelta(days=3))
j.record_reconciliation(scope=ReconciliationScope.FULL, ok=True, at=now)

st = j.recover(date(2026, 8, 28), now=now)
st.is_clean        # -> True
st.book.taken_at   # -> 2026-08-25   (three days stale)
st.gaps            # -> ()
```

**What breaks.** `VIEW_STALE` measures the `reconciliations` table, which is written independently of
`position_snapshots` — nothing forces a recorded reconciliation to have included a snapshot. So a fresh
reconciliation record and an ancient book coexist happily.

**Concrete scenario.** The stale book's `unrealised_pnl` flows into `risk.AccountState.unrealised_pnl`
and thence into `conservative_day_pnl` (`risk.py:88-95`), which is the number the daily loss stop
compares against. A three-day-old unrealised figure is not today's, and `min(0.0, unrealised)` means a
stale *gain* is discarded while a stale *loss* is counted — so the error is not even symmetric. The stale
`max_loss` values simultaneously feed `open_risk` and the aggregate risk cap (`risk.py:227`).

**Suggested fix.** `book.taken_at` is already returned; compare it. Add
`RecoveryGap.POSITIONS_STALE`, raised when `now - book.taken_at > max_view_age` (reuse the same
threshold, or add a separate `max_book_age`). Note the `now is None` case must be treated as stale, the
way `VIEW_STALE` already does at `journal.py:2417`.

---

### H5 — Session-open equity: first-write-wins over an undefined trading day

**Location:** `journal.py:2306-2345` (`record_session_open_equity`), plus the absence of any trading-day
definition anywhere in `src/underwriter/`

Fully treated in **Q1** below. In brief: the once-per-day mechanism is correct and restart-safe, but the
day it is keyed on is undefined by the module and undefined by the repo, and the first write is
irreversible. The full analysis, including why the UTC boundary does *not* fall mid-session, is in Q1.

---

### H6 — Nothing implements the write-ahead sequence

**Location:** `src/underwriter/execution.py` — `grep -rn journal src/underwriter/*.py` returns exactly
one hit outside `journal.py`: a docstring mention at `execution.py:601`.

**What breaks.** `execution.py` never calls `record_intent`, `mark_submitted`, or `mark_status`. The
ordering that the entire module exists to guarantee — journal, then submit, never the reverse — is
currently a convention described in prose in two docstrings, with no code enforcing it and no test
covering the joined path. `tests/test_execution.py` and `tests/test_journal.py` each test their own half.

This is not a defect in `journal.py`; the module is correct in isolation. It is a note that the
protocol's guarantee does not yet exist in the system, and the code that will provide it also inherits
the undefined trading-day contract (H5/Q1) and the `None`-handling traps (Q2). All three should be
settled in the same change.

**Suggested fix.** Put the sequence in one place — a thin `submit_journalled(journal, adapter, order)`
that owns the ordering — rather than leaving it to each call site. Add an integration test that kills the
sequence at each of the four points and asserts what recovery reconstructs.

---

### H7 — `_realised_today` returns a confident `0.0` for a day whose only P&L event was an assignment

**Location:** `journal.py:2489-2496`

```python
if snapshot is None:
    if last_fill is None:
        # "Nothing filled today and nothing was reported: the day has
        #  realised nothing, and that is a fact rather than a guess."
        return 0.0, None
```

**What breaks.** The "this is a fact" branch keys off `spread_fills` alone. Assignment, exercise and
expiry realise P&L and produce **no fill row** — that is the entire premise of GOTCHAS #12 and the reason
`position_events` exists as a separate table.

**Reproduction.**

```python
j.record_session_open_equity(trading_day=DAY, equity=100_000.0)
j.record_position_snapshot([SPY_position], at=T(0))
j.record_reconciliation(scope=ReconciliationScope.FULL, ok=True, at=now)
j.record_position_event(symbol="SPY", trading_day=DAY, cause=PositionEventCause.ASSIGNMENT,
                        evidence=PositionEventEvidence.CONFIRMED_BY_ACTIVITY,
                        activity_id="OPASN-1", spreads=5.0)

st = j.recover(DAY, now=now)
st.realised_pnl_today   # -> 0.0
st.gaps                 # -> ()
```

**Concrete scenario.** A quiet Friday: no orders submitted, no fills. A short put from Wednesday is
assigned. The position event is recorded and confirmed. `recover()` reports realised P&L of exactly
`0.0` with no gap, and the daily loss stop measures a potentially five-figure assignment loss as zero.

This is the specific failure `journal.py:59-63` says must not happen: *"A day whose realised P&L we
cannot vouch for reads as `None`, not as zero — because zero would silently disarm the daily loss
stop."* The rule is right; the predicate that decides when it applies is incomplete.

**Suggested fix.** Include `position_events` in the "did anything happen today?" test:

```sql
SELECT MAX(detected_at) FROM position_events
 WHERE trading_day = ? AND cause != 'closed_by_us'
```

Return `0.0` only when there were no fills *and* no P&L-bearing position events. Apply the same
`MAX(...) > snapshot.at` staleness comparison to position events that is already applied to fills — an
assignment after the last P&L snapshot makes that snapshot stale just as a fill does.

---

## MEDIUM

### M1 — `UNIQUE (symbol, trading_day, from_snapshot_id)` is NULL-permeable; duplicate events

**Location:** `journal.py:758` (constraint), `journal.py:2038` (`INSERT OR IGNORE`), `journal.py:2061`
(the dedupe `SELECT`)

SQLite treats NULLs as distinct in a UNIQUE index, so the constraint does not bind when
`from_snapshot_id` is `None`.

```python
for _ in range(3):
    j.record_position_event(symbol="SPY", trading_day=DAY, cause=PositionEventCause.UNKNOWN)
len(j.position_events())   # -> 3
```

The `INSERT OR IGNORE` never fires, so `cur.rowcount == 1` always, and the dedupe `SELECT` at
`journal.py:2061` — which *does* handle NULL correctly via `from_snapshot_id IS ?` — is unreachable. The
docstring's promise that *"a monitor that runs twice in a cycle cannot inflate the count"* holds only
when a snapshot id is supplied. Three phantom unexplained exits then block `is_clean` and inflate the
count a judge reads.

**Fix.** Either make `from_snapshot_id` NOT NULL (with a sentinel of `0` for "not from a snapshot"), or
add a partial unique index for the NULL case:
`CREATE UNIQUE INDEX position_events_no_snapshot ON position_events (symbol, trading_day)
 WHERE from_snapshot_id IS NULL;`

### M2 — `confirm_position_event` will overwrite an exit we made ourselves

**Location:** `journal.py:2102-2105` — the lookup filters on `symbol AND trading_day AND
activity_id IS NULL` with no filter on `cause`.

```python
mine = j.record_position_event(symbol="SPY", trading_day=DAY,
                               cause=PositionEventCause.CLOSED_BY_US, from_snapshot_id=1, spreads=5.0)
got  = j.confirm_position_event(activity_id="OPEXP-1", symbol="SPY", trading_day=DAY,
                                cause=PositionEventCause.EXPIRY)
got.id == mine.id   # -> True
got.cause           # -> "expiry"
```

Our own deliberate close is relabelled an expiry, and the real expiry gets no row of its own. Plausible
whenever we close one position in a symbol and another expires the same day.

A second, related trap: the lookup matches on the *activity's* `trading_day`. Paper NTAs sync "at the
start of the following day" (GOTCHAS #12), so if the caller passes the sync date rather than the date the
event actually occurred, nothing matches, a duplicate confirmed event is created, and the original
inference stays unexplained forever. `test_confirmation_does_not_steal_another_days_inference`
(`tests/test_journal.py:1077`) shows the day-scoping is deliberate — which makes the caller contract
sharp and undocumented.

**Fix.** Add `AND cause != 'closed_by_us'` to the lookup, and document in the docstring that
`trading_day` must be the date the event occurred, not the date the activity synced.

### M3 — Position snapshots bypass `_finite` and every positivity check

**Location:** `journal.py:1894-1935` — every value goes through a bare `float()`.

```python
j.record_position_snapshot([
    PositionRecord(symbol="XLE", spreads=float("inf"), max_loss=float("inf")),
    PositionRecord(symbol="XLV", spreads=-3.0,         max_loss=-500.0),
], at=now)
# both round-trip intact
```

`max_loss` feeds `risk.AccountState.open_risk` directly, so a negative value silently *reduces* measured
open risk against the aggregate cap at `risk.py:227`. This is the only writer in the module that skips
`_finite`, which is applied consistently to P&L, equity, order quantities and fill figures.

`NaN` is caught, but only by accident: SQLite binds NaN as NULL and the `NOT NULL` constraint rejects it,
surfacing as a raw `sqlite3.IntegrityError` rather than a `JournalError`. That is the one place the
module leaks its storage layer to callers. (Worth noting how close this was: `risk.py` has no NaN guard
on `open_risk`, and `NaN + x > cap` is `False`, so a NaN `max_loss` reaching `AccountState` disarms the
aggregate risk cap entirely — verified, `evaluate()` returns `allowed=True`. The journal happens to block
it; nothing downstream would.)

**Fix.** Apply `_finite` to all four numeric fields, refuse negative `spreads` and negative `max_loss`,
and raise `JournalError` rather than letting `sqlite3.IntegrityError` escape.

### M4 — Guard #3 is one-sided: a closing debit net passes as a leg premium

**Location:** `journal.py:1782-1787`

The `premium_per_contract <= 0` check catches an *opening* credit spread's signed net (`-1.20` →
correctly refused, verified) but not a *closing* credit spread's net, which is a positive debit and
passes cleanly:

```python
j.record_leg_fill(fill_id="L1", contracts=0.40, premium_per_contract=0.40,
                  side="buy", source=FillSource.REST, ...)   # -> True
```

Half the round trip is guarded. Related: nothing relates `contracts` to `ratio_qty × spreads` of the
parent — `parent_fill_id` has no foreign key (deliberate, per the `spread_fills` comment) and no
validation, so the parent/leg relationship the docstring describes is never checked.

**Fix.** When `parent_fill_id` or `client_order_id` resolves, check `contracts` against
`ratio_qty × parent spreads` derived from the order payload. A leg premium that exactly equals the
parent's `|net_price_per_spread|` is also a strong confusion signal worth at least a `detail` note.

### M5 — Snapshot timestamps are not required to be monotonic

**Location:** `journal.py:1937-1943` (`recent_position_books`, `ORDER BY taken_at DESC, id DESC`)

```python
j.record_position_snapshot([SPY_position], at=now)
j.record_position_snapshot([],             at=now - timedelta(minutes=5))   # skewed clock / backfill

j.recent_position_books(2)   # -> [(id=1, 14:00), (id=2, 13:55)]
j.latest_positions()         # -> snapshot 1  <-- NOT the latest observation
j.vanished_positions()       # -> ()          <-- the disappearance is hidden
```

An NTP step, a caller passing a stale `at`, or any backfill silently reorders history. Combined with H2,
an inverted window makes `_fills_between` return empty and every exit read as unexplained.

**Fix.** Reject an `at` earlier than the newest existing `taken_at` in `record_position_snapshot`, or
order by `id DESC` alone (insertion order is the real sequence) and treat `taken_at` as metadata.

### M6 — A confirmed REST fill cannot be corrected by a later REST read

**Location:** `journal.py:1724` — `if source is not FillSource.REST or _opt_text(existing, "confirmed_at")
is not None: return`

```python
j.record_spread_fill(fill_id="f", spreads=5.0, net_price_per_spread=-1.20, source=FillSource.REST, ...)
j.record_spread_fill(fill_id="f", spreads=2.0, net_price_per_spread=-1.35, source=FillSource.REST, ...)
j.spread_fill("f")   # -> spreads=5.0, net=-1.20, detail=''
```

The corrected authoritative figures are dropped with no update and no note. The disagreement-recording
logic immediately below (`journal.py:1729-1736`), which is good, only ever runs on the stream→REST
transition. Also, `record_spread_fill` returns `False` for both "already recorded, nothing to do" and
"confirmed just now", so a caller cannot distinguish them.

**Fix.** On a REST-over-REST disagreement, keep the stored figures (a later read is not automatically
more correct) but append the disagreement to `detail` and set a flag the reconciler can surface —
silence is the wrong response to two authoritative reads that differ.

### M7 — `abandon()` is a one-way door

**Location:** `journal.py:1458-1482` (`abandon`), `journal.py:1505-1514` (the terminal-status guard)

```python
j.record_intent(client_order_id="A", ...)
j.abandon("A", detail="broker 404 on get-by-client-id")
j.mark_status("A", OrderStatus.FILLED, spreads_filled=5.0, net_price_per_spread=-1.20)
# -> JournalError: 'A' is already abandoned and cannot become filled
j.unreconciled_orders()   # -> []   <-- no longer chased
```

If the 404 was transient (eventual consistency on `orders:by_client_order_id`), or a retried submission
landed after the lookup, the order is both unchased and unrecordable. The fill itself would still be
captured with `attribution=journalled` because the `orders` row exists, so the position is not lost — but
the recovery path throws, and throwing inside recovery is a poor failure mode.

The strictness is deliberate and defensible (two sources disagreeing about a settled order should be
loud). The problem is that it is loud in a way the caller cannot act on.

**Fix.** Add an explicit `unabandon(client_order_id, detail)` — or let `ABANDONED → <any>` transition
with a mandatory `detail` and a recorded `reconciliations` row — so the correction is possible and
audited rather than impossible.

### M8 — A failed `COMMIT` leaves the connection inside a transaction

**Location:** `journal.py:1143-1153`

```python
cur.execute("BEGIN IMMEDIATE")
try:
    yield cur
except BaseException:
    cur.execute("ROLLBACK")
    raise
cur.execute("COMMIT")      # <-- not guarded
finally:
    cur.close()
```

If `COMMIT` raises (`SQLITE_BUSY_SNAPSHOT`, disk full, I/O error) the cursor is closed but the
transaction is never rolled back. Every subsequent `BEGIN IMMEDIATE` on that connection fails with
"cannot start a transaction within a transaction" — including the next `record_intent`, which is the one
write the module exists to guarantee. The process would need a restart to recover, and the failure
presents as a cascade of confusing errors rather than the disk-full it is.

(The ordinary rollback path is fine: I confirmed `in_transaction` is `False` after a raising body and the
journal remains writable.)

**Fix.** Wrap the `COMMIT` and roll back on failure:

```python
try:
    cur.execute("COMMIT")
except BaseException:
    with contextlib.suppress(sqlite3.Error):
        cur.execute("ROLLBACK")
    raise
```

Likewise suppress-and-chain a failing `ROLLBACK` so it cannot mask the original exception.

---

## LOW

### L1 — Test coverage gaps

The tests are genuinely good and are **not** restatements of the implementation. Crashes are simulated
by closing and reopening a real file-backed database rather than by mocking; cross-connection
idempotency uses two live `Journal` objects on one file; `TestRecovery` asserts gap codes and displayable
detail strings rather than internals; `test_recovery_survives_a_restart_intact`
(`tests/test_journal.py:1276`) compares whole `RecoveryState` values across a reopen, which is a strong
check. The module docstring's three recurring questions are each covered by real tests.

What is missing maps almost exactly onto the findings above:

| Gap | Finding |
|---|---|
| `test_file_database_uses_wal` (`:142`) comments that "the durability promise is WAL plus `synchronous=FULL`" and asserts only WAL. Dropping `synchronous=FULL` would break no test. | pragmas |
| No test writes a leg contract count or a leg premium into `record_spread_fill` — the mirror of `test_contracts_written_where_spreads_belong_is_refused` (`:544`) on the parent-fill path. | C2 |
| `TestVanishedPositions` never records a third snapshot. | C1 |
| `TestVanishedPositions` never changes a position's quantity. | H3 |
| `test_a_position_we_closed_ourselves_is_explained` (`:880`) places the fill at minute 45, comfortably inside the 30→60 window; the realistic lag case (fill *before* the last snapshot that still lists the position) is never exercised. | H2 |
| No test asserts `recover()` is *not* clean given yesterday's unconfirmed fills, or given a days-old book. | C3, H4 |
| No test calls `record_position_event` with `from_snapshot_id=None` twice. | M1 |
| No test has an activity meet a `CLOSED_BY_US` event. | M2 |
| No test records a position event without a fill and checks `realised_pnl_today`. | H7 |
| No power-cut / crash-injection test (e.g. `kill -9` a subprocess mid-write and reopen). Durability is asserted by pragma inspection only. | — |

**Fix.** Assert `PRAGMA synchronous` alongside `journal_mode` in `TestOpening`, and add one test per row
above. Each is three to six lines; the reproductions in this document can be lifted directly.

---

## Q1 — Session-open equity and the trading-day boundary

### Is the day boundary exchange-local or UTC?

**Neither. It is undefined, and that is the finding.**

The journal never derives a trading day. `trading_day: date` is a caller-supplied parameter on every
writer and every reader that touches it — `record_spread_fill`, `record_leg_fill`,
`record_position_event`, `confirm_position_event`, `record_pnl`, `record_session_open_equity`,
`spread_fills_on`, `latest_pnl`, `session_open_equity`, `position_events`, and `recover` itself. Nothing
validates that the process which *writes* the session-open equity and the process which *reads* it agree
on what day it is.

`grep` finds no exchange-calendar helper, no `ZoneInfo`, and no `America/New_York` anywhere in
`src/underwriter/`. The only date-derivation precedents in the codebase are both UTC:
`execution.py:432` (`now.astimezone(UTC).strftime("%Y%m%d")` for the `client_order_id` day bucket) and
`data.py:139` (`datetime.now(UTC).date()`). So the de facto answer, if nothing changes, will be UTC.

### Does a UTC boundary roll over mid-session?

**No — and I want to correct the premise, because it changes what the fix should be.**

UTC midnight falls at **20:00 ET during EDT** (UTC−4) and **19:00 ET during EST** (UTC−5). The US
options regular session ends at 16:00 ET (16:15 for some index products). Both boundaries are three to
four hours *after* the close, not mid-session. For every event in the regular session, the UTC date and
the exchange date are the same date. A UTC day bucket is therefore safe for fills, P&L snapshots and the
09:30 ET session-open equity write, and the daily loss stop will not reset during an afternoon.

The hazard runs the other way: **any journal write between roughly 19:00 ET and midnight ET is stamped
with tomorrow's UTC date.** That is not a trading-hours concern, but it is very much an
overnight-monitoring and restart concern, and it interacts badly with first-write-wins (below).

### Is session-open equity written exactly once per day?

**Yes, and the mechanism is correct.** `session_equity` has `trading_day TEXT PRIMARY KEY`
(`journal.py:793`), and `record_session_open_equity` (`journal.py:2306`) does `INSERT OR IGNORE` followed
by a `SELECT`, returning the value **of record** rather than the value passed. The docstring's reasoning
is right: re-recording mid-session would let the baseline drift with P&L and the stop would never fire.
Non-finite and non-positive values are refused up front (`journal.py:2331`), which correctly prevents a
zero baseline from silently disabling the stop.

### What happens if the agent restarts after it was already written?

**The correct thing.** The restarting process re-reads current equity — say 96,000 after a bad morning —
calls `record_session_open_equity` again, and gets the **original** baseline back:

```python
j.record_session_open_equity(trading_day=DAY, equity=100_000.0)   # 09:30 ET
j.record_session_open_equity(trading_day=DAY, equity=96_000.0)    # post-crash restart, 11:15 ET
# -> returns 100000.0
```

So a naive caller that always calls this on startup and uses the return value is safe by construction.
That is good design and it works.

### The two real problems

**(a) The first write is irreversible, and the UTC boundary can make the first write the wrong one.**

Any process that touches `record_session_open_equity` after ~19:00/20:00 ET is already on tomorrow's UTC
date and will claim tomorrow's baseline with tonight's equity:

```python
# 22:05 ET Thursday = 02:05 UTC Friday. An overnight monitor records "Friday's" open.
j.record_session_open_equity(trading_day=date(2026, 8, 28), equity=91_000.0,
                             at=datetime(2026, 8, 28, 2, 5, tzinfo=UTC))
# The real 09:30 ET open, same UTC date:
j.record_session_open_equity(trading_day=date(2026, 8, 28), equity=100_000.0,
                             at=datetime(2026, 8, 28, 13, 30, tzinfo=UTC))
# -> 91000.0    the 09:30 write was silently ignored
```

With `daily_loss_stop_pct = 1.5`, the stop now measures against a baseline 9% below the true open and
will not fire until the day is roughly 10.5% down. There is no override, and — worse — no record that a
conflicting value was ever offered. The discarded value is not written to `detail`, not journalled as a
reconciliation, not returned as a warning. A 9% divergence between the stored baseline and the proposed
one is exactly the signal an operator needs and it is thrown away.

The same UTC boundary also means a weekend or holiday write can claim a baseline for a day the market
never opens, which then sits in the table forever.

**(b) A day whose open was missed cannot be reconstructed, though the data to do it exists.**

If the agent crashes before the open write and restarts at 11:00 ET, `recover()` correctly reports
`SESSION_EQUITY_MISSING` — it fails closed, which is right. But the agent has no correct way forward:
current equity at 11:00 is not session-open equity, and writing it would set a baseline that already
absorbs the morning's losses, disarming the stop precisely on the day it is needed. Meanwhile
`pnl_snapshots.equity` (`journal.py:788`) records equity readings with timestamps, so the earliest
reading of the day is a defensible reconstruction — and nothing offers it.

### Suggested fixes for Q1

1. **Define the trading day once, in code.** Add `underwriter.clock` (or a `trading_day()` helper in
   `journal.py`) returning the exchange-local date via `ZoneInfo("America/New_York")`, and have every
   caller use it. This is not because UTC breaks mid-session — it does not — but because "the trading day
   is the ET date" is a one-line invariant that can be tested, while "everyone happens to pass the UTC
   date and it happens to coincide" is a coincidence that will not survive the first overnight process.
   Best done together with H6, since the same caller needs both.
2. **Refuse a session-open write outside the session.** Reject an `at` that is not within a defined
   window (say 09:00–16:30 ET) for the `trading_day` being written, rather than accepting a 02:05 UTC
   write for "today".
3. **Never discard a conflicting baseline silently.** When `INSERT OR IGNORE` is ignored and the offered
   equity differs materially from the stored one, append the divergence to a `detail` column and return
   it, or raise. The caller should be able to see that the two disagree.
4. **Add `reconstruct_session_open_equity(trading_day)`** that returns the earliest
   `pnl_snapshots.equity` for the day with an explicit "reconstructed" marker, so recovery from a missed
   open is possible without inventing a number and without pretending it is as good as the real one.

---

## Q2 — Restart recovery completeness against `risk.py`

### What `risk.AccountState` needs, and where it comes from

| `AccountState` field | Provided by `recover()`? | Notes |
|---|---|---|
| `equity` | **No** | Correct — must be a live broker read; a recovered equity would be stale by definition. |
| `options_buying_power` | **No** | Correct — same reason. Also the input to the `INSUFFICIENT_BUYING_POWER` gate and to the GOTCHAS #10 sell-out risk. |
| `starting_equity` | **Yes** — `RecoveryState.session_open_equity: float \| None` | ⚠️ see trap 1 |
| `realised_pnl_today` | **Yes** — `RecoveryState.realised_pnl_today: float \| None` | ⚠️ see trap 2 |
| `open_positions[].symbol` | Yes | `PositionRecord.symbol` |
| `open_positions[].max_loss` | Yes | `PositionRecord.max_loss` |
| `open_positions[].unrealised_pnl` | Yes | `PositionRecord.unrealised_pnl` — ⚠️ may be days stale, see H4 |
| `open_positions[].net_delta` | Yes | `PositionRecord.net_delta` |

`PositionRecord` (`journal.py:478`) mirrors `risk.OpenPosition` field-for-field by name, so the mapping is
a straight comprehension with no translation layer inventing anything. That was a deliberate design
choice and it pays off. `open_risk`, `net_delta`, `unrealised_pnl` and `conservative_day_pnl` are all
derived properties on `AccountState`, so nothing else is needed for the concentration, aggregate-risk and
aggregate-delta gates.

### The two traps in the glue — both silently disarm the daily loss stop

`RecoveryState` returns `float | None` for exactly the two fields the daily loss stop depends on, and
`AccountState` declares both as plain `float` with defaults. `risk.py` has **no** `UNREADABLE_PNL`
denial and **no** guard on a zero `starting_equity`. Verified on a 9%-down day:

```python
lim = RiskLimits()   # daily_loss_stop_pct = 1.5

# trap 1: session_open_equity was None, defaulted to 0.0
AccountState(equity=91_000, options_buying_power=91_000,
             starting_equity=0.0, realised_pnl_today=-9_000)
evaluate(...)  # -> allowed=True, denials=()          <-- stop never evaluated

# trap 2: realised_pnl_today was None, defaulted to 0.0
AccountState(equity=91_000, options_buying_power=91_000,
             starting_equity=100_000, realised_pnl_today=0.0)
evaluate(...)  # -> allowed=True, denials=()          <-- stop sees no loss

# correctly wired
AccountState(equity=91_000, options_buying_power=91_000,
             starting_equity=100_000, realised_pnl_today=-9_000)
evaluate(...)  # -> allowed=False, denials=(Denial.DAILY_LOSS_STOP,)
```

`risk.py:181` reads `if account.starting_equity > 0:` — a `0.0` baseline skips the entire daily loss stop
block with no denial, no detail line, and nothing in the audit trail saying the gate was not evaluated.
This is the single sharpest piece of glue in the system: two `or 0.0` fallbacks, each of which looks
harmless at the call site, each of which turns off the stop on the day it matters.

`journal.py` is right to return `None`. `risk.py` is the half that should be hardened: add a
`Denial.UNREADABLE_PNL` and make `AccountState.starting_equity` / `realised_pnl_today` accept `None` and
deny on it, rather than relying on every caller to remember. Absence of evidence is not evidence of
safety, per that module's own third rule — it just is not applied to these two inputs.

### State the agent needs that `recover()` does NOT return

Glue somebody has to write, in rough order of danger:

1. **The kill switch.** `risk.evaluate(kill_switch=...)` gates every entry, and `kill_switch` appears
   **zero times** in `journal.py`. It is not recorded, not restored, and not in `RecoveryState`. An agent
   that trips its own kill switch and then crashes restarts with it off. This is the largest single
   omission — it is a durable safety decision stored nowhere durable. *Fix: a `flags` table
   (`name TEXT PRIMARY KEY, value, set_at, detail`) and a `kill_switch: bool` on `RecoveryState`, with a
   `RecoveryGap` if it was engaged.*
2. **The pending snapshot diff.** `recover()` never calls `vanished_positions()`, and there is no gap for
   "snapshots recorded but never diffed" (C1). The caller must know to run the diff itself, and must not
   record a new snapshot before doing so — an undocumented ordering constraint on the module's most
   important operation.
3. **Book freshness.** `book.taken_at` is returned but never checked against `now` (H4), so the caller
   must do it and must know to.
4. **`now_et` for the session-timing gate.** `risk.evaluate` takes `now_et: time` and
   `limits.no_new_entries_after_et` is an ET string; there is no ET clock anywhere in the repo. Same
   missing piece as Q1's trading-day helper.
5. **The leg→strategy mapping.** `docs/research/reconciliation-reference.md:540` is unambiguous:
   *"Persist the leg→strategy mapping durably, keyed on `client_order_id` and the leg OCC symbols, before
   submitting."* The journal stores leg symbols only inside the opaque `orders.payload` JSON blob and in
   `leg_fills.occ_symbol`. There is no queryable "strategy X consists of these OCC symbols" relation and
   `recover()` returns nothing of the sort — so the half-closed-spread detection that §6 requires cannot
   be written against the journal as it stands. This is a schema gap, not just a recovery gap, and it
   compounds H3.
6. **Per-order fill reconstruction.** `unreconciled_orders` returns the records, and the aggregate
   working-spread count appears only inside a human-readable `detail` string
   (`journal.py:2379-2384`). A caller wanting the number must re-derive it from
   `sum(o.spreads_working)`. Minor, but it means the detail string is doing work that a field should do.
7. **The shadow P&L series.** `_realised_today` reads `PnlSource.OFFICIAL` only (`journal.py:2478`),
   which is correct for the risk gate. But GOTCHAS #3 requires both series side by side in the
   submission, and `RecoveryState` carries neither history. Not a safety issue; a reporting one.
8. **Cycle continuity.** Nothing records "which cycle was in flight". After a crash mid-cycle the agent
   cannot tell whether it had already evaluated a candidate, though `record_decision` rows keyed on
   `cycle_id` make this reconstructible if the caller looks.

Not gaps, correctly excluded: live equity, live buying power, `RiskLimits` (config), and the universe /
correlation tables (static).

---

## Recommended order of work

1. **C2** — smallest diff, largest immediate protection; the validator already exists in `_checked_filled`.
2. **C3** — a two-line change (drop the `trading_day=` filters at `journal.py:2431` and `:2440`).
3. **Q2 trap 1 and 2** — harden `risk.py` against `None`/`0.0` before any caller is written, so the
   dangerous glue is impossible rather than merely documented.
4. **C1** — needs a schema change (snapshot cursor); do it before the monitoring loop is written, not after.
5. **H7, H4, M1, M2, M3, M8** — small, independent, each closes a specific silent-wrong-answer path.
6. **H2, H3** — need a little design thought (lag grace window, size diffing, leg mapping); worth doing
   together.
7. **Q1 fixes and H6** — the trading-day helper and the write-ahead caller are the same change.
8. **L1** — add the tests alongside each fix, lifting the reproductions above.
