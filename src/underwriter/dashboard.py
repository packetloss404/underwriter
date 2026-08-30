"""The read-only dashboard: everything the agent did, and everything it refused.

This module serves the journal. It never writes to it, and it exposes no way
to write to it -- that is not a convention here, it is the design.

**Read-only is structural, not aspirational.** Every route is registered with
`@app.get`, there is no route that places, cancels or modifies an order, none
that touches the kill switch in either direction, and none that mutates a row.
The app is handed a `JournalGateway` whose only public method runs a callable
against the journal, and every callable this module passes it is a query. A
test enumerates the router and asserts the method set of every route is a
subset of `{GET}`, so a mutation added later fails the suite rather than the
account. The dashboard is the part of this system a stranger can reach, and a
stranger must not be able to trade with it.

**The refusals are the product.** `/api/rejections` is the headline: this
strategy declines far more often than it trades, and a filter chain that
cannot show its work is indistinguishable from one that is broken. Reason
codes are the journal's own `StrEnum` values, rendered verbatim, grouped and
counted. One rejection can carry several reason codes, so the grouped counts
sum to more than the number of rejections -- the payload says so rather than
quietly presenting a total that does not add up.

**Units are labelled at the boundary.** Parent-level figures are in strategy
units (spreads) at a SIGNED net price, negative for a credit; leg-level
figures are in contracts at that leg's own positive premium (docs/GOTCHAS.md
#8). Field names carry the unit -- `spreads`, `contracts`,
`net_price_per_spread_signed`, `..._usd` -- and every payload that carries
money ships a `units` block naming the convention for each of its fields. A
dashboard that renders a number without its unit is how a per-spread max loss
gets read as a position total.

**The two P&L series never touch.** `official` is what Alpaca's paper engine
reports and `shadow` is our own conservative pricing across the quoted spread.
They are returned as separate series, each flagged with how far it can be
trusted, and nothing here adds, averages or reconciles them: the paper fill
model is undocumented (docs/GOTCHAS.md #3) and a single blended number would
hide exactly the discrepancy the shadow series exists to show.

**An unknown is never a zero.** `realised_pnl_today_usd` is `null` when the
journal could not vouch for it, because a null renders as "unknown" while a
0.0 renders as "flat" -- and flat is a claim. The same rule governs the empty
journal: a book that has never been observed reports `observed: false` with an
empty position list, which is a different fact from an observed-empty book,
and both render without error. We have not traded yet, so that is the state
the dashboard is most often in and the one it must handle best.

**The front page is one request.** `/api/overview` answers "what is it doing
and how much money is there" in a single read. The alternative -- stitching it
in the browser from `/api/state`, `/api/positions` and `/api/pnl` -- reads a
journal a live agent is writing to three times, and renders the equity from one
moment beside the open risk from another. A headline nobody can reproduce is
worse than a slower one. Every money figure on it is the official series and
the payload names the series, because the two must never be added.

**Staleness is on every response.** Each payload carries `generated_at`, the
`data_as_of` moment of the newest datum behind it, and `data_age_seconds`, so
the UI can say how old the picture is instead of implying it is live.

**The journal connection is confined to one thread.** SQLite objects belong to
the thread that created them, and a threaded ASGI server hands each request to
whichever worker is free. `JournalGateway` therefore owns the connection on a
single-worker executor and marshals every read onto it, which keeps one WAL
reader open for the process rather than reopening (and re-verifying the schema
of) the database on every request.
"""

from __future__ import annotations

import math
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Final

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from underwriter.config import RiskLimits
from underwriter.journal import (
    CONTRACT_MULTIPLIER,
    DEFAULT_MAX_VIEW_AGE,
    MEMORY,
    SCHEMA_VERSION,
    DecisionRecord,
    IntentLeg,
    Journal,
    JournalError,
    OrderRecord,
    PnlSnapshot,
    PnlSource,
    PositionRecord,
    ReconciliationScope,
    RecoveryGap,
    SpreadFill,
    trading_day_of,
)
from underwriter.occ import OccParseError, OccSymbol
from underwriter.occ import parse as parse_occ
from underwriter.runtime import Schedule

# Caps on what one request may ask for. A dashboard that will happily render
# the entire journal in one response is a denial-of-service tool pointed at the
# machine the agent trades from.
MAX_LIMIT: Final = 1_000
DEFAULT_DECISION_LIMIT: Final = 100
DEFAULT_REJECTION_LIMIT: Final = 500
DEFAULT_ORDER_LIMIT: Final = 50
DEFAULT_PNL_DAYS: Final = 30
MAX_PNL_DAYS: Final = 365

# How far back the overview looks for facts the journal files by day. The scan
# is bounded for the same reason every other limit here is, and where a bound
# can hide a row the payload reports that rather than presenting a floor as a
# count.
DEFAULT_OVERVIEW_SCAN: Final = 500
OVERVIEW_EQUITY_DAYS: Final = 30
OVERVIEW_WATCHING_LIMIT: Final = 100

REDACTED: Final = "[redacted]"

# Redaction is by key name and by value shape, and it is deliberately blunt.
# Decision context is written by the cycle and holds strikes, IVs and widths;
# nothing here should ever carry a credential. Blunt is the right setting for a
# thing that must never be wrong in one direction.
_SECRETISH_KEY: Final = re.compile(
    r"key|secret|token|password|passwd|credential|bearer|authorization", re.IGNORECASE
)
_SECRETISH_VALUE: Final = re.compile(r"^\s*(sk-|sk_|pk_|bearer\s)", re.IGNORECASE)

# How deep redaction walks a nested context before it stops looking. Context is
# a shallow record of what was considered; anything deeper is malformed.
_MAX_REDACT_DEPTH: Final = 6

MONEY_UNITS: Final[Mapping[str, str]] = {
    "spreads": "strategy units (spreads), never contracts -- docs/GOTCHAS.md #8",
    "contracts": "option contracts (ratio_qty x spreads), 100 shares each",
    "net_price_per_spread_signed": (
        "US dollars per share, SIGNED: negative is a credit received, positive a "
        "debit paid -- docs/GOTCHAS.md #7"
    ),
    "credit_per_spread_usd": "US dollars per spread (100 shares), magnitude only",
    "credit_received_usd": ("US dollars for the whole position, negative when a debit was paid"),
    "premium_per_contract_usd": "US dollars per share for that leg alone, always positive",
    "max_loss_usd": "US dollars, position total (not per spread)",
    "unrealised_pnl_usd": "US dollars, position total; 0.0 when quotes were unavailable",
    "realised_pnl_usd": "US dollars for the session; null means unknown, never zero",
    "equity_usd": "US dollars, account equity as reported at that moment",
    "net_delta": "share-equivalent delta, position total",
}

