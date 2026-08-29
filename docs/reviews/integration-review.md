# Integration review: the seams between execution, journal, and the rest

Reviewed: 2026-08-28.
Scope: `src/underwriter/{config,preflight,universe,volatility,regime,chain,risk,data,occ,calibrate,execution,journal}.py`
against `docs/research/strategy-spec.md` v2.0 and `docs/GOTCHAS.md` (20 entries).

`execution.py` was being edited during this review (it grew from 1483 to 1741 lines; `build_adapter`
flipped its default to `primary=Backend.SDK` and `OrderResult.as_record()` appeared). Everything below
reflects the state at 21:37. Line numbers for that file may drift; function names will not.

No files were modified. This is findings only.

---

## Summary

The two new modules are individually excellent and they do not contradict each other on the things
most likely to have gone wrong — the sign convention, the parent/leg unit split, and the write-ahead
ordering all agree. The failures are at the boundaries, and they cluster in three places:

1. **Nothing joins them.** No module imports both `execution` and `journal`. There is no orchestrator,
   no cycle, no ET clock, no account reader, and `pyproject.toml`'s declared console script
   (`underwriter.cli:app`) points at a module that does not exist.
2. **The older modules quietly disable the newer guarantees.** The liquidity screen will reject the
   strikes the strategy sells; the delta cap goes inert without saying so; and the journal's careful
   `float | None` unknowns get flattened to `0.0` by the risk engine's signature.
3. **The exit path cannot be built from what is stored.** `PositionRecord` has no expiry, no closing
   price function exists, and `regime.py` states as a design rule that it will not trigger exits — which
   the strategy spec requires it to do.

Ranked by probability of biting during the four-session run.

---

# A. Ranked findings

## 1. The liquidity screen will reject the strikes this strategy actually sells

`chain.screen_contract` (`chain.py:145-176`) applies `LiquidityPolicy.max_spread_pct_of_mid = 10.0`
(`chain.py:136`) identically to both legs. That threshold is calibrated against at-the-money contracts.
The strategy sells 0.15-0.30 delta strikes.

Worked example. XLE at 95, 7 DTE, 0.25-delta put quoted **0.30 / 0.35** — a penny-wide, entirely normal
market:

```
width_pct_of_mid = (0.35 - 0.30) / 0.325 * 100 = 15.4%   ->  SPREAD_TOO_WIDE
```

The protective wing, quoted 0.15 / 0.20, is 28.6% — also rejected. Neither leg survives,
`select_credit_vertical` returns `NO_SHORT_LEG_CANDIDATE` or `NO_VIABLE_WIDTH`, and the agent stands
down with a reason that looks entirely plausible in the dashboard.

The arithmetic is structural, not a tuning accident: a fixed penny spread is a small *relative* spread on
a $4 at-the-money contract and a large one on a $0.30 out-of-the-money contract. The filter is expressed
in relative terms, so it tightens exactly as the option gets cheaper — which is the direction the
strategy deliberately moves in.

**Calibration does not catch this.** `calibrate.near_the_money` uses a ±4% band (`calibrate.py:141`)
whose median is dominated by near-ATM strikes; the comment there records SPY's ATM contract quoting
3.86/3.94, about 2%. The strike we would actually sell sits 3-4% out and is far wider in relative terms.
The measured medians are not the numbers that will gate the trade.

`ZERO_BID` (`chain.py:163-164`) compounds it. Its rationale — "nothing is willing to buy it, we could
never exit" — is correct for the short leg we must buy back and wrong for the long wing, where we are the
buyer and a zero bid is normal and harmless.

`STALE_QUOTE` (`chain.py:167-168`, `max_quote_age_seconds = 30.0`) stacks on top. On the Basic plan's
*indicative* option feed, an out-of-the-money strike's quote can easily be more than 30 seconds old.
`calibrate.summarise_chain` noticed this and reports `passing_ignoring_staleness` as a second pass rate;
the live path has no equivalent relief.

**This is the highest-probability failure in the system, and its symptom is exactly the outcome
`calibrate.py`'s own docstring names as the largest practical risk to the project: an immaculate machine
that stands down for four sessions and posts a flat result.**

Fix: an absolute-cents escape hatch (pass if `quote.width <= 0.05` regardless of relative width), and a
leg-specific policy so the protective wing is not held to the short leg's exitability standard. Test both
against a real market-hours chain at the 0.15-0.30 delta strikes before the run — if the 10% filter
rejects them, nothing else in this document matters.

## 2. The aggregate short-delta cap is silently inert

`risk.evaluate` gates the delta cap on `if net_delta_per_contract:` (`risk.py:255`). The parameter
defaults to `0.0` (`risk.py:148`), which is falsy, so **an unknown delta skips the cap with no denial,
no reason code, and nothing in the audit log.** `Denial` has no member for "delta unknown".

Nothing anywhere computes `net_delta_per_contract`. Grep confirms zero call sites outside `tests/`.

It is derivable in principle. For a put credit spread, per spread in share-equivalents:

```
net_delta_per_spread = (-delta_short + delta_long) * 100
```

Both put deltas are negative, so this comes out positive-long, matching the reasoning in
`config.py:50-57`. But:

- `Contract.delta` is `float | None`, and the Basic plan omits Greeks whenever a bid or ask is zero, the
  underlying SIP price is unavailable, or the solver fails.
- `select_credit_vertical` (`chain.py:538-641`) picks the short leg by delta if **any** contract in the
  chain has one, but the long leg is drawn from unfiltered `tradeable`. So a spread with `delta_short`
  known and `delta_long is None` is a normal outcome, and there is no partial-computation path.
- In the full moneyness fallback, both legs are `None`.

The strategy spec says the delta cap and the regime filter "should be judged on whether they fire, not on
the P&L". A cap that cannot fire and does not announce it is worse than one that is absent, because the
submission will claim it exists.

Fix, minimum: make the parameter `float | None`, and add a `Denial` so the cap's stand-down is recorded.
Conservative interim: treat a missing `delta_long` as `0.0`, which overstates the position's net delta —
the safe direction.