STATE_UNITS: Final[Mapping[str, str]] = {
    "session_open_equity_usd": MONEY_UNITS["equity_usd"],
    "realised_pnl_today_usd": MONEY_UNITS["realised_pnl_usd"],
    "age_seconds": "seconds, wall clock",
}

OVERVIEW_UNITS: Final[Mapping[str, str]] = {
    "equity_usd": MONEY_UNITS["equity_usd"],
    "session_open_equity_usd": MONEY_UNITS["equity_usd"],
    "realised_today_usd": MONEY_UNITS["realised_pnl_usd"],
    "unrealised_usd": "US dollars, book total as the official series last marked it",
    "day_pnl_usd": "US dollars, realised plus unrealised on the official series alone",
    "day_pnl_pct": "percent of the session-open equity, which is what the loss stop measures",
    "open_risk_usd": "US dollars, the sum of every open position's max loss",
    "open_risk_pct_of_equity": "percent of current equity currently at risk",
    "risk_cap_pct": "percent of equity RiskLimits.max_total_open_risk_pct allows open at once",
    "spreads": MONEY_UNITS["spreads"],
    "net_delta": MONEY_UNITS["net_delta"],
    "seconds_since_last_cycle": "seconds, wall clock",
    "vrp_ratio": (
        "implied over realised volatility, a pure ratio and not a percentage: "
        "1.30 means implied sits 30% above realised"
    ),
    "implied_vol": "annualised volatility as a FRACTION, not percent: 0.22 is 22%",
    "realised_vol": (
        "annualised volatility as a FRACTION, over a window matched to the tenor "
        "sold -- the denominator of vrp_ratio, not a longer trailing window"
    ),
}

# Prose that travels with the payload, because each of these is a number a
# reader will otherwise assume means something it does not.
OVERVIEW_NOTES: Final[Mapping[str, str]] = {
    "pnl_series": (
        "every money figure here is the OFFICIAL series -- Alpaca's own paper "
        "numbers, reported and not trusted (docs/GOTCHAS.md #3). The shadow series "
        "is never added to it or averaged with it; read the two side by side at "
        "/api/pnl."
    ),
    "unknown_is_not_zero": (
        "any figure the journal cannot vouch for is null beside a _known flag. A "
        "zero here is a measurement, never a missing reading."
    ),
    "market_open": (
        "the regular US equity-options session on the exchange clock. Weekends are "
        "excluded and holidays are NOT: on a market holiday this reads open while "
        "the broker is shut. It is a schedule, not a calendar."
    ),
    "cycles_today": (
        "distinct cycle ids that recorded a decision or a regime verdict today, "
        "over a bounded scan. Nothing registers a cycle as such, so a pass that "
        "recorded neither is invisible here: this is a floor, and "
        "cycles_today_complete says whether the scan reached the start of the day."
    ),
    "watching": (
        "the instruments the last recorded cycle considered, with the verdict it "
        "reached and the volatility figures it measured. vrp_ratio, implied_vol "
        "and realised_vol all come from one decision -- the newest in the cycle "
        "that measured that instrument -- so the ratio equals its own quotient. "
        "They are null together on a decision recorded before the cycle began "
        "journalling them, and measured_at says which decision they came from."
    ),
    "fills": (
        "parent executions in strategy units (docs/GOTCHAS.md #8), not contracts "
        "and not legs. Direction comes from the position_intent on the order's "
        "legs; a fill on no order of ours -- a broker liquidation before expiry -- "
        "counts as unclassified rather than being guessed into open or close."
    ),
}

_MISSING_PAGE: Final = """<!doctype html>
<title>Underwriter dashboard</title>
<h1>Underwriter dashboard</h1>
<p>The static page has not been built yet. The read-only API is live:</p>
<ul>
  <li><a href="/api/overview">/api/overview</a></li>
  <li><a href="/api/state">/api/state</a></li>
  <li><a href="/api/positions">/api/positions</a></li>
  <li><a href="/api/decisions">/api/decisions</a></li>
  <li><a href="/api/rejections">/api/rejections</a></li>
  <li><a href="/api/pnl">/api/pnl</a></li>
  <li><a href="/api/orders">/api/orders</a></li>
  <li><a href="/api/health">/api/health</a></li>
</ul>
"""


def utcnow() -> datetime:
    """The default clock. Injected through `DashboardConfig` so tests can pin it."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    """Everything the app needs from outside itself.

    The journal path arrives here rather than being read from the environment
    inside a route, so a test runs the whole app against `:memory:` and two
    apps in one process cannot end up sharing a database by accident.
    """

    journal_path: str | Path = MEMORY
    static_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "static")
    max_view_age: timedelta = DEFAULT_MAX_VIEW_AGE
    pnl_days: int = DEFAULT_PNL_DAYS
    clock: Callable[[], datetime] = utcnow
    # The risk cap and the session hours are read, never applied: the overview
    # shows headroom against the same limits the agent trades under, and the
    # same schedule it runs on, rather than a second copy of either that can
    # drift from the one in force.
    limits: RiskLimits = field(default_factory=RiskLimits)
    schedule: Schedule = field(default_factory=Schedule)


class JournalGateway:
    """Owns one journal connection on one thread and runs reads on it.

    A SQLite connection may only be used from the thread that opened it, and a
    threaded ASGI server dispatches each request to whichever worker is free.
    Opening a journal per request would work but re-verifies the schema every
    time and multiplies open file handles under load, so instead the connection
    lives on a single-worker executor and every read is marshalled onto it.
    Reads are therefore serialised, which is the right trade for a dashboard
    reading a database whose writer is one trading process.

    `run` takes a callable rather than exposing the journal, so the routes can
    only reach it through functions this module wrote -- all of which are
    queries.
    """

    __slots__ = ("_journal", "_open", "_path", "_pool")

    def __init__(
        self,
        path: str | Path = MEMORY,
        *,
        open_journal: Callable[[str | Path], Journal] = Journal,
    ) -> None:
        self._path = path
        self._open = open_journal
        self._journal: Journal | None = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="journal-read")

    def run[T](self, work: Callable[[Journal], T]) -> T:
        """Run one function against the journal, on the thread that owns it."""
        return self._pool.submit(self._apply, work).result()

    def _apply[T](self, work: Callable[[Journal], T]) -> T:
        if self._journal is None:
            self._journal = self._open(self._path)
        return work(self._journal)

    def close(self) -> None:
        journal = self._journal
        if journal is not None:
            self._pool.submit(journal.close).result()
            self._journal = None
        self._pool.shutdown(wait=True)


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _opt_iso(moment: datetime | None) -> str | None:
    return None if moment is None else _iso(moment)


def _age_seconds(now: datetime, moment: datetime | None) -> float | None:
    """How old a datum is, or None when there is no datum.

    None is not zero. A view that has never been established is staler than any
    number, and rendering it as a fresh zero is the one mistake this field
    exists to prevent.
    """
    if moment is None:
        return None
    return max(0.0, (now.astimezone(UTC) - moment.astimezone(UTC)).total_seconds())


def _envelope(now: datetime, data_at: datetime | None) -> dict[str, object]:
    """The staleness header every response carries."""
    return {
        "generated_at": _iso(now),
        "data_as_of": _opt_iso(data_at),
        "data_age_seconds": _age_seconds(now, data_at),
    }


def _newest(moments: Sequence[datetime | None]) -> datetime | None:
    known = [m for m in moments if m is not None]
    return max(known) if known else None


def _redact(value: object, depth: int = 0) -> object:
    """Strip anything credential-shaped out of free-form recorded context."""
    if depth >= _MAX_REDACT_DEPTH:
        return REDACTED
    if isinstance(value, str):
        return REDACTED if _SECRETISH_VALUE.match(value) else value
    if isinstance(value, Mapping):
        return {
            str(k): (REDACTED if _SECRETISH_KEY.search(str(k)) else _redact(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v, depth + 1) for v in value]
    return value


def _safe_context(context: Mapping[str, object]) -> dict[str, object]:
    redacted = _redact(context)
    return redacted if isinstance(redacted, dict) else {}


def _parsed(occ_symbol: str) -> OccSymbol | None:
    """The OCC breakdown of a contract, or None if it does not parse.

    A symbol we cannot read costs the row its expiry and strike; it must not
    cost the request its response.
    """
    try:
        return parse_occ(occ_symbol)
    except OccParseError:
        return None


def _decision_row(record: DecisionRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "at": _iso(record.at),
        "cycle_id": record.cycle_id,
        "stage": str(record.stage),
        "symbol": record.symbol,
        "accepted": record.accepted,
        "reasons": list(record.reasons),
        "detail": list(record.detail),
        "context": _safe_context(record.context),
    }


def _leg_row(leg: IntentLeg, spreads: float) -> dict[str, object]:
    parsed = _parsed(leg.occ_symbol)
    return {
        "occ_symbol": leg.occ_symbol,
        "side": leg.side,
        "ratio_qty": leg.ratio_qty,
        "contracts": leg.ratio_qty * spreads,
        "position_intent": leg.position_intent,
        "expiry": None if parsed is None else parsed.expiry.isoformat(),
        "strike": None if parsed is None else parsed.strike,
        "right": None if parsed is None else ("call" if parsed.is_call else "put"),
    }


def _fill_row(journal: Journal, fill: SpreadFill) -> dict[str, object]:
    """One parent execution, with its legs beneath it in their own units."""
    return {
        "fill_id": fill.fill_id,
        "occurred_at": _iso(fill.occurred_at),
        "trading_day": fill.trading_day.isoformat(),
        "spreads": fill.spreads,
        "net_price_per_spread_signed": fill.net_price_per_spread,
        "credit_received_usd": fill.credit_received,
        "source": str(fill.source),
        "attribution": str(fill.attribution),
        "confirmed": fill.is_confirmed,
        "confirmed_at": _opt_iso(fill.confirmed_at),
        "acknowledged": fill.is_acknowledged,
        "detail": fill.detail,
        "legs": [
            {
                "fill_id": leg.fill_id,
                "occ_symbol": leg.occ_symbol,
                "contracts": leg.contracts,
                "premium_per_contract_usd": leg.premium_per_contract,
                "side": leg.side,
                "source": str(leg.source),
                "confirmed": leg.is_confirmed,
                "occurred_at": _iso(leg.occurred_at),
            }
            for leg in journal.leg_fills_for(fill.fill_id)
        ],
    }


def _order_row(journal: Journal, order: OrderRecord) -> dict[str, object]:
    """One order's life. The payload we sent the broker is deliberately absent.

    It is an opaque blob we do not model and have no reason to publish; the
    figures a reader needs -- size, signed net price, status, timings -- are
    columns on the row itself.
    """
    fills = journal.spread_fills_for(order.client_order_id)
    net = order.net_price_per_spread
    return {
        "client_order_id": order.client_order_id,
        "cycle_id": order.cycle_id,
        "symbol": order.symbol,
        "status": str(order.status),
        "is_terminal": order.status.is_terminal,
        "needs_reconciliation": order.needs_reconciliation,
        "spreads_ordered": order.spreads_ordered,
        "spreads_filled": order.spreads_filled,
        "spreads_working": order.spreads_working,
        "partially_filled": order.is_partially_filled,
        "net_price_per_spread_signed": net,
        "credit_per_spread_usd": None if net is None else abs(net) * CONTRACT_MULTIPLIER,
        "credit_received_usd": (
            None if net is None else -net * CONTRACT_MULTIPLIER * order.spreads_filled
        ),
        "intent_at": _iso(order.intent_at),
        "submitted_at": _opt_iso(order.submitted_at),
        "status_at": _opt_iso(order.status_at),
        "reconciled_at": _opt_iso(order.reconciled_at),
        "broker_order_id": order.broker_order_id,
        "detail": order.detail,
        "legs": [
            _leg_row(leg, order.spreads_ordered) for leg in journal.legs_for(order.client_order_id)
        ],
        "fills": [_fill_row(journal, fill) for fill in fills],
    }


def _position_row(journal: Journal, position: PositionRecord, today: date) -> dict[str, object]:
    """One open spread, joined back to the order that opened it.

    The journal stores the lean risk-engine view of a position; the legs, the
    expiry and the credit live on the order that created it. Where the position
    carries no `client_order_id` -- an orphan, or a book recovered without one
    -- the join simply yields nothing, and the row says so rather than
    inventing an expiry.
    """
    order = None if position.client_order_id is None else journal.order(position.client_order_id)
    legs = () if position.client_order_id is None else journal.legs_for(position.client_order_id)
    leg_rows = [_leg_row(leg, position.spreads) for leg in legs]
    expiries = [parsed.expiry for leg in legs if (parsed := _parsed(leg.occ_symbol)) is not None]
    expiry = min(expiries) if expiries else None
    net = None if order is None else order.net_price_per_spread
    return {
        "underlying": position.symbol,
        "spreads": position.spreads,
        "max_loss_usd": position.max_loss,
        "unrealised_pnl_usd": position.unrealised_pnl,
        "net_delta": position.net_delta,
        "net_price_per_spread_signed": net,
        "credit_per_spread_usd": None if net is None else abs(net) * CONTRACT_MULTIPLIER,
        "credit_received_usd": (
            None if net is None else -net * CONTRACT_MULTIPLIER * position.spreads
        ),
        "expiry": None if expiry is None else expiry.isoformat(),
        "days_to_expiry": None if expiry is None else (expiry - today).days,
        "client_order_id": position.client_order_id,
        "order_status": None if order is None else str(order.status),
        "legs": leg_rows,
        "mapped_to_order": order is not None,
        "detail": position.detail,
    }


def _pnl_point(snapshot: PnlSnapshot) -> dict[str, object]:
    return {
        "trading_day": snapshot.trading_day.isoformat(),
        "at": _iso(snapshot.at),
        "realised_pnl_usd": snapshot.realised_pnl,
        "unrealised_pnl_usd": snapshot.unrealised_pnl,
        "equity_usd": snapshot.equity,
        "detail": snapshot.detail,
    }


def _measured(value: object) -> float | None:
    """A number that was actually recorded, or None if the slot holds anything else.

    Recorded context is free-form JSON, so a figure read out of it is validated
    before it is published as a measurement. Three ways it can fail, and all
    three return None rather than a number:

    `bool` is refused first and deliberately. It is a subclass of `int` in
    Python, so a flag written where a ratio belongs would pass an `isinstance`
    check and render as 1.0 -- a plausible premium ratio assembled out of a
    True. That is the exact shape of bug this endpoint exists to not have.

    A non-numeric value -- a string, a null, a nested object -- is not a
    measurement either. And `json.dumps` will happily write a NaN or an
    infinity that `json.loads` reads back, so a value that is numeric but not
    finite is refused too: it is a failed calculation, not a reading.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _recorded_flag(value: object) -> bool | None:
    """A boolean that was actually recorded, or None. Absent is not False."""
    return value if isinstance(value, bool) else None