Secondary: even with entry deltas, `PositionRecord.net_delta` is never refreshed. A short strike going
in-the-money has its delta grow toward 100 per spread, which is precisely when the cap matters, and the
book would still be carrying the entry number.

## 3. `RecoveryState -> AccountState` reintroduces the bug the journal exists to prevent

`journal.RecoveryState` deliberately types `realised_pnl_today: float | None` and
`session_open_equity: float | None`, with the rationale stated at `journal.py:603-628`:

> The daily loss stop measures against it, so a value we cannot vouch for must not be handed over as a
> number — an unknown that reads as 0.0 disarms the stop exactly when the day has already gone wrong.

`risk.AccountState` types both as plain `float`, with `realised_pnl_today: float = 0.0` (`risk.py:74`).
And `risk.evaluate` skips the entire daily-loss-stop block when `starting_equity` is not positive:

```python
# risk.py:191
if account.starting_equity > 0:
    loss_limit = account.starting_equity * (limits.daily_loss_stop_pct / 100)
    ...
```

**No denial is appended. The gate simply does not run.**

So the obvious glue —

```python
AccountState(
    starting_equity=recovery.session_open_equity or 0.0,
    realised_pnl_today=recovery.realised_pnl_today or 0.0,
    ...
)
```

— silently disables the daily loss stop, which is one of the risk gates the submission will quote. The
two modules take opposite positions on fail-closed at the one seam where it matters most. The caller must
refuse to trade when either value is `None`; nothing in either module makes that refusal for them.

Note that `journal.record_session_open_equity` (`journal.py:2306-2339`) already refuses a non-positive
equity outright for exactly this reason, and `risk.evaluate` already treats unreadable *current* equity
as `UNREADABLE_EQUITY`. The hole is specifically `starting_equity` and `realised_pnl_today`.

## 4. Cross-cycle duplicate submission after `UNKNOWN_OUTCOME`

`ExecutionAdapter._via` refuses to resubmit without an `_AbsenceProof` — **within one call**. Nothing
carries that refusal across cycles.

Concrete sequence:

1. Cycle N submits an XLE spread. The CLI times out. The lookup cannot confirm absence. Result:
   `OrderResult(ok=False, reason=UNKNOWN_OUTCOME)` (`execution.py:1650`).
2. Cycle N+1 runs sixty seconds later.
3. `risk.evaluate`'s `DUPLICATE_SYMBOL` gate reads `account.open_positions`, which comes from the last
   position snapshot — taken *before* the submission, so XLE is absent. Risk allows.
4. `select_credit_vertical` returns the same structure at the same price.
5. `client_order_id` is deterministic on `(action, legs, qty, limit_price, UTC day, nonce)`
   (`execution.py:411-458`), so it produces the **identical id**.
6. `journal.record_intent` sees a matching payload and returns the existing row as a no-op
   (`journal.py:1336-1401`) — the second attempt leaves no trace in the journal.