def _book_line(position: PositionRecord) -> dict[str, object]:
    """One open position at the detail a glance needs. `/api/positions` has the rest."""
    return {
        "symbol": position.symbol,
        "spreads": position.spreads,
        "max_loss_usd": position.max_loss,
        "unrealised_pnl_usd": position.unrealised_pnl,
        "net_delta": position.net_delta,
    }


@dataclass(frozen=True, slots=True)
class _CycleActivity:
    """What a bounded scan of the journal can say about cycles.

    `complete` is False when the scan hit its limit without reaching a record
    from before today, which makes `cycles_today` a floor rather than a count.
    """

    last_at: datetime | None = None
    last_cycle_id: str | None = None
    cycles_today: int = 0
    complete: bool = True


def _scan_reached_yesterday(moments: Sequence[datetime], limit: int, today: date) -> bool:
    """Whether a newest-first scan ran past the start of today's session."""
    if len(moments) < limit:
        return True
    return trading_day_of(moments[-1]) < today


def _cycle_activity(journal: Journal, *, today: date, scan: int) -> _CycleActivity:
    """When the agent last ran, and how many cycles today left a trace.

    Both sources are read because neither is complete on its own: a cycle names
    itself on every decision it records, and it files a regime verdict carrying
    the same id whether or not that verdict blocked anything -- so a pass that
    refused nothing still shows up, and one that never reached the regime still
    recorded why. Nothing registers a cycle as such, which is why the count
    ships as a floor with the scan's completeness beside it rather than as a
    census.
    """
    decisions = journal.recent_decisions(scan)
    verdicts = journal.regime_history(scan)
    marks: list[tuple[datetime, str | None]] = [
        (record.at, record.cycle_id) for record in decisions
    ]
    for verdict in verdicts:
        recorded = verdict.context.get("cycle_id")
        marks.append((verdict.at, recorded if isinstance(recorded, str) else None))
    if not marks:
        return _CycleActivity()
    marks.sort(key=lambda mark: mark[0], reverse=True)
    named = [cycle_id for _, cycle_id in marks if cycle_id is not None]
    today_ids = {
        cycle_id for at, cycle_id in marks if cycle_id is not None and trading_day_of(at) == today
    }
    return _CycleActivity(
        last_at=marks[0][0],
        last_cycle_id=named[0] if named else None,
        cycles_today=len(today_ids),
        complete=(
            _scan_reached_yesterday([record.at for record in decisions], scan, today)
            and _scan_reached_yesterday([verdict.at for verdict in verdicts], scan, today)
        ),
    )


def _refusals_today(journal: Journal, *, today: date, scan: int) -> tuple[int, bool]:
    """How many refusals today, and whether the scan saw all of them."""
    rejected = journal.rejections(scan)
    counted = sum(1 for record in rejected if trading_day_of(record.at) == today)
    return counted, _scan_reached_yesterday([record.at for record in rejected], scan, today)


def _latest_equity(journal: Journal, *, today: date, days: int) -> PnlSnapshot | None:
    """The newest official snapshot carrying an equity reading, or None.

    Walked back a day at a time because the journal indexes P&L by trading day.
    Only the official series is read: equity is the broker's own figure, the
    shadow series prices exits against the quoted spread, and reaching for
    whichever happens to be newer is how the two get mixed (docs/GOTCHAS.md #3).
    """
    for offset in range(days):
        snapshot = journal.latest_pnl(
            trading_day=today - timedelta(days=offset), source=PnlSource.OFFICIAL
        )
        if snapshot is not None and snapshot.equity is not None:
            return snapshot
    return None


def _fill_direction(journal: Journal, fill: SpreadFill) -> str:
    """Whether a parent fill opened or closed, from the intent on its legs.

    `position_intent` is the only durable statement of direction. The fill's own
    signed net price is not one: a debit is as consistent with buying back a
    credit spread as with opening a debit one. A fill against no order of ours
    -- a broker liquidation inside the hour before expiry, docs/GOTCHAS.md #10
    -- is counted as neither rather than guessed into one.
    """
    if fill.client_order_id is None:
        return "unclassified"
    legs = journal.legs_for(fill.client_order_id)
    if any(leg.position_intent.endswith("_to_close") for leg in legs):
        return "close"
    if any(leg.position_intent.endswith("_to_open") for leg in legs):
        return "open"
    return "unclassified"


def _watching_rows(
    journal: Journal, *, cycle_id: str | None, limit: int
) -> list[dict[str, object]]:
    """What the last recorded cycle considered, one row per instrument.

    A symbol is `rejected` if anything in that cycle turned it away, and the
    reason codes come with it; otherwise it cleared every stage it reached.

    The volatility figures are read from the recorded decision context, where
    the cycle now writes them as numbers. `vrp_ratio` stays null beside
    `vrp_ratio_known: false` for any decision that carries none -- every one
    taken before the cycle started journalling them, which is most of the
    history on disk. A ratio of 0.0 there would say implied volatility was
    measured at zero.

    The ratio and the two figures it is made of are taken from ONE decision,
    the newest in the cycle that measured this instrument. Filling each field
    from whichever decision happens to carry it would produce a row whose
    implied over realised does not equal its own quotient.

    Context is free-form JSON that a writer chose the shape of, so it is
    validated rather than trusted: only these four keys are read, only numbers
    are accepted for the three numeric ones, and nothing else in the context
    reaches the response.
    """
    if cycle_id is None:
        return []
    grouped: dict[str, list[DecisionRecord]] = {}
    for record in journal.recent_decisions(limit, cycle_id=cycle_id):
        if record.symbol is not None:
            grouped.setdefault(record.symbol, []).append(record)

    rows: list[dict[str, object]] = []
    for symbol, group in sorted(grouped.items()):
        refused = [record for record in group if not record.accepted]
        reasons: list[str] = []
        for record in refused:
            for reason in record.reasons:
                if reason not in reasons:
                    reasons.append(reason)
        # Rows arrive newest first, so the head of the group is this cycle's
        # last word on the instrument, and the first group member carrying a
        # ratio is its freshest measurement.
        newest = group[0]
        measured = next(
            (record for record in group if _measured(record.context.get("vrp_ratio")) is not None),
            None,
        )
        context: Mapping[str, object] = {} if measured is None else measured.context
        ratio = _measured(context.get("vrp_ratio"))
        rows.append(
            {
                "symbol": symbol,
                "verdict": "rejected" if refused else "accepted",
                "reasons": reasons,
                "stage": str(newest.stage),
                "at": _iso(newest.at),
                "vrp_ratio": ratio,
                "vrp_ratio_known": ratio is not None,
                "implied_vol": _measured(context.get("implied_vol")),
                "realised_vol": _measured(context.get("realised_vol")),
                "realised_is_expanding": _recorded_flag(context.get("realised_is_expanding")),
                "measured_at": None if measured is None else _iso(measured.at),
            }
        )
    return rows


# --------------------------------------------------------------------------
# Payload builders. Each is a pure read: journal in, JSON-ready dict out.
# --------------------------------------------------------------------------


def overview_payload(
    journal: Journal,
    *,
    now: datetime,
    max_view_age: timedelta,
    limits: RiskLimits,
    schedule: Schedule,
    scan: int = DEFAULT_OVERVIEW_SCAN,
    equity_days: int = OVERVIEW_EQUITY_DAYS,
) -> dict[str, object]:
    """The front page: what the agent is doing, and how much money is there.

    One request rather than five, because the figures on it have to agree with
    each other. Assembling this in the browser means three or four reads of a
    journal a live agent is writing to, and the equity from one moment rendered
    beside the open risk from another is a headline nobody can reproduce.

    Every money figure here is the OFFICIAL series and `notes.pnl_series` says
    so. Alpaca's paper P&L is reported, not trusted (docs/GOTCHAS.md #3), and
    the shadow series is neither added to it nor averaged with it -- the two are
    read side by side at `/api/pnl`.

    Nothing unknown is rendered as zero. Every figure the journal cannot vouch
    for is `null` beside a `_known` flag, because a 0.0 reads as "flat" and flat
    is a claim: it says the day was measured and made nothing. Three separate
    cases take that path and each is a different fact -- a book that has never
    been observed has unknown open risk rather than none, a day with no P&L
    snapshot has unknown unrealised rather than none, and a snapshot that
    predates a fill has an unknown realised figure rather than a stale one.

    `book.count` is the exception, and deliberately: it counts the rows in the
    list beside it, so it is 0 for a book that has never been observed exactly
    as `/api/positions` reports it -- with `observed: false` next to it saying
    which of the two zeroes this is.
    """
    state = journal.recover(now=now, max_view_age=max_view_age)
    today = state.trading_day
    book = state.book
    activity = _cycle_activity(journal, today=today, scan=scan)

    equity_snapshot = _latest_equity(journal, today=today, days=equity_days)
    equity = None if equity_snapshot is None else equity_snapshot.equity
    marked = journal.latest_pnl(trading_day=today, source=PnlSource.OFFICIAL)
    unrealised = None if marked is None else marked.unrealised_pnl
    realised = state.realised_pnl_today
    session_open = state.session_open_equity

    # Both halves or neither. A day P&L built from a known realised figure and
    # an unmeasured mark is not a partial answer, it is a wrong one.
    day_pnl = None if realised is None or unrealised is None else realised + unrealised
    day_pnl_pct = (
        None
        if day_pnl is None or session_open is None or session_open == 0
        else day_pnl / session_open * 100
    )

    # A book we have never seen has unknown open risk, not none. The cycle
    # takes the same position: an unreadable book bars every entry precisely
    # because open risk and the position cap become unanswerable.
    open_risk = sum((p.max_loss for p in book.positions), 0.0) if book.observed else None
    open_risk_pct = (
        None if open_risk is None or equity is None or equity <= 0 else open_risk / equity * 100
    )

    fills = journal.spread_fills_on(today)
    directions = [_fill_direction(journal, fill) for fill in fills]
    refusals, refusals_complete = _refusals_today(journal, today=today, scan=scan)

    data_at = _newest(
        [
            book.taken_at,
            state.last_reconciled_at,
            activity.last_at,
            None if equity_snapshot is None else equity_snapshot.at,
            None if marked is None else marked.at,
            max((fill.occurred_at for fill in fills), default=None),
        ]
    )

    return {
        **_envelope(now, data_at),
        "trading_day": today.isoformat(),
        "running": {
            "last_cycle_at": _opt_iso(activity.last_at),
            "last_cycle_id": activity.last_cycle_id,
            "seconds_since_last_cycle": _age_seconds(now, activity.last_at),
            "cycles_today": activity.cycles_today,
            "cycles_today_complete": activity.complete,
            "market_open": schedule.in_session(now),
            "may_trade": state.may_trade,
            "view_stale": RecoveryGap.VIEW_STALE in state.gaps,
            "kill_switch_engaged": state.kill_switch.engaged,
        },
        "money": {
            "pnl_series": str(PnlSource.OFFICIAL),
            "equity_usd": equity,
            "equity_known": equity is not None,
            "equity_as_of": _opt_iso(None if equity_snapshot is None else equity_snapshot.at),
            "session_open_equity_usd": session_open,
            "session_open_recorded": session_open is not None,
            "realised_today_usd": realised,
            "realised_today_known": realised is not None,
            "unrealised_usd": unrealised,
            "unrealised_known": unrealised is not None,
            "day_pnl_usd": day_pnl,
            "day_pnl_pct": day_pnl_pct,
            "day_pnl_known": day_pnl is not None,
            "open_risk_usd": open_risk,
            "open_risk_known": open_risk is not None,
            "open_risk_pct_of_equity": open_risk_pct,
            "risk_cap_pct": limits.max_total_open_risk_pct,
        },
        "book": {
            "observed": book.observed,
            "taken_at": _opt_iso(book.taken_at),
            "count": len(book.positions),
            "spreads_total": sum((p.spreads for p in book.positions), 0.0),
            "underlyings": [
                _book_line(position)
                for position in sorted(book.positions, key=lambda p: (-p.max_loss, p.symbol))
            ],
        },
        "today": {
            "fills": len(fills),
            "opens": directions.count("open"),
            "closes": directions.count("close"),
            "unclassified": directions.count("unclassified"),
            "refusals": refusals,
            "refusals_complete": refusals_complete,
        },
        "watching": _watching_rows(
            journal, cycle_id=activity.last_cycle_id, limit=OVERVIEW_WATCHING_LIMIT
        ),
        "notes": dict(OVERVIEW_NOTES),
        "units": dict(OVERVIEW_UNITS),
    }