7. The order goes out again. Broker de-duplication of `client_order_id` on POST is undocumented
   (GOTCHAS #9).

The caller must gate entries on `journal.unreconciled_orders()` being empty for that symbol. Nothing says
so and nothing enforces it.

**Related, and needed to write that gate: execution never exposes the fact it worked hardest to
establish.** `_AbsenceProof` (`execution.py:1307`) is internal and discarded. On `ok=False`:

- "the broker proved this order does not exist, and attempts are exhausted" returns
  `reason = ambiguous.reason` — typically `TIMEOUT` or `API_ERROR` (`execution.py:1667`);
- "we could not prove absence" returns `reason = UNKNOWN_OUTCOME` (`execution.py:1650`).

That distinction decides between `journal.abandon()` and leave-it-open, and the only structured
difference is that one enum value. It works, but it reads as incidental rather than as the contract. A
`proven_absent: bool` field on `OrderResult` would make it explicit and is a two-line change.

## 5. Stream fills can never be confirmed, so recovery is permanently gapped

`journal.record_spread_fill` upgrades a `STREAM` fill to confirmed only when the **same `fill_id`** is
re-recorded from `REST` (`journal._confirm_spread_fill`, `journal.py:1708-1740`). The docstring calls
`fill_id` "the broker's own execution id".

A `trade_updates` event carries `execution_id`. A REST order read carries `id` (the *order* id) and
`filled_avg_price` — **there is no per-execution identifier on the order object**, and
`execution._broker_order_from` reads exactly those fields. So a REST-sourced `fill_id` must be
synthesised (`f"{order_id}:rest"` or similar) and will never equal the stream's execution UUID.

Whichever way the caller resolves it:

- **Record both under their natural ids** → the same economic fill exists as two rows.
  `realised_pnl_from_fills` double-counts, and `_fills_between` (`journal.py:1995`) double-explains a
  vanished position.
- **Record both, ids differ** → the stream row keeps `confirmed_at IS NULL` forever →
  `RecoveryGap.UNCONFIRMED_FILLS` is permanently set → `RecoveryState.is_clean` is never true again. If
  the caller gates trading on `is_clean`, **the agent stops trading after its first fill.**
- **Record only REST** → journal's entire `FillSource.STREAM` path is dead code and the latency benefit
  is gone.

The two modules never agreed what a fill identifier is. This needs a decision before the first live fill.
The cheapest resolution is probably: the stream is the only source of `fill_id` (it is the only place an
`execution_id` exists), and REST confirmation is keyed by looking up the order and matching
`(client_order_id, cumulative filled_qty)` rather than by re-recording a fill.

Adjacent: `recover()` filters `unconfirmed_fills` and `unattributed_fills` by `trading_day`
(`journal.py:2440-2455`), so an unresolved gap from yesterday silently disappears from today's recovery.
Gaps that heal themselves at midnight without being fixed.

## 6. The exit path cannot be built from what is stored

Nothing builds exits yet, as expected. What specifically blocks each trigger:

- **`PositionRecord` has no expiry.** It carries `symbol` (the underlying), `spreads`, `max_loss`,
  `unrealised_pnl`, `net_delta`, `client_order_id`, `detail` (`journal.py:479-495`). You cannot compute
  days-to-expiry from it. The **time stop** and the **hard flatten** both need it, and both must instead
  go back to `OrderRecord.payload`, pull a leg's OCC symbol, and `occ.parse` it. That join is not written
  and `PositionRecord` is the obvious place for the field.
- **`RiskLimits.force_flat_days_before_expiry = 2` has zero readers.** It exists at `config.py:67` and
  nowhere else in the codebase.
- **There is no ET clock anywhere in the repo.** No `zoneinfo` import in any module. `risk.evaluate`
  takes `now_et: time` and nothing produces it. The 15:00 ET entry cutoff, the flatten cutoff, and
  `trading_day` all need one. (For this particular window UTC and ET dates coincide across all session
  times — 09:30-16:00 ET is 13:30-20:00 UTC — so `trading_day` is *accidentally* safe. The cutoffs are
  not.)
- **No function computes a closing debit.** `chain._conservative_credit` prices the opening credit.
  `chain._conservative_debit` prices a *debit vertical* and is a different computation. Closing a credit
  spread costs `short_leg.ask - long_leg.bid`. Both the **profit target** and the **loss limit** need
  this number and neither can be evaluated without it.
- **`build_closing_order` requires a `CreditSpread`** (`execution.py:515`) but only uses
  `spread.short_leg.symbol`, `spread.long_leg.symbol` and `spread.underlying`. The journal stores no
  `CreditSpread`, so the caller must fabricate one — including a dummy `credit` value that the function
  ignores — to call it. That is exactly the kind of glue that goes wrong quietly.
- **Regime break contradicts the module.** `regime.py:15-20` states as a design rule that the filter
  "only ever blocks entries. It never forces liquidation, because a forced exit into a disorderly tape is
  its own risk". The strategy spec's exit list says "Regime break: close or stop rolling when the filter
  turns hostile." The module refuses to be an exit trigger, and nothing distinguishes which `RegimeBlock`
  values warrant an exit from those that warrant a stand-down. This is a genuine design disagreement that
  needs a decision, not just code.
- **The flatten has no way to guarantee a fill.** `TIME_IN_FORCE = "day"` (`execution.py:92`) and
  `build_closing_order` produces a plain limit at whatever debit the caller passes. If it does not fill,
  the position sits into the window where Alpaca sells it out (GOTCHAS #10) — the uncontrolled outcome
  defined risk exists to prevent. Nothing escalates the limit price over successive cycles.
  `to_limit_price`'s `ROUND_CEILING` rounds a closing debit *up*, which helps marginally and is not a
  mechanism.

**Actively in the way:** the `day` default means every exit is an order the agent re-places each session,
so nothing protects an overnight gap; `build_closing_order`'s `CreditSpread` parameter forces
fabrication; and `regime.py`'s stated non-liquidation rule contradicts the spec.

## 7. NFP: the regime filter cannot deliver "flat into the event"

`RegimePolicy.event_lookahead_days` defaults to **1** (`regime.py:75-83`). The comment there is right
about why — setting it to the 5-day minimum DTE would block every entry for the whole week over a single
event, and the agent would stand down completely while logging a plausible reason.

But `KNOWN_EVENTS` contains non-farm payrolls on 2026-09-04, and the spec requires the agent be "flat or
deliberately positioned into it, by rule rather than by accident."

With a 1-day lookahead, entries block only on 3 and 4 September. A spread opened 2 September with 5-14
DTE is still open through NFP. **"Flat into NFP" is an exit rule, and there is no exit code.**

This is a known, dated event inside the judged window, so it is certain to occur rather than merely
likely.

## 8. `build_adapter`'s fail-closed claim is false

`build_adapter`'s docstring (`execution.py:1682`, in the paragraph beginning "One residual risk") states:

> Pass `sdk_client=None` and `primary=Backend.SDK` to make submission fail closed instead of falling
> through to the CLI.

That is the **default configuration**, and it does not fail closed. `ExecutionAdapter.submit`
(`execution.py:1379`) records an unavailable backend into `unusable` and `continue`s to the next one, so
with no SDK client the POST goes through the CLI — the transport the same docstring argues carries an
unquantified double-submission risk. `build_adapter` always sets `fallback`, and there is no argument
that suppresses it.

Also: `disable_automatic_retries` is only invoked inside `paper_trading_client()`. A caller who
constructs `SdkBackend(client=...)` with their own client keeps alpaca-py's default retry-on-429/504
behaviour on the order path, and `SdkBackend` does not check. Given the module's own analysis that 504 is
the dangerous code, the verification should live in `SdkBackend` rather than in one constructor.

## 9. Position snapshots do not create position events

Three separate calls have to happen in the right order, every cycle:

```python
snapshot_id = journal.record_position_snapshot(positions)   # journal.py:1894
for v in journal.vanished_positions():                      # journal.py:1962
    if not v.explained:
        journal.record_position_event(...)                  # journal.py:2011
```

`vanished_positions()` compares **only the two most recent snapshots**. So:

- Miss the `vanished_positions()` call in one cycle, and the disappearance is only ever visible in that
  one comparison. It is lost permanently.
- Call `record_position_snapshot` twice in a cycle (say, once in MONITOR and once in REVIEW) and the
  second diff compares two identical books, hiding a disappearance that happened before the first.

On a paper account the snapshot diff is the **only** same-day evidence of an assignment, exercise, expiry
or broker liquidation — GOTCHAS #12 is explicit that none of those reach the websocket or the activities
feed until the next day. This fragile, undocumented three-call protocol is the sole guard on the system's
single blind spot. It should be one method.

## 10. Parent fill sign is never validated

The journal refuses a non-positive `premium_per_contract` on a leg fill with a dedicated
`UnitConfusionError` (`journal.py:1770-1776`):

> a leg premium is always positive... A signed net price belongs on the parent, not on a leg.

There is **no corresponding check on the parent.** `record_spread_fill` and `mark_status` accept any
finite `net_price_per_spread`. Nothing verifies that an opening credit order's fill price is negative.
`execution._as_decimal` passes through whatever the broker sends.

If Alpaca ever returns a positive `filled_avg_price` for a credit fill — the convention is documented
(GOTCHAS #7) but the paper multi-leg fill model is explicitly undocumented (GOTCHAS #3) — then
`spread_realised_pnl` flips sign and every downstream number is wrong in a way that reads as an unlucky
day rather than a bug. That is precisely the failure mode the module docstring says it exists to prevent.

The journal already knows the order's intent: the stored `payload`'s `limit_price` sign is right there in
`OrderRecord.payload`. It could assert agreement and refuse.

## 11. `PositionRecord.max_loss` and `net_delta` have undeclared units

`PositionRecord` (`journal.py:479-495`) carries `spreads` **and** `max_loss` side by side with no
statement of whether `max_loss` is per-spread or the position total.

`risk.AccountState.open_risk` sums it as a total (`risk.py:78-80`). `CreditSpread.max_loss` supplies it
per-spread. A caller writing `max_loss=spread.max_loss` instead of `spread.max_loss * contracts`
understates aggregate open risk by the contract count, and the `AGGREGATE_RISK_CAP` gate passes when it
should deny.

Same for `net_delta`: `risk.OpenPosition` documents "equivalent shares of the underlying"; the journal
says nothing.

This is the class of confusion the journal's docstring claims to have eliminated ("Units and signs are
never conflated. No column here is named `qty` or `price`"), and it is the one place the discipline
lapses — ironically, in the dataclass whose docstring says "Field names mirror `risk.OpenPosition` so a
recovered book feeds the risk engine without a translation layer inventing anything on the way."

## 12. Naming: "contracts" means spreads across the entire risk seam

`risk.evaluate(max_loss_per_contract=...)` returns `Decision.contracts`, which
`build_opening_order(contracts=...)` maps to `MultiLegOrder.qty` — the number of **spreads**.
Numerically correct only because `ratio_qty` is 1 on both legs of a vertical.

`risk.py:257` computes `contracts * net_delta_per_contract`, so that parameter must be per-**spread**
(both legs summed) despite its name. A caller reading it as per-option-contract gets it wrong.

The journal is scrupulous about this vocabulary; `risk` and `execution` are not. Low severity for a 1:1
vertical, and a landmine if a condor is ever added as the spec's documented extension.

## 13. Stale GOTCHAS cross-references

GOTCHAS grew from 8 to 20 entries while the new modules were being built. Two references did not follow:

- `execution.py:52` cites `docs/GOTCHAS.md #12` for the broken MCP multi-leg serialization. That is now
  **#19**; #12 is paper assignment/expiry invisibility.
- `pyproject.toml:12` cites `docs/GOTCHAS.md #7` for the pandas pin. That is now **#20**; #7 is the
  negative limit price.

Every other cross-reference in `src/`, `tests/` and `pyproject.toml` checks out against the current
numbering.

## 14. Smaller items

- **`pyproject.toml:26` declares `underwriter = "underwriter.cli:app"`. There is no `cli.py`.** The
  console script is broken. `calibrate = "underwriter.calibrate:main"` is fine.
- `preflight.check_cli` runs `[path, "version"]` **without** passing `env=`, so it inherits
  `ALPACA_OUTPUT` from `.env` and `ALPACA_LIVE_TRADE` from the parent process. It is the only CLI
  subprocess in the codebase not using `execution.paper_environment()`. Harmless for `version`;
  inconsistent as a pattern, and GOTCHAS #18 exists because inherited `ALPACA_OUTPUT` causes silent
  empty output.
- **The BACKLOG item "`alpaca doctor` wired into the same preflight report" is now wrong.** GOTCHAS #17
  establishes that doctor is not a safety indicator — it prints "active profile: paper" against the live
  host. Preflight correctly does not use it. The backlog item should be struck rather than done.
- `data.atm_implied_vol` (`data.py:~130`) selects by strike distance **ignoring expiry entirely**. Given
  a chain slice spanning three expiries, which expiry's IV becomes `near_iv` — and the `near_dte`
  reported alongside it — is decided by dict iteration order among ties. That feeds `TermStructure`, and
  a missing or unusable curve blocks **all** entries via `regime.check_term_structure`. It should group
  by expiry first, then pick the nearest strike within the chosen expiry.
- **Open-interest screening does nothing on the live path.** `MarketData.contracts` never passes
  `open_interest`, so `Contract.open_interest` is always `None` and `min_open_interest = 100` never
  fires. This is consistent with GOTCHAS #4's rule (missing is tolerated), but it means a gate the spec
  leans on is inert outside calibration, which is the only place that performs the join.
- `journal._advance` raises `JournalError` when a terminal order changes to a *different* terminal status
  (`journal.py:1517-1525`). A realistic sequence — the stream reports `filled`, a later REST sweep
  reports `canceled` for a parent that partially filled and then had its remainder cancelled — hard-errors
  in the middle of reconciliation. The caller must wrap it or the monitor loop dies.
- `mark_status` stamps `reconciled_at` on every call, documented as "any status after `INTENT` came from
  the broker, so it is by definition a confirmation of the order's state at that moment." For
  `OrderStatus.UNKNOWN` written after an `UNKNOWN_OUTCOME`, that is false — nothing was confirmed. Low
  impact (`recover()` keys off status, not this field), but the audit trail asserts something untrue in
  the one case an auditor would care about.
- `client_order_id` does **not** hash `time_in_force`. Two orders differing only in `day` vs `gtc`
  collide on the id, and `record_intent` then raises `ConflictingIntentError` on the differing payload.
- `journal._attribute` (`journal.py:1699`) checks only that the `client_order_id` exists, not that the
  fill's symbol matches the order's symbol.
- `OrderResult.ok=True` does **not** mean filled, and via the recovery-lookup path it can carry
  `status="rejected"` or `"canceled"`: `_via` returns `ok=True` whenever the probe answers
  `Kind.ACCEPTED` with an order, and `_broker_order_from` accepts any status string. A caller doing
  `if result.ok: mark_submitted(...)` and treating it as an open position is wrong.
- `OrderResult.status` is `str | None`. On a dry run it is `DRY_RUN_STATUS = "dry_run_unvalidated"`,
  which `OrderStatus.from_broker` maps to `UNKNOWN` — non-terminal — so a journalled dry run becomes a
  permanently unreconciled order. On a failure it is `None`, and `OrderStatus.from_broker(None)` raises
  `AttributeError`.

---

# B. Answers to the specific questions asked

### Q1. Type and unit mismatches at every boundary

**`chain.CreditSpread` -> `execution.build_opening_order`.** Carries what the *order* needs: leg OCC
symbols, `underlying`, and `credit` as a per-share float that `to_limit_price` signs correctly. What it
does not carry is anything the *journal* needs.

- **No `cycle_id`.** Execution has no such concept; `record_decision` and `record_intent` both require
  one. The caller must generate and thread it.
- **No underlying symbol on the wire.** An `mleg` body has no top-level `symbol` (correctly — GOTCHAS
  #6), so `OrderResult.payload` cannot supply `journal.record_intent(symbol=...)`. The caller must carry
  `spread.underlying` separately, or reconstruct it with `occ.parse(leg_symbol).root` — which is not the
  same thing for an adjusted option.
- **`Decimal` vs `float`.** `OrderResult.filled_qty` and `filled_avg_price` are `Decimal | None`;
  `journal.mark_status` takes `float | None`. `journal._finite()` coerces silently (`value != value`
  works on `Decimal`, comparison against `float('inf')` works, and it returns `float(value)`), so this
  functions correctly today. It is a type-checker complaint and an invitation to a future mistake, not a
  live bug.

**`journal.RecoveryState` -> `risk.AccountState`.** Field names mirror, types do not, and the mismatch is
load-bearing — see finding #3. Also missing entirely from the journal and required by `AccountState`:
`equity` and `options_buying_power`. Both must come from an account read that does not exist.

**Every place a caller must write glue, and where it can go wrong:**

| Boundary | Glue required | How it goes wrong |
|---|---|---|
| `CreditSpread` -> `record_intent` | supply `cycle_id`, `symbol=spread.underlying` | forgetting the underlying is not in the payload |
| `Decision.contracts` -> `qty` | none (identity) | the word "contracts" means spreads (#12) |
| `OrderResult.status` -> `OrderStatus` | `from_broker`, guarding `None` | `AttributeError` on failure; dry runs become permanent `UNKNOWN` |
| `OrderResult.filled_*` -> `mark_status` | `Decimal` -> `float` | silent, works today |
| Alpaca positions -> `PositionRecord` | spread reassembly (does not exist) | `max_loss` per-spread vs total (#11) |
| `PositionRecord` -> `OpenPosition` | field-by-field copy | same unit trap |
| `RecoveryState` -> `AccountState` | `None` handling | `or 0.0` disarms the daily stop (#3) |
| `CreditSpread` -> `net_delta_per_contract` | delta arithmetic (does not exist) | falsy `0.0` disables the cap (#2) |

### Q2. Contradictions between the two modules

**They agree where it counts.**

- *Sign convention:* both use negative-is-credit, on the limit price and on the parent fill price.
  `spread_realised_pnl` applies the negation once, to both sides, for exactly the reason its docstring
  gives. No disagreement.
- *Parent vs leg units:* both encode GOTCHAS #8 identically — parent in spreads at a signed net, leg in
  contracts at a positive premium.
- *Identifiers:* both key on `client_order_id`, and the digest is deterministic in a way that makes
  `record_intent`'s idempotency and `get-by-client-id` recovery work together. The only gap is
  `time_in_force` not being hashed (#14).
- *Timezone:* both are UTC-native and consistent with each other. The contradiction is with the **spec**,
  which is ET-based, and neither module can produce ET.

**Where they disagree.**

- *What a fill is:* finding #5. Journal's confirmation model requires a shared execution id; execution's
  REST path cannot produce one.
- *Fail-closed on unknown numbers:* finding #3, and it is `risk` rather than `execution` on the other
  side.
- *"Terminal":* the same word means different things — `execution.Kind.TERMINAL` means "no order was
  created and none will be"; `journal.OrderStatus.is_terminal` means "the order's fate is settled". Not a
  bug, but they are not interchangeable and the naming invites treating them as such. The practical
  consequence is #14's `ok=True` with a rejected status.

### Q3. What is missing to run one cycle end to end

Section C. This is the substance of the review.

### Q4. Risk-engine integration

**`max_loss_per_contract`: yes, directly.** `CreditSpread.max_loss` is `(width - credit) * 100` — dollars
per spread, which is exactly what `size_position` divides the budget by and what `qty` then multiplies.
The units are consistent end to end despite the name.

One unwired dependency: nobody sets `CreditPolicy.max_loss_per_contract` to
`risk.max_risk_dollars(equity, limits)`. It defaults to `None`, so the selector can return a structure
the risk engine will then size to zero contracts. At $100k equity and 0.5% per trade the budget is $500,
and the default `max_width = 10.0` admits spreads with up to $1000 of max loss. `chain.py` built the
`EXCEEDS_RISK_BUDGET` rejection specifically to make this displayable rather than silent; it is inert
until wired.

**`net_delta_per_contract`: no, not in the common case.** Finding #2. The Basic plan's missing Greeks are
not an edge case — `select_credit_vertical` has a whole fallback path for them — and the cap defaults to
skipping itself with no record. The delta cap does silently become inert, which is the exact question
asked, and the answer is yes.

### Q5. The exit path

Finding #6, in full. Summary of what must still be written per trigger:

| Trigger | Missing |
|---|---|
| Profit target | closing-debit function; the received credit is available as `OrderRecord.net_price_per_spread` |
| Loss limit | same closing-debit function |
| Regime break | a policy mapping `RegimeBlock` values to exit-vs-stand-down; `regime.py` declines to trigger exits by design |
| Time stop | expiry on the position; `force_flat_days_before_expiry` has no readers |
| Hard flatten | expiry on the position, an ET clock, and a price-escalation loop so the limit actually fills |

Actively in the way: `PositionRecord`'s missing expiry, `build_closing_order`'s `CreditSpread` parameter,
`regime.py`'s stated non-liquidation rule, and the `day` TIF meaning exits never rest.

### Q6. Failure interaction

**On `UNKNOWN_OUTCOME`, the caller should:**

1. `journal.mark_status(cid, OrderStatus.UNKNOWN, detail=result.message)` — non-terminal, so the order
   stays on `unreconciled_orders()` and drives `RecoveryGap.UNRECONCILED_ORDERS`.
2. **Not** call `abandon()`. That is reserved for proven absence, and its docstring says so.
3. Block new entries on that symbol until it is reconciled.

The journal supports all three. Nothing enforces any of them, and step 3 is finding #4 — the concrete
path to a doubled position.

**If the journal's write fails after a successful submission**, the design holds. The `INTENT` row was
committed before the POST, so recovery finds it, `unreconciled_orders()` returns it, and the caller looks
it up by `client_order_id`. No untracked position. `synchronous=FULL` (`journal._configure`) exists
specifically so the newest transaction — the intent written moments before submitting — survives a power
loss. This is the part of the design that works best.

The unprotected direction is the reverse: submit-then-journal. `ExecutionAdapter` holds no journal
reference, so **nothing structurally enforces the one ordering rule the journal exists for.** It is a
convention in a docstring.

**Sequences that leave an untracked live position:**

- *Yes:* finding #4's cross-cycle duplicate. The second order is invisible in the journal because
  `record_intent` no-ops on the identical id, so the broker holds two spreads and the journal records
  one.
- *Yes, partially:* finding #5's permanently-unconfirmed fills combined with day-scoped recovery gaps —
  the gap that should force a human look disappears at midnight.
- *No:* a fill on a broker-initiated order is caught (`UNKNOWN_ORDER` -> `UNATTRIBUTED_FILLS`, GOTCHAS
  #14 handled).
- *No:* an unobserved book with orders on file is caught (`POSITIONS_UNOBSERVED`).
- *No:* a crash between `record_intent` and `submit` is caught (the write-ahead rule).

### Q7. Older modules invalidated by the new gotchas

Checked `chain`, `risk`, `regime`, `volatility`, `preflight` against all 20 entries.

- **Does anything assume `time_in_force` is day-only (#11)?** No. `execution.VALID_TIME_IN_FORCE` is
  `{day, gtc}` and no other module touches TIF. Nothing stale.
- **Does anything treat `--dry-run` as validation (#15)?** No. `validate()` runs before every submission,
  `_interpret_dry_run` is explicit that exit 0 establishes only that the CLI parsed the flags, and
  `preflight.check_cli` uses `alpaca version` rather than a dry run.
- **Does anything treat the stream as authoritative (#13)?** No. Only `journal` references the stream,
  and it does so correctly — `STREAM` fills are stored unconfirmed by construction.

**What is now wrong:**

- The two stale cross-references (#13 above).
- The BACKLOG's `alpaca doctor` item, invalidated by GOTCHAS #17.
- A gap rather than a contradiction: **GOTCHAS #8's `nested=true` requirement has no implementation at
  all.** There is no `order list` code anywhere in `src/`, and `get-by-client-id` returns the parent only
  (`OrderResult`'s docstring says so). Leg-level reconciliation cannot happen today.
- Also a gap: GOTCHAS #12's activities polling for `OPASN`/`OPEXC`/`OPEXP` has no implementation, so
  `confirm_position_event` has no caller and every inferred exit stays inferred forever.

---

# C. What still has to be written

From "calibration says XLE is a candidate" to "an order is submitted and journalled".

**There is currently no orchestrator.** No module imports both `execution` and `journal`, and
`pyproject.toml`'s declared entry point `underwriter.cli:app` does not exist. Everything below is new
code.

Signatures are proposals — they are written to match the existing call sites exactly, so they can be
pasted and filled in.

## C.1 Infrastructure (nothing exists)

**1. `src/underwriter/clock.py`** — the ET clock. Every session rule in the spec is ET; there is no
`zoneinfo` import in the codebase.

```python
ET = ZoneInfo("America/New_York")

def now_et(now: datetime | None = None) -> datetime: ...
def session_time_et(now: datetime | None = None) -> time:
    """The `now_et` argument risk.evaluate requires."""
def trading_day(now: datetime | None = None) -> date:
    """The ET calendar day every journal writer keys on."""
def past_flatten_cutoff(expiry: date, now: datetime | None = None, *, cutoff: time = time(14, 30)) -> bool:
    """True on expiration day past the hard flatten cutoff. GOTCHAS #10."""
```

**2. `cycle_id` generation.** Required by `record_decision` and `record_intent`; execution has no such
concept. Something stable and sortable — `f"{trading_day.isoformat()}-{seq:03d}"` or a ULID. One per pass
of the state machine, threaded through every stage.

**3. `src/underwriter/account.py`** — the account reader. `preflight.AccountLike` exists as a Protocol
but nothing fetches the values, and `AccountState` needs two fields the journal cannot supply.

```python
class AccountReader:
    def __init__(self, api_key: str, secret_key: str) -> None: ...
    def snapshot(self) -> AccountSnapshot:
        """equity, options_buying_power, last_equity, status, options_trading_level."""
    def positions(self) -> list[RawOptionPosition]:
        """GET /v2/positions — individual option contracts, not spreads."""
    def orders(self, *, nested: bool = True) -> list[dict]:
        """GET /v2/orders?nested=true. GOTCHAS #8 — mandatory, and unimplemented today."""
    def activities(self, *, since: date) -> list[dict]:
        """GET /v2/account/activities for OPASN/OPEXC/OPEXP. GOTCHAS #12."""
```

Note `preflight` already declares the Protocol shape for the account fields; reuse it rather than
inventing a second one.

## C.2 Signal path glue

**4. Per-symbol ranking loop** — do not use `rank_universe` for the regime input.

`rank_universe` returns `Skipped` for below-floor instruments, which **discards their
`realised_is_expanding` flag**. So `expanding_flags` computed from its output samples only
floor-clearing instruments, and `regime.check_volatility_expansion` measures a biased subset. The caller
must call `rank_instrument` per symbol and collect flags across the whole universe:

```python
def rank_and_flag(
    bars: Bars, ivs: Mapping[str, float | None], policy: VolPolicy
) -> tuple[list[VolRanking], list[Skipped], list[bool]]:
    """Returns (candidates above floor, skips with reasons, expansion flags for ALL symbols)."""
```

**5. Term-structure construction** — nothing builds it, and a missing curve blocks every entry.

```python
def spy_term_structure(market: MarketData, today: date) -> TermStructure | None:
    near = ExpiryWindow.from_dte(today, risk.min_days_to_expiry, risk.max_days_to_expiry)  # 5-14
    far  = ExpiryWindow.from_dte(today, 30, 60)   # >= near_dte + 14, per min_term_structure_gap_days
    spot = ...
    return data.term_structure_from(market.chain("SPY", near), market.chain("SPY", far),
                                    underlying_price=spot)
```

Fix `data.atm_implied_vol` first (finding #14) — group by expiry, then pick the nearest strike within the
chosen expiry, so `near_dte` is not decided by dict ordering.

**6. Spot price.** `calibrate` uses `closes[-1]`, which is ~20 minutes stale via `data.SIP_EMBARGO`.
Acceptable for a ±3-4% moneyness band and for strike distance; state it as a known approximation rather
than discovering it later.

**7. The AI catalyst veto.** Known and stated as not built. Its contract with the journal is
`record_decision(stage=Stage.VETO, accepted=False, reasons=[...])`, and per the spec a missing or
malformed response is a veto.

## C.3 Risk seam

**8. `RecoveryState -> AccountState`, refusing on `None`** (finding #3):

```python
def account_state(
    recovery: RecoveryState, snapshot: AccountSnapshot
) -> AccountState | RiskUnavailable:
    """None session-open equity or None realised P&L must REFUSE, never default to 0.0.
    risk.evaluate silently skips the daily loss stop when starting_equity <= 0."""
```

**9. `PositionRecord -> risk.OpenPosition`**, with the units pinned (finding #11):

```python
def to_open_positions(book: PositionBook) -> tuple[OpenPosition, ...]:
    """PositionRecord.max_loss is the POSITION TOTAL in dollars, not per spread.
    PositionRecord.net_delta is share-equivalents for the whole position."""
```

Write that as an assertion, not a comment: `assert p.max_loss >= 0` plus a sanity bound against
`p.spreads * plausible_max_per_spread`.

**10. `net_delta_per_spread`** (finding #2):

```python
def net_delta_per_spread(spread: CreditSpread) -> float | None:
    """(-delta_short + delta_long) * 100, in share equivalents per spread.
    None when the short leg's delta is missing. When only the long leg's is
    missing, treat it as 0.0 — that overstates the position delta, the safe
    direction — and record that it was estimated."""
```

Then change `risk.evaluate`'s parameter to `float | None` and add a `Denial` so an inert cap is visible
in the audit log.

**11. Wire the selector's risk ceiling:**

```python
credit_policy = replace(CreditPolicy(), max_loss_per_contract=risk.max_risk_dollars(equity, limits))
```

Without this the `EXCEEDS_RISK_BUDGET` rejection never fires and sizing silently floors to zero.

## C.4 Execute seam — the sequence

**12. The submit path, in this exact order:**

```python
def open_position(
    journal: Journal, adapter: ExecutionAdapter, *, cycle_id: str,
    spread: CreditSpread, contracts: int,
) -> OrderResult:
    # (a) ENTRY GATE — finding #4. Without this, a prior UNKNOWN_OUTCOME on this
    #     symbol lets cycle N+1 rebuild the identical client_order_id and submit again.
    blocking = [o for o in journal.unreconciled_orders() if o.symbol == spread.underlying]
    if blocking:
        journal.record_decision(cycle_id=cycle_id, stage=Stage.EXECUTE, accepted=False,
                                symbol=spread.underlying, reasons=["unreconciled_order"])
        return ...

    order = build_opening_order(spread, contracts=contracts)

    # (b) WRITE-AHEAD — committed before the POST. Nothing may reorder these two.
    journal.record_intent(
        client_order_id=order.client_order_id,
        cycle_id=cycle_id,
        symbol=spread.underlying,          # NOT in the mleg payload — carry it separately
        spreads=float(order.qty),
        payload=order.as_payload(),
    )

    result = adapter.submit(order)

    # (c) STATUS — guard the None and the dry-run sentinel
    status = OrderStatus.from_broker(result.status) if result.status else OrderStatus.UNKNOWN
    journal.mark_status(
        order.client_order_id, status,
        spreads_filled=result.filled_qty,          # Decimal; _finite coerces
        net_price_per_spread=result.filled_avg_price,
        broker_order_id=result.order_id,
        detail=result.message,
    )

    # (d) FAILURE BRANCH
    #     abandon() ONLY when absence was proven. Today that means reading
    #     result.reason: UNKNOWN_OUTCOME means absence was NOT proven; any other
    #     reason on a non-ok result after the lookup means it was. Add
    #     OrderResult.proven_absent so this is not a reason-enum inference.
    return result
```

**13. Add `OrderResult.proven_absent: bool`** to `execution.py`. Two lines: set it `True` in the
"absence proven, attempts exhausted" branch (`execution.py:1667`), `False` everywhere else. It is the
single most important fact the module establishes and it is currently thrown away.

**14. `record_decision` at every stage.** Nothing calls it. The README's claim of "39 distinct refusal
reasons, every refusal recorded and displayed" depends entirely on this being written:

```python
def record_stage(journal, cycle_id, stage, symbol, outcome) -> None:
    """Rejection/Skip/Denial/RegimeBlock are all StrEnum and store as their own
    strings — journal deliberately does not import them."""
```

**15. `record_regime_verdict` from `RegimeVerdict`.** The mapping is clean
(`blocks=[b.reason.value for b in verdict.blocks]`, `detail=[b.detail for b in verdict.blocks]`), and
nobody calls it. The spec says the filter should be judged on whether it fired, which requires the
verdicts be on disk whether or not they blocked.

## C.5 Monitor — the largest unwritten block

**16. Alpaca positions -> `PositionRecord`.** This is the biggest single missing piece.

`GET /v2/positions` returns individual option contracts, not spreads. Reassembly requires:

```python
def reassemble_spreads(
    raw: Sequence[RawOptionPosition], orders: Mapping[str, OrderRecord],
    quotes: Mapping[str, Quote], deltas: Mapping[str, float | None],
) -> list[PositionRecord]:
    """
    1. occ.parse each symbol -> (root, expiry, strike, right)
    2. group by (underlying, expiry, right)
    3. pair short (qty < 0) with long (qty > 0); spreads = min(|qty_short|, qty_long)
    4. width = |strike_short - strike_long|
    5. credit = |OrderRecord.net_price_per_spread| from the opening order
       (net_price_per_spread is SIGNED and negative for a credit)
    6. max_loss = (width - credit) * 100 * spreads     # POSITION TOTAL, see #11
    7. net_delta = (-d_short + d_long) * 100 * spreads # share equivalents, refreshed
    8. unrealised_pnl from current quotes, not from the broker's per-leg figure
    """
```

Required **every cycle**, both for the risk engine's `open_risk`/`net_delta` and for the snapshot diff
that is the only same-day assignment signal. None of it exists.

**17. The snapshot protocol as one atomic call** (finding #9):

```python
def observe_book(journal: Journal, positions: Sequence[PositionRecord], *, day: date) -> list[PositionEvent]:
    """snapshot -> vanished_positions -> record_position_event, in one place.
    vanished_positions() only ever compares the two most recent snapshots, so
    skipping this in one cycle loses the disappearance permanently."""
```

**18. A REST order sweep with `nested=true`** (GOTCHAS #8). No code exists. `get-by-client-id` returns
the parent only. Needed to reconcile `unreconciled_orders()` and to get leg fills at all.

**19. Fill recording**, after the finding #5 decision is made. Whichever way it goes:

```python
def record_fills_from_stream(journal, event) -> None:   # fill_id = event.execution_id, source=STREAM
def confirm_fills_from_rest(journal, order) -> None:    # must NOT re-record under a synthetic id
```

**20. Activities polling -> `confirm_position_event`.** Nothing exists. Runs once at the start of each
session for the previous day, per GOTCHAS #12. Note `OPEXC` is correct and `OPXRC` is a documentation
error.

**21. P&L recording, both series.** `record_pnl(source=OFFICIAL)` from the account, and
`record_pnl(source=SHADOW)` from quoted exit prices per GOTCHAS #3.

**Critical scheduling constraint:** `journal._realised_today` (`journal.py:2474`) returns `None` unless a
P&L snapshot exists **dated after the day's last fill**. So a snapshot must be recorded after every fill,
or `recover()` is permanently gapped on `REALISED_PNL_UNKNOWN` — which, per finding #3, is the input the
daily loss stop needs.

**22. `record_reconciliation(scope=FULL, ok=True)`** after each successful sweep. Without it `view_age`
stays `None`, `VIEW_STALE` never clears, and `RecoveryState.is_clean` is never true.

**23. `record_session_open_equity`** once at the first cycle of each ET trading day. Unrecoverable after
the fact — if it is missed, the daily loss stop has no baseline for the rest of the session.

## C.6 Exit — nothing exists

**24. Closing price:**

```python
def closing_debit(short_leg: Contract, long_leg: Contract, economics: SpreadEconomics) -> float | None:
    """What it costs to flatten: buy back the short (pay ask), sell the long (receive bid).
    Mirror of chain._conservative_credit. Returns None when either leg lacks a quote."""
```

**25. Position -> exit order:**

```python
def build_exit(order: OrderRecord, quotes: Mapping[str, Quote], contracts: int, debit: float) -> MultiLegOrder:
    """OrderRecord.payload -> OCC-parse both legs -> the short leg is the one whose
    payload position_intent is 'sell_to_open' -> build_closing_order.
    Simpler if build_closing_order takes (short_symbol, long_symbol, underlying)
    instead of a CreditSpread the journal cannot produce."""
```

**26. The five triggers:**

```python
def exit_reason(
    position: PositionRecord, order: OrderRecord, current_debit: float,
    regime: RegimeVerdict, now_et: datetime, expiry: date, limits: RiskLimits,
) -> ExitReason | None:
    """
    PROFIT_TARGET  current_debit <= profit_take_fraction * |order.net_price_per_spread|
    LOSS_LIMIT     current_debit >= loss_multiple * |order.net_price_per_spread|
    REGIME_BREAK   needs a policy: which RegimeBlock values force an exit rather
                   than a stand-down. regime.py declines to answer this by design.
    TIME_STOP      (expiry - today).days <= limits.force_flat_days_before_expiry
                   — currently has no readers anywhere
    HARD_FLATTEN   clock.past_flatten_cutoff(expiry, now) — GOTCHAS #10
    """
```

**27. Flatten escalation.** A `day` limit that does not fill leaves the position for Alpaca to sell out.
Re-place at a worse debit each cycle inside the flatten window, or submit marketable-through-the-spread.
Either way it must be written; `ROUND_CEILING` is not a mechanism.

**28. Expiry on `PositionRecord`** — or the OCC join written once, in `reassemble_spreads`, and carried
on the record. Without it the time stop and the hard flatten both need an ad-hoc lookup through
`OrderRecord.payload`.

## C.7 Two decisions to make before writing any of it

1. **What is a `fill_id`?** (finding #5.) Everything in C.5 depends on it, and getting it wrong either
   double-counts P&L or permanently gaps recovery.
2. **Does the delta cap run at all?** (finding #2.) If Greeks are absent on the live chain, decide now
   whether the answer is "estimate conservatively" or "deny the trade" — not at 09:35 on Monday.

And before either: **test finding #1 against a real market-hours chain at the 0.15-0.30 delta strikes.**
If the 10%-of-mid filter rejects them, nothing else in this document matters, because there will be no
trades to journal.