def state_payload(journal: Journal, *, now: datetime, max_view_age: timedelta) -> dict[str, object]:
    """What the agent knows about itself right now, gaps included."""
    state = journal.recover(now=now, max_view_age=max_view_age)
    switch = state.kill_switch
    book = state.book
    candidate = state.session_open_candidate
    return {
        **_envelope(now, _newest([book.taken_at, state.last_reconciled_at])),
        "trading_day": state.trading_day.isoformat(),
        "may_trade": state.may_trade,
        "kill_switch": {
            "engaged": switch.engaged,
            "may_trade": switch.may_trade,
            "reason": switch.reason,
            "actor": None if switch.actor is None else str(switch.actor),
            "changed_at": _opt_iso(switch.changed_at),
            "age_seconds": _age_seconds(now, switch.changed_at),
        },
        "recovery": {
            "gaps": [str(gap) for gap in state.gaps],
            "detail": list(state.detail),
            "is_clean": state.is_clean,
        },
        "reconciliation": {
            "scope": str(ReconciliationScope.FULL),
            "last_at": _opt_iso(state.last_reconciled_at),
            "age_seconds": None if state.view_age is None else state.view_age.total_seconds(),
            "max_view_age_seconds": max_view_age.total_seconds(),
            "never_reconciled": state.last_reconciled_at is None,
        },
        # The one question the UI asks loudest: is what you are looking at
        # still true? Never reconciled counts as stale, not as fresh.
        "view_stale": RecoveryGap.VIEW_STALE in state.gaps,
        "session_open_equity_usd": state.session_open_equity,
        "session_open_equity_recorded": state.session_open_equity is not None,
        "session_open_candidate_usd": None if candidate is None else candidate.equity,
        "session_open_disputes": [
            {
                "offered_usd": rejection.offered,
                "kept_usd": rejection.kept,
                "drift_pct": rejection.drift_pct,
                "offered_at": _iso(rejection.offered_at),
            }
            for rejection in state.rejected_session_opens
        ],
        # None means UNKNOWN. It is not rendered as zero anywhere, because a
        # zero would read as "flat" and disarm the daily loss stop in the UI
        # exactly as it would in the engine.
        "realised_pnl_today_usd": state.realised_pnl_today,
        "realised_pnl_today_known": state.realised_pnl_today is not None,
        "book": {
            "observed": book.observed,
            "taken_at": _opt_iso(book.taken_at),
            "age_seconds": _age_seconds(now, book.taken_at),
            "open_positions": len(book.positions),
        },
        "attention": {
            "unreconciled_orders": len(state.unreconciled_orders),
            "unconfirmed_fills": len(state.unconfirmed_fills),
            "unattributed_fills": len(state.unattributed_fills),
            "unexplained_exits": len(state.unexplained_exits),
            "pending_vanishes": len(state.pending_vanishes),
            "undiffed_snapshots": state.undiffed_snapshots,
        },
        "units": dict(STATE_UNITS),
    }


def positions_payload(journal: Journal, *, now: datetime) -> dict[str, object]:
    """The open book as last observed, joined to the orders that opened it."""
    book = journal.latest_positions()
    today = trading_day_of(now)
    return {
        **_envelope(now, book.taken_at),
        "observed": book.observed,
        "taken_at": _opt_iso(book.taken_at),
        "as_of_trading_day": today.isoformat(),
        "count": len(book.positions),
        "positions": [_position_row(journal, p, today) for p in book.positions],
        "totals": {
            "max_loss_usd": sum(p.max_loss for p in book.positions),
            "unrealised_pnl_usd": sum(p.unrealised_pnl for p in book.positions),
            "net_delta": sum(p.net_delta for p in book.positions),
            "spreads": sum(p.spreads for p in book.positions),
        },
        "units": dict(MONEY_UNITS),
    }


def decisions_payload(
    journal: Journal,
    *,
    now: datetime,
    limit: int,
    cycle_id: str | None = None,
    symbol: str | None = None,
) -> dict[str, object]:
    """The decision log: what was considered, and what came of it."""
    records = journal.recent_decisions(limit, cycle_id=cycle_id, symbol=symbol)
    accepted = sum(1 for record in records if record.accepted)
    return {
        **_envelope(now, _newest([record.at for record in records])),
        "window": {
            "limit": limit,
            "returned": len(records),
            "truncated": len(records) == limit,
            "cycle_id": cycle_id,
            "symbol": symbol,
        },
        "accepted": accepted,
        "rejected": len(records) - accepted,
        "decisions": [_decision_row(record) for record in records],
    }


def rejections_payload(journal: Journal, *, now: datetime, limit: int) -> dict[str, object]:
    """Every refusal, grouped by reason code. The headline.

    Counts are per reason code, and a single rejection may carry several, so
    they sum to at least the number of rejections and often to more. The
    payload states that rather than presenting a total that does not add up.
    """
    rejected = journal.rejections(limit)
    examined = journal.recent_decisions(limit)
    accepted = sum(1 for record in examined if record.accepted)

    counts: dict[str, int] = {}
    symbols: dict[str, list[str]] = {}
    stages: dict[str, list[str]] = {}
    last_at: dict[str, datetime] = {}
    examples: dict[str, tuple[str, ...]] = {}
    by_stage: dict[str, int] = {}
    for record in rejected:
        stage = str(record.stage)
        by_stage[stage] = by_stage.get(stage, 0) + 1
        for reason in record.reasons:
            counts[reason] = counts.get(reason, 0) + 1
            seen_symbols = symbols.setdefault(reason, [])
            if record.symbol is not None and record.symbol not in seen_symbols:
                seen_symbols.append(record.symbol)
            seen_stages = stages.setdefault(reason, [])
            if stage not in seen_stages:
                seen_stages.append(stage)
            # Rows arrive newest first, so the first sighting of a reason is
            # its most recent one and the detail beside it is the freshest
            # example of that refusal.
            if reason not in last_at:
                last_at[reason] = record.at
                examples[reason] = record.detail

    groups = [
        {
            "reason": reason,
            "count": count,
            "symbols": sorted(symbols.get(reason, [])),
            "stages": sorted(stages.get(reason, [])),
            "last_at": _iso(last_at[reason]),
            "example_detail": list(examples.get(reason, ())),
        }
        for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]

    total = len(rejected)
    return {
        **_envelope(now, _newest([record.at for record in rejected])),
        "counting": (
            "counts are per reason code and one rejection may carry several, so "
            "the grouped counts sum to at least total_rejections"
        ),
        "total_rejections": total,
        "rejections_examined": total,
        "decisions_examined": len(examined),
        "accepted_in_decisions_examined": accepted,
        "refusal_rate_pct": (
            None if not examined else (len(examined) - accepted) / len(examined) * 100
        ),
        "window": {"limit": limit, "truncated": total == limit},
        "by_reason": groups,
        "by_stage": [
            {"stage": stage, "count": count}
            for stage, count in sorted(by_stage.items(), key=lambda item: (-item[1], item[0]))
        ],
        "recent": [_decision_row(record) for record in rejected],
    }


def pnl_payload(journal: Journal, *, now: datetime, days: int) -> dict[str, object]:
    """Both P&L series, side by side and never summed.

    One point per trading day per series: the newest snapshot that day
    recorded. The journal exposes P&L a day at a time, so the series is
    assembled by walking back over the requested window; days with no snapshot
    are absent rather than zero-filled, because a day we did not measure is not
    a day that made nothing.
    """
    today = trading_day_of(now)
    span = [today - timedelta(days=offset) for offset in reversed(range(days))]

    series: dict[str, object] = {}
    latest_moments: list[datetime | None] = []
    for source, trust in (
        (
            PnlSource.OFFICIAL,
            "Alpaca's paper figure. Reported, not trusted: the multi-leg fill model "
            "is undocumented and simulates against modified indicative quotes "
            "(docs/GOTCHAS.md #3).",
        ),
        (
            PnlSource.SHADOW,
            "Our own conservative pricing across the quoted spread. The honest one, "
            "and the one to read when the two disagree.",
        ),
    ):
        points = [
            snapshot
            for day in span
            if (snapshot := journal.latest_pnl(trading_day=day, source=source)) is not None
        ]
        latest_moments.append(points[-1].at if points else None)
        series[str(source)] = {
            "source": str(source),
            "trusted": source is PnlSource.SHADOW,
            "note": trust,
            "points": [_pnl_point(snapshot) for snapshot in points],
            "latest": _pnl_point(points[-1]) if points else None,
        }

    return {
        **_envelope(now, _newest(latest_moments)),
        "never_summed": (
            "the two series measure the same account by different rules and are "
            "reported side by side; adding or averaging them would hide the "
            "discrepancy the shadow series exists to expose"
        ),
        "window": {
            "days": days,
            "from_trading_day": span[0].isoformat(),
            "to_trading_day": span[-1].isoformat(),
        },
        "session_open_equity_usd": journal.session_open_equity(today),
        "series": series,
        "units": dict(MONEY_UNITS),
    }


def orders_payload(journal: Journal, *, now: datetime, limit: int) -> dict[str, object]:
    """Order history, newest intent first, with each order's fills beneath it."""
    orders = journal.order_history(limit)
    unreconciled = journal.unreconciled_orders()
    by_status: dict[str, int] = {}
    for order in orders:
        status = str(order.status)
        by_status[status] = by_status.get(status, 0) + 1
    return {
        **_envelope(now, _newest([order.status_at or order.intent_at for order in orders])),
        "window": {"limit": limit, "returned": len(orders), "truncated": len(orders) == limit},
        "unreconciled": len(unreconciled),
        "by_status": [
            {"status": status, "count": count}
            for status, count in sorted(by_status.items(), key=lambda item: (-item[1], item[0]))
        ],
        "orders": [_order_row(journal, order) for order in orders],
        "units": dict(MONEY_UNITS),
    }


def health_payload(journal: Journal, *, now: datetime) -> dict[str, object]:
    """Liveness: the process is up and the journal answers a trivial read."""
    journal.order_history(1)
    return {
        **_envelope(now, None),
        "status": "ok",
        "read_only": True,
        "journal_readable": True,
        "journal_mode": journal.journal_mode,
        "schema_version": SCHEMA_VERSION,
    }


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------


def create_app(
    config: DashboardConfig | None = None,
    *,
    gateway: JournalGateway | None = None,
) -> FastAPI:
    """Build the read-only dashboard app.

    Every route registered here is a GET. Nothing in this function, and nothing
    it calls, writes to the journal or reaches the broker.

    A `gateway` passed in belongs to the caller and is left open on shutdown;
    one built here is closed with the app. Tests pass their own so they can
    seed the journal it holds.
    """
    cfg = config if config is not None else DashboardConfig()
    owned = gateway is None
    reader = gateway if gateway is not None else JournalGateway(cfg.journal_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if owned:
            reader.close()

    app = FastAPI(
        title="Underwriter dashboard",
        version="1",
        summary=(
            "Read-only view of an autonomous options underwriter: "
            "what it did, and everything it refused."
        ),
        lifespan=lifespan,
    )

    def _journal_unreadable(_request: Request, exc: Exception) -> JSONResponse:
        """A journal we cannot read is reported as such, not as empty data.

        The message is withheld deliberately: it names the database file, and
        this app is the part of the system a stranger can reach.
        """
        return JSONResponse(
            status_code=503,
            content={
                "generated_at": _iso(cfg.clock()),
                "status": "unavailable",
                "error": type(exc).__name__,
                "detail": "the journal could not be read; see the agent's logs",
            },
        )

    app.add_exception_handler(JournalError, _journal_unreadable)

    @app.get("/", response_class=HTMLResponse, response_model=None)
    def index() -> Response:
        """The dashboard page itself, from disk.

        The page is built separately. Until it exists this serves a plain index
        of the API rather than a 500, because a demo that cannot start is worse
        than one that starts unstyled.
        """
        page = cfg.static_dir / "index.html"
        if page.is_file():
            return FileResponse(page, media_type="text/html")
        # Fall back to the ledger rather than an API index: a working page
        # showing the wrong thing first beats no page at all.
        ledger = cfg.static_dir / "ledger.html"
        if ledger.is_file():
            return FileResponse(ledger, media_type="text/html")
        return HTMLResponse(_MISSING_PAGE)

    @app.get("/ledger", response_model=None)
    def ledger_page() -> Response:
        """The decision ledger: every refusal, with its reason.

        Split from the main dashboard deliberately. The refusals are the most
        interesting thing about this agent and the right story for a reviewer,
        but they are not what an operator needs at a glance -- money and
        activity are. Both pages read the same journal.
        """
        page = cfg.static_dir / "ledger.html"
        if page.is_file():
            return FileResponse(page, media_type="text/html")
        return HTMLResponse(_MISSING_PAGE)

    @app.get("/favicon.ico", response_model=None)
    def favicon() -> Response:
        """Browsers request this from the root whether or not the page links it."""
        target = cfg.static_dir / "favicon.ico"
        if not target.is_file():
            return Response(status_code=404)
        return FileResponse(
            target,
            media_type="image/x-icon",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/static/{name}", response_model=None)
    def static_asset(name: str) -> Response:
        """Serve a named brand asset.

        Deliberately a flat lookup against an allow-list rather than a mounted
        directory. The static folder sits inside the installed package, and a
        path-traversal bug here would read arbitrary files out of a container
        that also holds broker credentials in its environment. An allow-list
        cannot traverse, and the set of assets is small and known.
        """
        allowed = {
            "favicon.ico": "image/x-icon",
            "logo.png": "image/png",
            "logo-mark.png": "image/png",
            "logo-wordmark.png": "image/png",
            **{f"icon-{px}.png": "image/png" for px in (16, 32, 48, 180, 192, 512)},
        }
        media = allowed.get(name)
        if media is None:
            return Response(status_code=404)
        target = cfg.static_dir / name
        if not target.is_file():
            return Response(status_code=404)
        return FileResponse(
            target,
            media_type=media,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/api/health", response_model=None)
    def health() -> dict[str, object]:
        return reader.run(lambda journal: health_payload(journal, now=cfg.clock()))

    @app.get("/api/overview", response_model=None)
    def overview() -> dict[str, object]:
        return reader.run(
            lambda journal: overview_payload(
                journal,
                now=cfg.clock(),
                max_view_age=cfg.max_view_age,
                limits=cfg.limits,
                schedule=cfg.schedule,
            )
        )

    @app.get("/api/state", response_model=None)
    def state() -> dict[str, object]:
        return reader.run(
            lambda journal: state_payload(journal, now=cfg.clock(), max_view_age=cfg.max_view_age)
        )

    @app.get("/api/positions", response_model=None)
    def positions() -> dict[str, object]:
        return reader.run(lambda journal: positions_payload(journal, now=cfg.clock()))

    @app.get("/api/decisions", response_model=None)
    def decisions(
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_DECISION_LIMIT,
        cycle_id: Annotated[str | None, Query(max_length=128)] = None,
        symbol: Annotated[str | None, Query(max_length=32)] = None,
    ) -> dict[str, object]:
        return reader.run(
            lambda journal: decisions_payload(
                journal, now=cfg.clock(), limit=limit, cycle_id=cycle_id, symbol=symbol
            )
        )

    @app.get("/api/rejections", response_model=None)
    def rejections(
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_REJECTION_LIMIT,
    ) -> dict[str, object]:
        return reader.run(lambda journal: rejections_payload(journal, now=cfg.clock(), limit=limit))

    @app.get("/api/pnl", response_model=None)
    def pnl(
        days: Annotated[int | None, Query(ge=1, le=MAX_PNL_DAYS)] = None,
    ) -> dict[str, object]:
        window = days if days is not None else cfg.pnl_days
        return reader.run(lambda journal: pnl_payload(journal, now=cfg.clock(), days=window))

    @app.get("/api/orders", response_model=None)
    def orders(
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_ORDER_LIMIT,
    ) -> dict[str, object]:
        return reader.run(lambda journal: orders_payload(journal, now=cfg.clock(), limit=limit))

    return app
