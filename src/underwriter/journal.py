"""The durable journal: the agent's memory across crashes and restarts.

The agent must never lose track of an open position. Everything else in this
module follows from that one sentence.

**Write-ahead of submission.** An order is journalled as `INTENT` *before* it
is sent to the broker, in its own committed transaction, under a
`client_order_id` we chose. This ordering is the entire point of the module.
If the process dies between the journal write and the broker call, restart
finds an intent with no submission and reconciles it against the broker by
`client_order_id` -- the one identifier that survives the crash on both sides.
The opposite ordering has a window in which the broker holds a live order the
agent has no record of, and no amount of later reconciliation can close it,
because reconciliation needs an identifier to reconcile *by*. Alpaca does not
de-duplicate `client_order_id` on submission (docs/GOTCHAS.md #9), so after any
timeout the only safe move is to look the order up by that id -- which requires
that we wrote it down first.

    journal.record_intent(...)   # committed, durable
    broker.submit(...)           # may time out, may crash, may double-send
    journal.mark_submitted(...)  # or, at restart, journal.mark_status(...)

**The stream is never the system of record.** `trade_updates` has no replay,
no resume token, no sequence number and no cursor, so every disconnect is a
definite gap rather than a possible one. A streamed fill is therefore recorded
as *unconfirmed* and stays that way until a REST read says the same thing.
`unconfirmed_fills()` and the `UNCONFIRMED_FILLS` recovery gap exist so that
"we heard about this on a socket and never checked" cannot pass for knowledge.

**On paper, the position list is the only same-day truth.** Non-trade
activities -- assignment, exercise, expiry, corporate actions -- are not
delivered over the websocket by design, and on paper accounts they do not
reach `/v2/account/activities` until the following day. The only same-day
signal that a position was assigned or expired is that it quietly disappears
from `/v2/positions`. So the book is snapshotted every cycle and diffed:
`vanished_positions()` finds what left, and anything that left without a
closing fill we initiated is recorded as a `PositionEvent` with cause
`UNKNOWN` and evidence `INFERRED_FROM_SNAPSHOT`. That diff carries a durable
cursor, because comparing only the two newest snapshots would make a
disappearance visible for exactly one polling cycle and then lose it for
good. When the activity record arrives the next day,
`confirm_position_event()` attaches it to the existing inference rather than
creating a second version of the same event.

**Units and signs are never conflated.** A multi-leg order fills all-or-nothing
on the *ratio*, not the *quantity*: with five spreads ordered, two can fill
while three keep working (docs/GOTCHAS.md #8). Parent and leg then speak
different languages:

| | quantity | price |
|---|---|---|
| parent | **spreads** (strategy units) | **signed** net per spread; negative is a credit |
| leg | **contracts** (`ratio_qty` x spreads) | that leg's own premium, always positive |

No column here is named `qty` or `price`. The parent's numbers live on
`orders` and `spread_fills` as `spreads` and `net_price_per_spread`; a leg's
live on `leg_fills` as `contracts` and `premium_per_contract`, and a
non-positive premium is refused because it means the two were mixed up.

**Fail closed.** Every read path refuses to turn unreadable state into
plausible-looking state. An order status we do not recognise reads as
`UNKNOWN`, which is non-terminal, which forces reconciliation. A day whose
realised P&L we cannot vouch for reads as `None`, not as zero -- because zero
would silently disarm the daily loss stop. `RecoveryState` carries a tuple of
displayable `RecoveryGap` codes for exactly this reason: recovery that could
not establish something says so rather than guessing.

**It is also the audit trail.** Every cycle's considered candidates, every
rejection with its reason code, every regime verdict, and both P&L series
(official and conservative shadow, per docs/GOTCHAS.md #3) land here, because
a strategy that cannot show why it did not trade is indistinguishable from one
that was broken.

Timestamps are stored as ISO8601 UTC strings with a fixed field width, so
lexicographic ordering in SQL is chronological ordering. They are returned as
timezone-aware datetimes; a naive datetime handed to a writer is rejected
rather than assumed to be UTC.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final, Self

# Bumped whenever the schema changes shape. A database written by a NEWER
# version is refused outright: a column this build cannot see could be the one
# holding an open position, and reading around it would look like success. An
# OLDER one is refused too, for the same reason in reverse.
SCHEMA_VERSION: Final = 3

MEMORY: Final = ":memory:"

# Options are 100 shares to a contract, and a spread's P&L is quoted per share.
CONTRACT_MULTIPLIER: Final = 100

# How long a writer waits for another connection's lock before giving up. The
# agent has one writer and a read-only dashboard, so contention is brief; five
# seconds is far longer than any statement here takes and still fails rather
# than hanging a trading cycle forever.
BUSY_TIMEOUT_MS: Final = 5_000

# How stale the agent's view of the broker may be before trading on it is
# reckless. This is a default, not a law -- the caller passes its own. Five
# minutes is roughly one monitoring cycle's grace: long enough that an ordinary
# poll does not trip it, short enough that a wedged reconciler does.
DEFAULT_MAX_VIEW_AGE: Final = timedelta(minutes=5)

_SIDES: Final = frozenset({"buy", "sell"})

# Spread counts are whole numbers in practice but stored as REAL, so the
# over-fill comparison needs a hair of slack rather than an exact `>`.
_SPREAD_EPSILON: Final = 1e-9


class JournalError(RuntimeError):
    """The journal could not be opened, read, or written truthfully."""


class SchemaTooNewError(JournalError):
    """The database was written by a newer build than this one."""


class SchemaTooOldError(JournalError):
    """The database predates this schema and there is no migration for it."""


class ConflictingIntentError(JournalError):
    """A client_order_id was reused for a materially different order.

    Reusing the identifier we reconcile by would make two orders indis-
    tinguishable after a crash, which is the one failure this module exists to
    prevent.
    """


class UnknownOrderError(JournalError):
    """A status update arrived for an order that was never journalled.

    Under the write-ahead rule this cannot happen for an order *we* placed, so
    it means the rule was broken somewhere. Note that fills are deliberately
    more forgiving: Alpaca's own pre-expiry sell-out arrives as a fill on an
    order id we never created, so those are recorded and flagged rather than
    refused. See `FillAttribution`.
    """


class UnitConfusionError(JournalError):
    """Spreads and contracts, or a signed net and a leg premium, were mixed up.

    Five spreads across two legs is ten contracts, and a filled credit's net
    price is negative while every leg premium is positive. Writing one where
    the other belongs misstates open risk and P&L without ever looking wrong,
    so the writers refuse the arithmetic that can only mean confusion.
    """


class Stage(StrEnum):
    """Where in the cycle a decision was taken. Displayed verbatim."""

    SCAN = "scan"
    RANK = "rank"
    REGIME = "regime"
    VETO = "veto"
    RISK = "risk"
    EXECUTE = "execute"
    MONITOR = "monitor"
    EXIT = "exit"
    REVIEW = "review"


class OrderStatus(StrEnum):
    """An order's life, as the journal understands it.

    `INTENT` is ours, not the broker's: it means journalled and not yet known
    to have reached Alpaca. `ABANDONED` is also ours: reconciliation looked for
    this `client_order_id` and the broker had never heard of it.
    """

    INTENT = "intent"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        """True when the order's fate is settled and needs no further chasing."""
        return self in _TERMINAL_STATUSES

    @classmethod
    def from_broker(cls, raw: str) -> OrderStatus:
        """Map an Alpaca order status onto ours, resolving doubt into `UNKNOWN`.

        Alpaca publishes more statuses than this strategy has meanings for, and
        several of them (`replaced`, `done_for_day`, `stopped`, `calculated`)
        are ambiguous about whether anything filled. Every one of those maps to
        `UNKNOWN`, which is deliberately non-terminal: an ambiguous status keeps
        the order on the reconciliation list until a human or a later broker
        event settles it. Guessing "probably nothing filled" is how a book gets
        a position nobody is watching.
        """
        return _BROKER_STATUSES.get(raw.strip().lower(), cls.UNKNOWN)


class FillSource(StrEnum):
    """Where we learned about an execution, which decides whether we believe it.

    `STREAM` is a latency optimisation with a known hole in it: `trade_updates`
    reconnects without a cursor, so a disconnect drops events silently. `REST`
    is the system of record. `INFERRED` is neither -- it is our own deduction
    from a snapshot diff, and it never counts as confirmation.
    """

    STREAM = "stream"
    REST = "rest"
    INFERRED = "inferred"


class FillAttribution(StrEnum):
    """Whose order an execution belongs to.

    Not every fill is ours. If buying power is short of an ITM exercise, Alpaca
    sells the position out within the hour before expiry (docs/GOTCHAS.md #10),
    and that arrives as a fill on an order id we never created. Refusing it
    would lose a real, risk-changing event, so it is recorded and marked.
    """

    JOURNALLED = "journalled"
    BROKER_INITIATED = "broker_initiated"
    UNKNOWN_ORDER = "unknown_order"


class PositionEventCause(StrEnum):
    """Why a position left the book. Displayed verbatim."""

    CLOSED_BY_US = "closed_by_us"
    ASSIGNMENT = "assignment"
    EXERCISE = "exercise"
    EXPIRY = "expiry"
    LIQUIDATION = "liquidation"
    CORPORATE_ACTION = "corporate_action"
    UNKNOWN = "unknown"


class PositionEventEvidence(StrEnum):
    """How well we know why a position left.

    `INFERRED_FROM_SNAPSHOT` means the position was in one snapshot and gone
    from the next, and nothing more. On a paper account that is all we get on
    the day it happens.
    """

    INFERRED_FROM_SNAPSHOT = "inferred_from_snapshot"
    CONFIRMED_BY_ACTIVITY = "confirmed_by_activity"


class ReconciliationScope(StrEnum):
    """Which part of the broker's state a reconciliation pass covered."""

    ORDERS = "orders"
    POSITIONS = "positions"
    ACTIVITIES = "activities"
    FULL = "full"


class PnlSource(StrEnum):
    """Which P&L series a snapshot belongs to.

    Both are recorded because the paper multi-leg fill model is undocumented
    and simulates against modified indicative quotes (docs/GOTCHAS.md #3). The
    official figure is what Alpaca reports; the shadow figure prices exits
    across the quoted spread. They belong side by side in the submission.
    """

    OFFICIAL = "official"
    SHADOW = "shadow"


class RecoveryGap(StrEnum):
    """What restart recovery could not establish. Displayed verbatim."""

    SESSION_EQUITY_MISSING = "session_equity_missing"
    REALISED_PNL_UNKNOWN = "realised_pnl_unknown"
    UNRECONCILED_ORDERS = "unreconciled_orders"
    POSITIONS_UNOBSERVED = "positions_unobserved"
    VIEW_STALE = "view_stale"
    UNCONFIRMED_FILLS = "unconfirmed_fills"
    UNATTRIBUTED_FILLS = "unattributed_fills"
    UNEXPLAINED_POSITION_EXITS = "unexplained_position_exits"
    POSITION_DIFFS_PENDING = "position_diffs_pending"


_TERMINAL_STATUSES: Final = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.ABANDONED,
    }
)

_BROKER_STATUSES: Final[Mapping[str, OrderStatus]] = {
    "new": OrderStatus.ACCEPTED,
    "pending_new": OrderStatus.ACCEPTED,
    "accepted": OrderStatus.ACCEPTED,
    "accepted_for_bidding": OrderStatus.ACCEPTED,
    "held": OrderStatus.ACCEPTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


def spread_realised_pnl(*, open_net_price: float, close_net_price: float, spreads: float) -> float:
    """Realised P&L for a round trip, from two SIGNED net prices.

    Both arguments are the parent's `filled_avg_price` convention: negative for
    a credit received, positive for a debit paid. Opening a credit spread at
    1.20 is `-1.20`; buying it back at 0.40 is `+0.40`; the profit is 0.80 per
    spread per share, so 80 dollars on one spread.

    Written as the negated sum of the signed prices rather than as
    `credit - debit`, because the negation is the one place the sign convention
    is applied and it is applied identically to both sides. A `credit - debit`
    formula silently produces the wrong sign the first time it is handed a
    debit spread, and the error looks like an unlucky day rather than a bug.
    """
    return -(open_net_price + close_net_price) * CONTRACT_MULTIPLIER * spreads


def realised_pnl_from_fills(fills: Sequence[SpreadFill]) -> float:
    """Realised P&L across a set of parent fills that form complete round trips.

    Only meaningful over positions that have been both opened and closed. Given
    an opening fill alone this returns the credit received, which is money in
    the account but not yet earned -- so it must never be used as "today's
    realised P&L". That number comes from `pnl_snapshots`, which is sourced
    from the broker rather than derived here.
    """
    return -sum(f.net_price_per_spread * f.spreads for f in fills) * CONTRACT_MULTIPLIER


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One candidate considered at one stage of one cycle.

    `reasons` holds the reason codes as written -- `Denial`, `Rejection`,
    `Skip` and `RegimeBlock` are all `StrEnum`, so they store and read back as
    their own strings without this module importing any of them.
    """

    id: int
    at: datetime
    cycle_id: str
    stage: Stage
    symbol: str | None
    accepted: bool
    reasons: tuple[str, ...] = ()
    detail: tuple[str, ...] = ()
    context: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OrderRecord:
    """An order as the journal knows it, from intent to settled fate.

    `spreads_ordered` and `spreads_filled` are both STRATEGY UNITS, never
    contracts. A partially filled parent is the normal case rather than an
    edge: legs fill together, but two of five spreads filling while three keep
    working leaves a balanced, smaller position that is neither "filled" nor
    "unfilled". `net_price_per_spread` is SIGNED -- negative for a filled
    credit.
    """

    client_order_id: str
    cycle_id: str
    symbol: str
    intent_at: datetime
    payload: dict[str, object]
    status: OrderStatus
    spreads_ordered: float = 0.0
    spreads_filled: float = 0.0
    net_price_per_spread: float | None = None
    broker_order_id: str | None = None
    submitted_at: datetime | None = None
    status_at: datetime | None = None
    # When we last confirmed this order's state against the broker. NULL on an
    # intent that was journalled and never seen again, which is precisely the
    # crash-between-write-and-submit case restart has to chase.
    reconciled_at: datetime | None = None
    detail: str = ""

    @property
    def needs_reconciliation(self) -> bool:
        return not self.status.is_terminal

    @property
    def spreads_working(self) -> float:
        """Ordered but not yet filled, in strategy units."""
        return max(0.0, self.spreads_ordered - self.spreads_filled)

    @property
    def is_partially_filled(self) -> bool:
        """Some spreads done, some still working. Real, and not a rounding case."""
        return 0.0 < self.spreads_filled < self.spreads_ordered


@dataclass(frozen=True, slots=True)
class SpreadFill:
    """One PARENT-level execution, in strategy units at a signed net price.

    `spreads` counts spreads, not contracts. `net_price_per_spread` is signed:
    negative means a credit was received. `confirmed_at is None` means we only
    ever heard this over the websocket, which is not knowledge.
    """

    fill_id: str
    symbol: str
    trading_day: date
    occurred_at: datetime
    spreads: float
    net_price_per_spread: float
    source: FillSource
    attribution: FillAttribution
    client_order_id: str | None = None
    broker_order_id: str | None = None
    confirmed_at: datetime | None = None
    # Set when somebody has dealt with a fill that recovery was blocking on.
    # It deliberately does NOT set `confirmed_at`: acknowledging a broker
    # liquidation does not make it a REST-confirmed execution, it only records
    # that the anomaly was seen and settled.
    acknowledged_at: datetime | None = None
    acknowledgement: str = ""
    recorded_at: datetime | None = None
    detail: str = ""

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None

    @property
    def is_acknowledged(self) -> bool:
        return self.acknowledged_at is not None

    @property
    def credit_received(self) -> float:
        """Dollars received. Negative when this fill paid a debit."""
        return -self.net_price_per_spread * CONTRACT_MULTIPLIER * self.spreads


@dataclass(frozen=True, slots=True)
class LegFill:
    """One LEG-level execution, in contracts at that leg's own premium.

    `contracts` is `ratio_qty * spreads`, so it is a different number from the
    parent's `spreads` even for the same execution. `premium_per_contract` is
    the leg's own price and is always positive, whichever side we took.
    """

    fill_id: str
    occ_symbol: str
    trading_day: date
    occurred_at: datetime
    contracts: float
    premium_per_contract: float
    side: str
    source: FillSource
    parent_fill_id: str | None = None
    client_order_id: str | None = None
    confirmed_at: datetime | None = None
    recorded_at: datetime | None = None
    detail: str = ""

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None


@dataclass(frozen=True, slots=True)
class PositionRecord:
    """An open position as observed at a point in time.

    Field names mirror `risk.OpenPosition` so a recovered book feeds the risk
    engine without a translation layer inventing anything on the way.
    `spreads` is strategy units, consistent with everything else here.
    """

    symbol: str
    spreads: float
    max_loss: float
    unrealised_pnl: float = 0.0
    net_delta: float = 0.0
    client_order_id: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PositionBook:
    """One observation of the open book.

    `taken_at is None` means the book has never been observed, which is not the
    same fact as an observed-empty book and must not be collapsed into it.
    """

    id: int | None = None
    taken_at: datetime | None = None
    positions: tuple[PositionRecord, ...] = ()

    @property
    def observed(self) -> bool:
        return self.taken_at is not None

    @property
    def by_symbol(self) -> dict[str, PositionRecord]:
        return {p.symbol: p for p in self.positions}


@dataclass(frozen=True, slots=True)
class VanishedPosition:
    """A position that was in one snapshot and gone from the next.

    On a paper account this is the only same-day evidence that an assignment,
    an expiry or a broker liquidation happened, because none of those reach the
    websocket or the activities feed until the following day.
    """

    position: PositionRecord
    last_seen_at: datetime
    missing_at: datetime
    from_snapshot_id: int
    closing_fills: tuple[SpreadFill, ...] = ()

    @property
    def explained(self) -> bool:
        """True when a fill we initiated accounts for the disappearance."""
        return bool(self.closing_fills)


@dataclass(frozen=True, slots=True)
class PositionEvent:
    """A recorded reason a position left the book.

    Created the moment a disappearance is noticed, with `cause=UNKNOWN` and
    `evidence=INFERRED_FROM_SNAPSHOT` when nothing explains it. The next day's
    activity record is attached to this same row rather than filed as a second
    event.
    """

    id: int
    detected_at: datetime
    trading_day: date
    symbol: str
    cause: PositionEventCause
    evidence: PositionEventEvidence
    spreads: float = 0.0
    from_snapshot_id: int | None = None
    activity_id: str | None = None
    confirmed_at: datetime | None = None
    detail: str = ""

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None


@dataclass(frozen=True, slots=True)
class RegimeVerdictRecord:
    """One evaluation of the global regime filter."""

    id: int
    at: datetime
    allowed: bool
    blocks: tuple[str, ...] = ()
    detail: tuple[str, ...] = ()
    context: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PnlSnapshot:
    """A point-in-time P&L reading on one of the two series."""

    id: int
    at: datetime
    trading_day: date
    source: PnlSource
    realised_pnl: float
    unrealised_pnl: float = 0.0
    equity: float | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ReconciliationRecord:
    """One pass of comparing our view against the broker's."""

    id: int
    at: datetime
    scope: ReconciliationScope
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryState:
    """Everything a 3am restart needs before it is allowed to trade again.

    `realised_pnl_today` is `float | None` on purpose. The daily loss stop
    measures against it, so a value we cannot vouch for must not be handed over
    as a number -- an unknown that reads as 0.0 disarms the stop exactly when
    the day has already gone wrong. `None` forces the caller to deal with it.

    `view_age` says how long ago our picture of the broker was last confirmed.
    At a restart it is stale by definition, which is why `VIEW_STALE` is an
    expected gap rather than an alarming one: it means reconcile first.
    """

    trading_day: date
    unreconciled_orders: tuple[OrderRecord, ...] = ()
    book: PositionBook = field(default_factory=PositionBook)
    realised_pnl_today: float | None = None
    session_open_equity: float | None = None
    unconfirmed_fills: tuple[SpreadFill, ...] = ()
    unattributed_fills: tuple[SpreadFill, ...] = ()
    unexplained_exits: tuple[PositionEvent, ...] = ()
    # Disappearances noticed but not yet acted on. Durable, so a restart in the
    # middle of a diff still sees them.
    pending_vanishes: tuple[VanishedPosition, ...] = ()
    undiffed_snapshots: int = 0
    last_reconciled_at: datetime | None = None
    view_age: timedelta | None = None
    gaps: tuple[RecoveryGap, ...] = ()
    detail: tuple[str, ...] = ()

    @property
    def open_positions(self) -> tuple[PositionRecord, ...]:
        return self.book.positions

    @property
    def is_clean(self) -> bool:
        """True when recovery established every fact it went looking for."""
        return not self.gaps


_SCHEMA: Final = """
BEGIN IMMEDIATE;

CREATE TABLE schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE decisions (
    id       INTEGER PRIMARY KEY,
    at       TEXT    NOT NULL,
    cycle_id TEXT    NOT NULL,
    stage    TEXT    NOT NULL,
    symbol   TEXT,
    accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
    reasons  TEXT    NOT NULL,
    detail   TEXT    NOT NULL,
    context  TEXT    NOT NULL
);
CREATE INDEX decisions_by_time ON decisions (at DESC, id DESC);
CREATE INDEX decisions_by_outcome ON decisions (accepted, at DESC);

-- spreads_* are STRATEGY UNITS. net_price_per_spread is SIGNED, negative for
-- a filled credit. Neither is ever contracts, and neither is ever a leg price.
CREATE TABLE orders (
    client_order_id      TEXT PRIMARY KEY,
    cycle_id             TEXT NOT NULL,
    symbol               TEXT NOT NULL,
    intent_at            TEXT NOT NULL,
    payload              TEXT NOT NULL,
    status               TEXT NOT NULL,
    spreads_ordered      REAL NOT NULL,
    spreads_filled       REAL NOT NULL DEFAULT 0,
    net_price_per_spread REAL,
    broker_order_id      TEXT,
    submitted_at         TEXT,
    status_at            TEXT NOT NULL,
    reconciled_at        TEXT,
    detail               TEXT NOT NULL DEFAULT ''
);
CREATE INDEX orders_by_status ON orders (status, intent_at);

-- PARENT-level executions. No foreign key to orders on purpose: Alpaca's own
-- pre-expiry sell-out arrives as a fill on an order id we never created, and
-- refusing it would discard a real change in open risk. `attribution` records
-- that instead.
CREATE TABLE spread_fills (
    fill_id              TEXT PRIMARY KEY,
    client_order_id      TEXT,
    broker_order_id      TEXT,
    symbol               TEXT NOT NULL,
    trading_day          TEXT NOT NULL,
    occurred_at          TEXT NOT NULL,
    spreads              REAL NOT NULL,
    net_price_per_spread REAL NOT NULL,
    source               TEXT NOT NULL,
    attribution          TEXT NOT NULL,
    confirmed_at         TEXT,
    acknowledged_at      TEXT,
    acknowledgement      TEXT NOT NULL DEFAULT '',
    recorded_at          TEXT NOT NULL,
    detail               TEXT NOT NULL DEFAULT ''
);
CREATE INDEX spread_fills_by_order ON spread_fills (client_order_id);
CREATE INDEX spread_fills_unresolved ON spread_fills (acknowledged_at, confirmed_at);
CREATE INDEX spread_fills_by_day ON spread_fills (trading_day, occurred_at);
CREATE INDEX spread_fills_by_symbol ON spread_fills (symbol, occurred_at);

-- LEG-level executions. contracts = ratio_qty * spreads, and
-- premium_per_contract is that leg's own price, always positive.
CREATE TABLE leg_fills (
    fill_id              TEXT PRIMARY KEY,
    parent_fill_id       TEXT,
    client_order_id      TEXT,
    occ_symbol           TEXT NOT NULL,
    trading_day          TEXT NOT NULL,
    occurred_at          TEXT NOT NULL,
    contracts            REAL NOT NULL CHECK (contracts > 0),
    premium_per_contract REAL NOT NULL CHECK (premium_per_contract > 0),
    side                 TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    source               TEXT NOT NULL,
    confirmed_at         TEXT,
    recorded_at          TEXT NOT NULL,
    detail               TEXT NOT NULL DEFAULT ''
);
CREATE INDEX leg_fills_by_parent ON leg_fills (parent_fill_id);
CREATE INDEX leg_fills_by_day ON leg_fills (trading_day, occurred_at);

-- Two tables so that an observed-empty book is representable. One table would
-- make "flat" and "never looked" the same fact.
CREATE TABLE position_snapshots (
    id       INTEGER PRIMARY KEY,
    taken_at TEXT NOT NULL
);
CREATE INDEX position_snapshots_by_time ON position_snapshots (taken_at DESC, id DESC);

-- The snapshot diff is the primary same-day evidence of an assignment or an
-- expiry, so where it has got to must be durable. Without a cursor, a vanish
-- is only visible while it sits between the two newest snapshots and the next
-- polling cycle erases it from history.
CREATE TABLE position_diff_cursor (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    snapshot_id INTEGER NOT NULL REFERENCES position_snapshots (id),
    advanced_at TEXT    NOT NULL
);

CREATE TABLE snapshot_positions (
    snapshot_id     INTEGER NOT NULL REFERENCES position_snapshots (id) ON DELETE CASCADE,
    symbol          TEXT NOT NULL,
    spreads         REAL NOT NULL,
    max_loss        REAL NOT NULL,
    unrealised_pnl  REAL NOT NULL,
    net_delta       REAL NOT NULL,
    client_order_id TEXT,
    detail          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (snapshot_id, symbol)
);

-- activity_id is UNIQUE so the next day's non-trade activity can be attached
-- exactly once, however many times the feed replays it.
CREATE TABLE position_events (
    id               INTEGER PRIMARY KEY,
    detected_at      TEXT NOT NULL,
    trading_day      TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    cause            TEXT NOT NULL,
    evidence         TEXT NOT NULL,
    spreads          REAL NOT NULL DEFAULT 0,
    from_snapshot_id INTEGER,
    activity_id      TEXT UNIQUE,
    confirmed_at     TEXT,
    detail           TEXT NOT NULL DEFAULT '',
    UNIQUE (symbol, trading_day, from_snapshot_id)
);
CREATE INDEX position_events_open ON position_events (activity_id, detected_at DESC);

CREATE TABLE reconciliations (
    id     INTEGER PRIMARY KEY,
    at     TEXT    NOT NULL,
    scope  TEXT    NOT NULL,
    ok     INTEGER NOT NULL CHECK (ok IN (0, 1)),
    detail TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX reconciliations_by_scope ON reconciliations (scope, ok, at DESC, id DESC);

CREATE TABLE regime_verdicts (
    id      INTEGER PRIMARY KEY,
    at      TEXT    NOT NULL,
    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
    blocks  TEXT    NOT NULL,
    detail  TEXT    NOT NULL,
    context TEXT    NOT NULL
);
CREATE INDEX regime_by_time ON regime_verdicts (at DESC, id DESC);

CREATE TABLE pnl_snapshots (
    id             INTEGER PRIMARY KEY,
    at             TEXT NOT NULL,
    trading_day    TEXT NOT NULL,
    source         TEXT NOT NULL,
    realised_pnl   REAL NOT NULL,
    unrealised_pnl REAL NOT NULL,
    equity         REAL,
    detail         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX pnl_by_day ON pnl_snapshots (trading_day, source, at DESC, id DESC);

CREATE TABLE session_equity (
    trading_day TEXT PRIMARY KEY,
    equity      REAL NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


def _iso(moment: datetime) -> str:
    """Serialise an aware datetime as fixed-width ISO8601 UTC.

    The width is fixed and the offset is always `+00:00`, so `ORDER BY` on the
    text column is chronological. A naive datetime is rejected rather than
    assumed to be UTC: the assumption is right until the one time it is not,
    and by then it is in the audit trail.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        msg = f"timestamp must be timezone-aware, got {moment!r}"
        raise ValueError(msg)
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _ts(raw: str) -> datetime:
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        msg = f"unreadable timestamp in journal: {raw!r}"
        raise JournalError(msg) from exc
    if moment.tzinfo is None:
        msg = f"journal holds a naive timestamp, which cannot be trusted: {raw!r}"
        raise JournalError(msg)
    return moment.astimezone(UTC)


def _day(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        msg = f"unreadable trading day in journal: {raw!r}"
        raise JournalError(msg) from exc


def _dumps_obj(value: Mapping[str, object], *, what: str) -> str:
    try:
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        msg = f"{what} is not JSON-serialisable: {exc}"
        raise JournalError(msg) from exc


def _dumps_list(values: Sequence[str]) -> str:
    return json.dumps([str(v) for v in values], separators=(",", ":"))


def _loads_obj(raw: str, *, what: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        msg = f"unreadable {what} in journal: {raw!r}"
        raise JournalError(msg) from exc
    if not isinstance(parsed, dict):
        msg = f"{what} in journal is not an object: {raw!r}"
        raise JournalError(msg)
    return {str(k): v for k, v in parsed.items()}


def _loads_list(raw: str, *, what: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        msg = f"unreadable {what} in journal: {raw!r}"
        raise JournalError(msg) from exc
    if not isinstance(parsed, list):
        msg = f"{what} in journal is not a list: {raw!r}"
        raise JournalError(msg)
    items: list[str] = []
    for item in parsed:
        if not isinstance(item, str):
            msg = f"{what} in journal holds a non-string entry: {item!r}"
            raise JournalError(msg)
        items.append(item)
    return tuple(items)


def _text(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        msg = f"journal column {key!r} should be text, holds {value!r}"
        raise JournalError(msg)
    return value


def _opt_text(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"journal column {key!r} should be text or null, holds {value!r}"
        raise JournalError(msg)
    return value


def _real(row: sqlite3.Row, key: str) -> float:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"journal column {key!r} should be numeric, holds {value!r}"
        raise JournalError(msg)
    return float(value)


def _opt_real(row: sqlite3.Row, key: str) -> float | None:
    return None if row[key] is None else _real(row, key)


def _whole(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"journal column {key!r} should be an integer, holds {value!r}"
        raise JournalError(msg)
    return value


def _opt_whole(row: sqlite3.Row, key: str) -> int | None:
    return None if row[key] is None else _whole(row, key)


def _flag(row: sqlite3.Row, key: str) -> bool:
    return _whole(row, key) != 0


def _opt_ts(row: sqlite3.Row, key: str) -> datetime | None:
    raw = _opt_text(row, key)
    return None if raw is None else _ts(raw)


def _enum[E: StrEnum](kind: type[E], raw: str, *, what: str) -> E:
    """Read a stored code, refusing to guess at one we do not recognise."""
    try:
        return kind(raw)
    except ValueError as exc:
        msg = f"journal holds an unrecognised {what}: {raw!r}"
        raise JournalError(msg) from exc


def _limit_price(payload: Mapping[str, object]) -> float | None:
    """The order's signed limit price, if the payload carries a readable one.

    Returns None rather than raising on anything unexpected. The payload is
    whatever was sent to the broker and this module deliberately does not model
    it; a shape we cannot read costs us one optional cross-check, and pretending
    otherwise would couple the journal to the execution layer's serialisation.
    """
    raw = payload.get("limit_price")
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return None if value != value else value


def _finite(value: float, *, what: str) -> float:
    if value != value or value in (float("inf"), float("-inf")):
        msg = f"{what} must be a finite number, got {value!r}"
        raise ValueError(msg)
    return float(value)


def _to_decision(row: sqlite3.Row) -> DecisionRecord:
    return DecisionRecord(
        id=_whole(row, "id"),
        at=_ts(_text(row, "at")),
        cycle_id=_text(row, "cycle_id"),
        stage=_enum(Stage, _text(row, "stage"), what="cycle stage"),
        symbol=_opt_text(row, "symbol"),
        accepted=_flag(row, "accepted"),
        reasons=_loads_list(_text(row, "reasons"), what="decision reasons"),
        detail=_loads_list(_text(row, "detail"), what="decision detail"),
        context=_loads_obj(_text(row, "context"), what="decision context"),
    )


def _to_order(row: sqlite3.Row) -> OrderRecord:
    return OrderRecord(
        client_order_id=_text(row, "client_order_id"),
        cycle_id=_text(row, "cycle_id"),
        symbol=_text(row, "symbol"),
        intent_at=_ts(_text(row, "intent_at")),
        payload=_loads_obj(_text(row, "payload"), what="order payload"),
        status=_enum(OrderStatus, _text(row, "status"), what="order status"),
        spreads_ordered=_real(row, "spreads_ordered"),
        spreads_filled=_real(row, "spreads_filled"),
        net_price_per_spread=_opt_real(row, "net_price_per_spread"),
        broker_order_id=_opt_text(row, "broker_order_id"),
        submitted_at=_opt_ts(row, "submitted_at"),
        status_at=_ts(_text(row, "status_at")),
        reconciled_at=_opt_ts(row, "reconciled_at"),
        detail=_text(row, "detail"),
    )


def _to_spread_fill(row: sqlite3.Row) -> SpreadFill:
    return SpreadFill(
        fill_id=_text(row, "fill_id"),
        symbol=_text(row, "symbol"),
        trading_day=_day(_text(row, "trading_day")),
        occurred_at=_ts(_text(row, "occurred_at")),
        spreads=_real(row, "spreads"),
        net_price_per_spread=_real(row, "net_price_per_spread"),
        source=_enum(FillSource, _text(row, "source"), what="fill source"),
        attribution=_enum(FillAttribution, _text(row, "attribution"), what="fill attribution"),
        client_order_id=_opt_text(row, "client_order_id"),
        broker_order_id=_opt_text(row, "broker_order_id"),
        confirmed_at=_opt_ts(row, "confirmed_at"),
        acknowledged_at=_opt_ts(row, "acknowledged_at"),
        acknowledgement=_text(row, "acknowledgement"),
        recorded_at=_ts(_text(row, "recorded_at")),
        detail=_text(row, "detail"),
    )


def _to_leg_fill(row: sqlite3.Row) -> LegFill:
    return LegFill(
        fill_id=_text(row, "fill_id"),
        occ_symbol=_text(row, "occ_symbol"),
        trading_day=_day(_text(row, "trading_day")),
        occurred_at=_ts(_text(row, "occurred_at")),
        contracts=_real(row, "contracts"),
        premium_per_contract=_real(row, "premium_per_contract"),
        side=_text(row, "side"),
        source=_enum(FillSource, _text(row, "source"), what="fill source"),
        parent_fill_id=_opt_text(row, "parent_fill_id"),
        client_order_id=_opt_text(row, "client_order_id"),
        confirmed_at=_opt_ts(row, "confirmed_at"),
        recorded_at=_ts(_text(row, "recorded_at")),
        detail=_text(row, "detail"),
    )


def _to_position(row: sqlite3.Row) -> PositionRecord:
    return PositionRecord(
        symbol=_text(row, "symbol"),
        spreads=_real(row, "spreads"),
        max_loss=_real(row, "max_loss"),
        unrealised_pnl=_real(row, "unrealised_pnl"),
        net_delta=_real(row, "net_delta"),
        client_order_id=_opt_text(row, "client_order_id"),
        detail=_text(row, "detail"),
    )


def _to_position_event(row: sqlite3.Row) -> PositionEvent:
    return PositionEvent(
        id=_whole(row, "id"),
        detected_at=_ts(_text(row, "detected_at")),
        trading_day=_day(_text(row, "trading_day")),
        symbol=_text(row, "symbol"),
        cause=_enum(PositionEventCause, _text(row, "cause"), what="position event cause"),
        evidence=_enum(
            PositionEventEvidence, _text(row, "evidence"), what="position event evidence"
        ),
        spreads=_real(row, "spreads"),
        from_snapshot_id=_opt_whole(row, "from_snapshot_id"),
        activity_id=_opt_text(row, "activity_id"),
        confirmed_at=_opt_ts(row, "confirmed_at"),
        detail=_text(row, "detail"),
    )


def _to_reconciliation(row: sqlite3.Row) -> ReconciliationRecord:
    return ReconciliationRecord(
        id=_whole(row, "id"),
        at=_ts(_text(row, "at")),
        scope=_enum(ReconciliationScope, _text(row, "scope"), what="reconciliation scope"),
        ok=_flag(row, "ok"),
        detail=_text(row, "detail"),
    )


def _to_regime(row: sqlite3.Row) -> RegimeVerdictRecord:
    return RegimeVerdictRecord(
        id=_whole(row, "id"),
        at=_ts(_text(row, "at")),
        allowed=_flag(row, "allowed"),
        blocks=_loads_list(_text(row, "blocks"), what="regime blocks"),
        detail=_loads_list(_text(row, "detail"), what="regime detail"),
        context=_loads_obj(_text(row, "context"), what="regime context"),
    )


def _to_pnl(row: sqlite3.Row) -> PnlSnapshot:
    return PnlSnapshot(
        id=_whole(row, "id"),
        at=_ts(_text(row, "at")),
        trading_day=_day(_text(row, "trading_day")),
        source=_enum(PnlSource, _text(row, "source"), what="P&L source"),
        realised_pnl=_real(row, "realised_pnl"),
        unrealised_pnl=_real(row, "unrealised_pnl"),
        equity=_opt_real(row, "equity"),
        detail=_text(row, "detail"),
    )


class Journal:
    """A durable, append-mostly SQLite journal of everything the agent did.

    Open one per process. It is safe to open a second connection for the
    dashboard: WAL mode lets readers proceed while the agent writes.
    """

    __slots__ = ("_conn", "_path")

    def __init__(self, path: str | Path = MEMORY) -> None:
        self._path: str | Path = MEMORY if str(path) == MEMORY else Path(path)
        self._conn = sqlite3.connect(str(self._path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._ensure_schema()

    # -- lifecycle -------------------------------------------------------

    def _configure(self) -> None:
        """Set the pragmas that make a crash survivable.

        WAL, because a reader (the dashboard) must never block the writer (the
        trading cycle), and because a WAL commit is a single append rather than
        a rollback-journal dance that can leave a hot journal behind.

        `synchronous=FULL` rather than WAL's usual `NORMAL`. Under `NORMAL` the
        WAL is not fsynced on every commit, so an OS crash or power loss can
        lose the last few committed transactions -- and the transaction most
        likely to be lost is the newest one, which is precisely the order
        intent written moments before submitting. Losing that single row
        reintroduces the exact failure the write-ahead rule exists to prevent.
        The cost is one fsync per commit on a system doing a handful of writes
        per minute, which is nothing against an untracked open position.
        """
        cur = self._conn.cursor()
        try:
            cur.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            cur.execute("PRAGMA foreign_keys = ON")
            cur.execute("PRAGMA journal_mode = WAL")
            mode_row = cur.fetchone()
            cur.execute("PRAGMA synchronous = FULL")
            mode = str(mode_row[0]).lower() if mode_row is not None else "unknown"
        finally:
            cur.close()
        # An in-memory database reports "memory" and has no durability to
        # offer; that is fine, because tests are the only caller that asks for
        # one. A file-backed database that would not enter WAL is a different
        # matter: some network filesystems refuse it, and running on one would
        # mean believing in a durability guarantee we do not have.
        if isinstance(self._path, Path) and mode != "wal":
            msg = (
                f"{self._path} would not enter WAL mode (journal_mode={mode!r}). "
                "Durability cannot be guaranteed there; put the journal on a "
                "local filesystem."
            )
            raise JournalError(msg)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        """Run statements in one all-or-nothing transaction.

        `BEGIN IMMEDIATE` takes the write lock up front. A deferred transaction
        that reads first and writes later can fail to upgrade its lock when
        another connection got there in between, which surfaces as a spurious
        `database is locked` in the middle of recording an order.
        """
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield cur
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            cur.execute("COMMIT")
        finally:
            cur.close()

    def _ensure_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            tables = {str(row[0]) for row in cur.fetchall()}
        finally:
            cur.close()

        if "schema_version" not in tables:
            # A file holding other tables is somebody else's database. Creating
            # our schema alongside theirs would half-work, which is worse than
            # not working.
            real = {t for t in tables if not t.startswith("sqlite_")}
            if real:
                msg = (
                    f"{self._path} is not an underwriter journal: it holds "
                    f"{', '.join(sorted(real))} and no schema_version table."
                )
                raise JournalError(msg)
            self._create_schema()
            return

        self._check_version()

    def _create_schema(self) -> None:
        """Create the schema and stamp its version in one transaction.

        `executescript` performs no transaction control of its own, so the
        `BEGIN IMMEDIATE` at the top of `_SCHEMA` is still open when it
        returns. The version row is therefore written and committed with the
        tables it describes -- a half-created database that could not say what
        version it was would be refused on the next open, and the recovery
        from that is deleting a file by hand at 3am.
        """
        cur = self._conn.cursor()
        try:
            cur.executescript(_SCHEMA)
            try:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, _iso(datetime.now(UTC))),
                )
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            cur.execute("COMMIT")
        finally:
            cur.close()

    def _check_version(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute("SELECT MAX(version) AS version FROM schema_version")
            row = cur.fetchone()
        finally:
            cur.close()
        found = None if row is None or row["version"] is None else _whole(row, "version")
        if found is None:
            msg = f"{self._path} has an empty schema_version table; it is not usable."
            raise JournalError(msg)
        if found > SCHEMA_VERSION:
            msg = (
                f"{self._path} was written by schema version {found}; this build "
                f"understands {SCHEMA_VERSION}. Refusing to open it: a column this "
                "build cannot see could be the one holding an open position."
            )
            raise SchemaTooNewError(msg)
        if found < SCHEMA_VERSION:
            msg = (
                f"{self._path} is at schema version {found} and there is no "
                f"migration to {SCHEMA_VERSION}. Refusing to open it rather than "
                "reading it as though the missing columns were empty."
            )
            raise SchemaTooOldError(msg)

    @property
    def path(self) -> str | Path:
        return self._path

    @property
    def journal_mode(self) -> str:
        cur = self._conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode")
            row = cur.fetchone()
        finally:
            cur.close()
        return "unknown" if row is None else str(row[0]).lower()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @staticmethod
    def _moment(at: datetime | None) -> str:
        return _iso(at if at is not None else datetime.now(UTC))

    # -- decisions -------------------------------------------------------

    def record_decision(
        self,
        *,
        cycle_id: str,
        stage: Stage,
        accepted: bool,
        symbol: str | None = None,
        reasons: Sequence[str] = (),
        detail: Sequence[str] = (),
        context: Mapping[str, object] | None = None,
        at: datetime | None = None,
    ) -> int:
        """Record one candidate's outcome at one stage.

        A rejection with no reason is refused. The dashboard and the submission
        both promise a judge that every refusal names itself, and a blank
        reason breaks that promise in the one place it matters.
        """
        if not accepted and not reasons:
            msg = f"a rejection must carry at least one reason ({stage}, {symbol})"
            raise ValueError(msg)
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO decisions (at, cycle_id, stage, symbol, accepted, "
                "reasons, detail, context) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._moment(at),
                    cycle_id,
                    str(stage),
                    symbol,
                    int(accepted),
                    _dumps_list(reasons),
                    _dumps_list(detail),
                    _dumps_obj(context or {}, what="decision context"),
                ),
            )
            return int(cur.lastrowid or 0)

    def recent_decisions(
        self,
        limit: int = 50,
        *,
        cycle_id: str | None = None,
        symbol: str | None = None,
    ) -> tuple[DecisionRecord, ...]:
        """Newest first, for the dashboard's activity feed."""
        clauses: list[str] = []
        params: list[object] = []
        if cycle_id is not None:
            clauses.append("cycle_id = ?")
            params.append(cycle_id)
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM decisions {where}ORDER BY at DESC, id DESC LIMIT ?",  # noqa: S608
            params,
        ).fetchall()
        return tuple(_to_decision(row) for row in rows)

    def rejections(self, limit: int = 50) -> tuple[DecisionRecord, ...]:
        """Every candidate that did not become a trade, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM decisions WHERE accepted = 0 ORDER BY at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(_to_decision(row) for row in rows)

    # -- orders ----------------------------------------------------------

    def record_intent(
        self,
        *,
        client_order_id: str,
        cycle_id: str,
        symbol: str,
        spreads: float,
        payload: Mapping[str, object],
        at: datetime | None = None,
    ) -> OrderRecord:
        """Journal an order BEFORE submitting it. Nothing may reorder these two.

        `spreads` is the quantity in STRATEGY UNITS -- the `qty` of the
        multi-leg order, not the contract count of any leg.

        Returns the stored record. Calling this twice with the same
        `client_order_id` and the same payload is a no-op that returns the
        original -- a retry after an ambiguous crash must not create a second
        row. Calling it with the same id and a *different* payload raises,
        because two different orders sharing the identifier we reconcile by
        would make the crash unrecoverable.
        """
        if not client_order_id.strip():
            msg = "client_order_id must be non-empty; it is what reconciliation matches on"
            raise ValueError(msg)
        if _finite(spreads, what="spreads") <= 0:
            msg = f"an order must be for at least one spread, got {spreads!r}"
            raise ValueError(msg)
        moment = self._moment(at)
        body = _dumps_obj(payload, what="order payload")
        with self._transaction() as cur:
            cur.execute("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,))
            existing = cur.fetchone()
            if existing is not None:
                stored = _to_order(existing)
                if (
                    _text(existing, "payload") != body
                    or stored.symbol != symbol
                    or stored.spreads_ordered != float(spreads)
                ):
                    msg = (
                        f"client_order_id {client_order_id!r} is already journalled "
                        f"for {stored.spreads_ordered:g} spread(s) of {stored.symbol} "
                        "with a different payload. Reusing it would make two orders "
                        "indistinguishable after a crash."
                    )
                    raise ConflictingIntentError(msg)
                return stored
            cur.execute(
                "INSERT INTO orders (client_order_id, cycle_id, symbol, intent_at, "
                "payload, status, spreads_ordered, status_at, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')",
                (
                    client_order_id,
                    cycle_id,
                    symbol,
                    moment,
                    body,
                    str(OrderStatus.INTENT),
                    float(spreads),
                    moment,
                ),
            )
            cur.execute("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,))
            return _to_order(cur.fetchone())

    def mark_submitted(
        self,
        client_order_id: str,
        *,
        broker_order_id: str | None = None,
        at: datetime | None = None,
        detail: str = "",
    ) -> OrderRecord:
        """Record that the broker acknowledged the submission."""
        moment = at if at is not None else datetime.now(UTC)
        return self._advance(
            client_order_id,
            status=OrderStatus.SUBMITTED,
            broker_order_id=broker_order_id,
            at=moment,
            submitted_at=moment,
            spreads_filled=None,
            net_price_per_spread=None,
            detail=detail,
        )

    def mark_status(
        self,
        client_order_id: str,
        status: OrderStatus,
        *,
        spreads_filled: float | None = None,
        net_price_per_spread: float | None = None,
        broker_order_id: str | None = None,
        at: datetime | None = None,
        detail: str = "",
    ) -> OrderRecord:
        """Record a broker-reported status and, with it, the parent fill state.

        `spreads_filled` is the parent's `filled_qty` in STRATEGY UNITS and
        `net_price_per_spread` its `filled_avg_price`, SIGNED -- negative for a
        filled credit. Passing a leg's contract count or its positive premium
        here is the conflation that silently misstates open risk, so a filled
        quantity above what was ordered, or one that goes backwards, is refused
        rather than stored.

        Every call also stamps `reconciled_at`: any status after `INTENT` came
        from the broker, so it is by definition a confirmation of the order's
        state at that moment.
        """
        return self._advance(
            client_order_id,
            status=status,
            broker_order_id=broker_order_id,
            at=at if at is not None else datetime.now(UTC),
            submitted_at=None,
            spreads_filled=spreads_filled,
            net_price_per_spread=net_price_per_spread,
            detail=detail,
        )

    def abandon(
        self,
        client_order_id: str,
        *,
        detail: str,
        at: datetime | None = None,
    ) -> OrderRecord:
        """Close out an intent the broker never received.

        Only for the case where reconciliation asked for this
        `client_order_id` and Alpaca had no such order. `detail` is required:
        an order that vanished must explain itself in the audit trail.
        """
        if not detail.strip():
            msg = "abandoning an intent requires a reason for the audit trail"
            raise ValueError(msg)
        return self._advance(
            client_order_id,
            status=OrderStatus.ABANDONED,
            broker_order_id=None,
            at=at if at is not None else datetime.now(UTC),
            submitted_at=None,
            spreads_filled=None,
            net_price_per_spread=None,
            detail=detail,
        )

    def _advance(
        self,
        client_order_id: str,
        *,
        status: OrderStatus,
        broker_order_id: str | None,
        at: datetime,
        submitted_at: datetime | None,
        spreads_filled: float | None,
        net_price_per_spread: float | None,
        detail: str,
    ) -> OrderRecord:
        moment = _iso(at)
        with self._transaction() as cur:
            cur.execute("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,))
            row = cur.fetchone()
            if row is None:
                msg = (
                    f"no journalled intent for client_order_id {client_order_id!r}. "
                    "An order reached the broker without being journalled first, "
                    "which breaks the write-ahead rule."
                )
                raise UnknownOrderError(msg)
            current = _to_order(row)
            if current.status.is_terminal and current.status is not status:
                # A settled order changing its mind means two sources disagree
                # about what happened, and quietly taking the newer one would
                # bury that. Duplicate deliveries of the SAME terminal status
                # are fine and fall through as an idempotent no-op.
                msg = (
                    f"{client_order_id!r} is already {current.status} and cannot "
                    f"become {status}. The broker and the journal disagree about "
                    "an order that was settled."
                )
                raise JournalError(msg)
            filled = self._checked_filled(current, spreads_filled)
            cur.execute(
                "UPDATE orders SET status = ?, status_at = ?, reconciled_at = ?, "
                "spreads_filled = ?, "
                "net_price_per_spread = COALESCE(?, net_price_per_spread), "
                "broker_order_id = COALESCE(?, broker_order_id), "
                "submitted_at = COALESCE(submitted_at, ?), "
                "detail = CASE WHEN ? = '' THEN detail ELSE ? END "
                "WHERE client_order_id = ?",
                (
                    str(status),
                    moment,
                    moment,
                    filled,
                    None
                    if net_price_per_spread is None
                    else _finite(net_price_per_spread, what="net_price_per_spread"),
                    broker_order_id,
                    _iso(submitted_at) if submitted_at is not None else None,
                    detail,
                    detail,
                    client_order_id,
                ),
            )
            cur.execute("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,))
            return _to_order(cur.fetchone())

    @staticmethod
    def _checked_filled(current: OrderRecord, spreads_filled: float | None) -> float:
        """Validate a parent fill quantity against what the order can possibly do.

        Two impossible numbers, both of which mean the same mistake. A filled
        quantity above the ordered quantity is the classic spreads-versus-
        contracts confusion: five spreads across two legs is ten contracts, and
        ten filled against five ordered looks like a doubled position. A filled
        quantity that goes backwards means we are reading a different order.
        Neither can be true, and storing either understates or overstates open
        risk without looking wrong.
        """
        if spreads_filled is None:
            return current.spreads_filled
        value = _finite(spreads_filled, what="spreads_filled")
        if value < 0:
            msg = f"spreads_filled cannot be negative, got {value!r}"
            raise ValueError(msg)
        if value > current.spreads_ordered:
            msg = (
                f"{current.client_order_id!r} ordered {current.spreads_ordered:g} "
                f"spread(s) but was reported filled for {value:g}. That is the "
                "parent's strategy-unit count confused with a leg's contract "
                "count (contracts = ratio_qty x spreads)."
            )
            raise UnitConfusionError(msg)
        if value < current.spreads_filled:
            msg = (
                f"{current.client_order_id!r} was filled for "
                f"{current.spreads_filled:g} spread(s) and is now reported at "
                f"{value:g}. Fills do not un-happen."
            )
            raise UnitConfusionError(msg)
        return value

    def order(self, client_order_id: str) -> OrderRecord | None:
        row = self._conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        return None if row is None else _to_order(row)

    def unreconciled_orders(self) -> tuple[OrderRecord, ...]:
        """Every order whose fate is not settled, oldest first.

        Non-terminal is the whole test. An order the broker last told us was
        `accepted` -- or `partially_filled`, with spreads still working -- is
        still live, so it comes back on every restart until something terminal
        happens to it. Being seen once is not the same as being finished.
        """
        placeholders = ", ".join("?" for _ in _TERMINAL_STATUSES)
        rows = self._conn.execute(
            f"SELECT * FROM orders WHERE status NOT IN ({placeholders}) "  # noqa: S608
            "ORDER BY intent_at ASC, client_order_id ASC",
            [str(s) for s in sorted(_TERMINAL_STATUSES)],
        ).fetchall()
        return tuple(_to_order(row) for row in rows)

    def order_history(self, limit: int = 100) -> tuple[OrderRecord, ...]:
        """Every order, newest intent first."""
        rows = self._conn.execute(
            "SELECT * FROM orders ORDER BY intent_at DESC, client_order_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(_to_order(row) for row in rows)

    # -- fills -----------------------------------------------------------

    def record_spread_fill(
        self,
        *,
        fill_id: str,
        symbol: str,
        trading_day: date,
        spreads: float,
        net_price_per_spread: float,
        occurred_at: datetime,
        source: FillSource,
        client_order_id: str | None = None,
        broker_order_id: str | None = None,
        detail: str = "",
        at: datetime | None = None,
    ) -> bool:
        """Record a PARENT-level execution. Returns True if it was new.

        `spreads` is STRATEGY UNITS and `net_price_per_spread` is SIGNED --
        negative for a credit received.

        Keyed on the broker's own execution id, so the same trade update
        delivered twice cannot double-count. That matters more than it looks: a
        double-counted fill inflates realised P&L, and realised P&L is what the
        daily loss stop measures.

        Source decides belief. A `STREAM` fill is stored unconfirmed, because
        `trade_updates` reconnects without a cursor and every disconnect is a
        silent gap. Re-recording the same `fill_id` from `REST` upgrades it in
        place: the confirmation stamp is set and the REST figures win, since
        the socket is a latency optimisation and never the system of record.

        A fill for an order we never journalled is *recorded*, not refused.
        Alpaca sells a position out before expiry when buying power is short
        (docs/GOTCHAS.md #10) and that arrives on an order id we never created;
        discarding it would lose a real change in open risk. It is marked
        `BROKER_INITIATED` or `UNKNOWN_ORDER` and surfaces as a recovery gap.

        When the fill DOES name an order of ours, both numbers are cross-
        checked against it. This ledger is what `realised_pnl_from_fills` and
        `credit_received` read, so the unit guards matter more here than on the
        order row: a leg's contract count written into `spreads`, or a leg's
        positive premium written into a signed `net_price_per_spread`, would
        misstate P&L rather than merely misstate the audit trail.
        """
        if not fill_id.strip():
            msg = "fill_id must be non-empty; it is what makes fills idempotent"
            raise ValueError(msg)
        if _finite(spreads, what="spreads") <= 0:
            msg = f"a fill must move at least one spread, got {spreads!r}"
            raise ValueError(msg)
        _finite(net_price_per_spread, what="net_price_per_spread")
        recorded = self._moment(at)
        confirmed = recorded if source is FillSource.REST else None

        with self._transaction() as cur:
            cur.execute("SELECT * FROM spread_fills WHERE fill_id = ?", (fill_id,))
            existing = cur.fetchone()
            # Validate only the figures that are about to be written. A repeat
            # stream delivery is discarded unread, so raising on its contents
            # would be an error about data we never intended to keep.
            upgrading = existing is not None and (
                source is FillSource.REST and _opt_text(existing, "confirmed_at") is None
            )
            if existing is None or upgrading:
                owner = (
                    client_order_id if existing is None else _opt_text(existing, "client_order_id")
                )
                self._check_fill_against_order(
                    cur,
                    client_order_id=owner,
                    fill_id=fill_id,
                    spreads=float(spreads),
                    net_price_per_spread=float(net_price_per_spread),
                )
            if existing is not None:
                self._confirm_spread_fill(
                    cur,
                    existing=existing,
                    source=source,
                    spreads=float(spreads),
                    net_price_per_spread=float(net_price_per_spread),
                    confirmed=confirmed,
                )
                return False
            attribution = self._attribute(cur, client_order_id)
            cur.execute(
                "INSERT INTO spread_fills (fill_id, client_order_id, broker_order_id, "
                "symbol, trading_day, occurred_at, spreads, net_price_per_spread, "
                "source, attribution, confirmed_at, recorded_at, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fill_id,
                    client_order_id,
                    broker_order_id,
                    symbol,
                    trading_day.isoformat(),
                    _iso(occurred_at),
                    float(spreads),
                    float(net_price_per_spread),
                    str(source),
                    str(attribution),
                    confirmed,
                    recorded,
                    detail,
                ),
            )
            return True

    @staticmethod
    def _attribute(cur: sqlite3.Cursor, client_order_id: str | None) -> FillAttribution:
        if client_order_id is None:
            return FillAttribution.BROKER_INITIATED
        cur.execute("SELECT 1 FROM orders WHERE client_order_id = ?", (client_order_id,))
        if cur.fetchone() is None:
            return FillAttribution.UNKNOWN_ORDER
        return FillAttribution.JOURNALLED

    @staticmethod
    def _check_fill_against_order(
        cur: sqlite3.Cursor,
        *,
        client_order_id: str | None,
        fill_id: str,
        spreads: float,
        net_price_per_spread: float,
    ) -> None:
        """Refuse a fill whose numbers the named order cannot possibly produce.

        Two checks, and they differ in strength on purpose.

        The quantity check is exact: fills against one order cannot sum past
        what it ordered. Five spreads across two legs is ten contracts, so a
        leg's contract count arriving here shows up as an order for five that
        filled ten -- the same confusion `_checked_filled` catches on the order
        row, arriving one call earlier and on the column P&L is computed from.
        The sum excludes this `fill_id` so that re-recording or confirming an
        existing execution is not mistaken for a second one.

        The sign check is best-effort, because the payload is opaque to this
        module by design. Where the order carries a signed `limit_price` we can
        still say that a credit order does not fill at a debit: opening a
        credit spread is submitted at a negative limit (docs/GOTCHAS.md #7) and
        must fill negative, while every leg premium is positive -- so a
        positive net price under a credit order is a leg premium in the parent
        column. A payload without a readable limit price simply skips this
        check; the quantity check above does not depend on it.
        """
        if client_order_id is None:
            return
        cur.execute(
            "SELECT spreads_ordered, payload FROM orders WHERE client_order_id = ?",
            (client_order_id,),
        )
        row = cur.fetchone()
        if row is None:
            # No order to check against. The fill is still recorded, and
            # `attribution` flags it for somebody to look at.
            return
        ordered = _real(row, "spreads_ordered")
        cur.execute(
            "SELECT COALESCE(SUM(spreads), 0) AS filled FROM spread_fills "
            "WHERE client_order_id = ? AND fill_id != ?",
            (client_order_id, fill_id),
        )
        already = _real(cur.fetchone(), "filled")
        if already + spreads > ordered + _SPREAD_EPSILON:
            msg = (
                f"fill {fill_id!r} would take {client_order_id!r} to "
                f"{already + spreads:g} spread(s) filled against {ordered:g} "
                "ordered. That is a leg's contract count in the parent's "
                "strategy-unit column (contracts = ratio_qty x spreads)."
            )
            raise UnitConfusionError(msg)

        limit = _limit_price(_loads_obj(_text(row, "payload"), what="order payload"))
        if limit is None or limit == 0 or net_price_per_spread == 0:
            return
        if (limit < 0) != (net_price_per_spread < 0):
            msg = (
                f"fill {fill_id!r} reports a net price of "
                f"{net_price_per_spread:+.4f} per spread against "
                f"{client_order_id!r}, submitted at a limit of {limit:+.4f}. A "
                "credit order cannot fill at a debit; a positive price here is "
                "a leg premium written into the parent's signed net."
            )
            raise UnitConfusionError(msg)

    @staticmethod
    def _confirm_spread_fill(
        cur: sqlite3.Cursor,
        *,
        existing: sqlite3.Row,
        source: FillSource,
        spreads: float,
        net_price_per_spread: float,
        confirmed: str | None,
    ) -> None:
        """Upgrade a stream-only fill when REST says the same thing.

        Only REST confirms, and only once. If the authoritative figures differ
        from what the socket reported, REST wins and the disagreement is
        written into `detail` rather than smoothed over -- a stream and a REST
        read disagreeing about an execution is worth seeing.
        """
        if source is not FillSource.REST or _opt_text(existing, "confirmed_at") is not None:
            return
        was_spreads = _real(existing, "spreads")
        was_price = _real(existing, "net_price_per_spread")
        note = _text(existing, "detail")
        if was_spreads != spreads or was_price != net_price_per_spread:
            disagreement = (
                f"REST confirmed {spreads:g} spread(s) at {net_price_per_spread:+.4f}; "
                f"the stream had reported {was_spreads:g} at {was_price:+.4f}."
            )
            note = f"{note} {disagreement}".strip()
        cur.execute(
            "UPDATE spread_fills SET spreads = ?, net_price_per_spread = ?, "
            "source = ?, confirmed_at = ?, detail = ? WHERE fill_id = ?",
            (
                spreads,
                net_price_per_spread,
                str(FillSource.REST),
                confirmed,
                note,
                _text(existing, "fill_id"),
            ),
        )

    def record_leg_fill(
        self,
        *,
        fill_id: str,
        occ_symbol: str,
        trading_day: date,
        contracts: float,
        premium_per_contract: float,
        side: str,
        occurred_at: datetime,
        source: FillSource,
        parent_fill_id: str | None = None,
        client_order_id: str | None = None,
        detail: str = "",
        at: datetime | None = None,
    ) -> bool:
        """Record a LEG-level execution. Returns True if it was new.

        `contracts` is `ratio_qty * spreads` and `premium_per_contract` is that
        leg's own price, which is positive on both sides of the trade. A
        non-positive premium here can only mean the parent's signed net price
        was written into a leg row, so it is refused: that single confusion
        would flip the sign of the position's P&L and never look wrong.
        """
        if not fill_id.strip():
            msg = "fill_id must be non-empty; it is what makes fills idempotent"
            raise ValueError(msg)
        if side not in _SIDES:
            msg = f"side must be one of {sorted(_SIDES)}, got {side!r}"
            raise ValueError(msg)
        if _finite(contracts, what="contracts") <= 0:
            msg = f"a leg fill must move at least one contract, got {contracts!r}"
            raise ValueError(msg)
        if _finite(premium_per_contract, what="premium_per_contract") <= 0:
            msg = (
                f"a leg premium is always positive, got {premium_per_contract!r}. "
                "A signed net price belongs on the parent, not on a leg."
            )
            raise UnitConfusionError(msg)
        recorded = self._moment(at)
        confirmed = recorded if source is FillSource.REST else None
        with self._transaction() as cur:
            cur.execute("SELECT confirmed_at FROM leg_fills WHERE fill_id = ?", (fill_id,))
            existing = cur.fetchone()
            if existing is not None:
                if source is FillSource.REST and _opt_text(existing, "confirmed_at") is None:
                    cur.execute(
                        "UPDATE leg_fills SET contracts = ?, premium_per_contract = ?, "
                        "source = ?, confirmed_at = ? WHERE fill_id = ?",
                        (
                            float(contracts),
                            float(premium_per_contract),
                            str(FillSource.REST),
                            confirmed,
                            fill_id,
                        ),
                    )
                return False
            cur.execute(
                "INSERT INTO leg_fills (fill_id, parent_fill_id, client_order_id, "
                "occ_symbol, trading_day, occurred_at, contracts, premium_per_contract, "
                "side, source, confirmed_at, recorded_at, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fill_id,
                    parent_fill_id,
                    client_order_id,
                    occ_symbol,
                    trading_day.isoformat(),
                    _iso(occurred_at),
                    float(contracts),
                    float(premium_per_contract),
                    side,
                    str(source),
                    confirmed,
                    recorded,
                    detail,
                ),
            )
            return True

    def spread_fill(self, fill_id: str) -> SpreadFill | None:
        row = self._conn.execute(
            "SELECT * FROM spread_fills WHERE fill_id = ?", (fill_id,)
        ).fetchone()
        return None if row is None else _to_spread_fill(row)

    def spread_fills_for(self, client_order_id: str) -> tuple[SpreadFill, ...]:
        rows = self._conn.execute(
            "SELECT * FROM spread_fills WHERE client_order_id = ? "
            "ORDER BY occurred_at ASC, fill_id ASC",
            (client_order_id,),
        ).fetchall()
        return tuple(_to_spread_fill(row) for row in rows)

    def spread_fills_on(self, trading_day: date) -> tuple[SpreadFill, ...]:
        rows = self._conn.execute(
            "SELECT * FROM spread_fills WHERE trading_day = ? "
            "ORDER BY occurred_at ASC, fill_id ASC",
            (trading_day.isoformat(),),
        ).fetchall()
        return tuple(_to_spread_fill(row) for row in rows)

    def leg_fills_for(self, parent_fill_id: str) -> tuple[LegFill, ...]:
        rows = self._conn.execute(
            "SELECT * FROM leg_fills WHERE parent_fill_id = ? "
            "ORDER BY occurred_at ASC, fill_id ASC",
            (parent_fill_id,),
        ).fetchall()
        return tuple(_to_leg_fill(row) for row in rows)

    def unconfirmed_fills(self, *, trading_day: date | None = None) -> tuple[SpreadFill, ...]:
        """Fills we heard on the socket, never verified, and never acknowledged.

        Unresolved rather than recent. Scoping this to today would let a
        stream-only fill from yesterday evening fall out of view at midnight,
        and a restart the next morning would report a clean recovery while
        still holding an execution nobody ever checked. Pass `trading_day` only
        for a dashboard that wants one session's worth.
        """
        return self._unresolved("confirmed_at IS NULL", [], trading_day)

    def unattributed_fills(self, *, trading_day: date | None = None) -> tuple[SpreadFill, ...]:
        """Fills that belong to no order of ours and have not been acknowledged.

        A broker liquidation, or an order that reached Alpaca without being
        journalled first. Either way somebody has to look, and the passage of
        midnight is not somebody looking.
        """
        return self._unresolved("attribution != ?", [str(FillAttribution.JOURNALLED)], trading_day)

    def _unresolved(
        self, clause: str, params: list[object], trading_day: date | None
    ) -> tuple[SpreadFill, ...]:
        full = f"{clause} AND acknowledged_at IS NULL"
        if trading_day is not None:
            full += " AND trading_day = ?"
            params = [*params, trading_day.isoformat()]
        rows = self._conn.execute(
            f"SELECT * FROM spread_fills WHERE {full} "  # noqa: S608
            "ORDER BY occurred_at ASC, fill_id ASC",
            params,
        ).fetchall()
        return tuple(_to_spread_fill(row) for row in rows)

    def acknowledge_fill(
        self, fill_id: str, *, detail: str, at: datetime | None = None
    ) -> SpreadFill:
        """Record that somebody dealt with a fill recovery was blocking on.

        A broker liquidation is never going to become attributable and a lost
        stream event is never going to be confirmed retroactively, so without
        this they would raise their gaps forever -- and a gap that is always on
        is a gap nobody reads. Acknowledging stops it blocking recovery and
        says who decided that and why.

        It deliberately does not set `confirmed_at`. The fill remains, in the
        audit trail, a thing we never verified; what changed is that we know
        about it. `detail` is required for the same reason.
        """
        if not detail.strip():
            msg = "acknowledging a fill requires a reason for the audit trail"
            raise ValueError(msg)
        moment = self._moment(at)
        with self._transaction() as cur:
            cur.execute(
                "UPDATE spread_fills SET acknowledged_at = ?, acknowledgement = ? "
                "WHERE fill_id = ?",
                (moment, detail, fill_id),
            )
            if cur.rowcount != 1:
                msg = f"no journalled fill with fill_id {fill_id!r} to acknowledge"
                raise JournalError(msg)
            cur.execute("SELECT * FROM spread_fills WHERE fill_id = ?", (fill_id,))
            return _to_spread_fill(cur.fetchone())

    # -- positions -------------------------------------------------------

    def record_position_snapshot(
        self,
        positions: Sequence[PositionRecord],
        *,
        at: datetime | None = None,
    ) -> int:
        """Record the whole open book as observed at one moment.

        Called every monitoring cycle, because on a paper account this diff is
        the only same-day evidence of an assignment, an exercise or an expiry:
        none of those reach the websocket, and the activities feed does not
        catch up until the next day. The position simply stops being listed.

        The book is stored as a snapshot rather than as incremental edits
        because reconstructing "what do we hold" by replaying edits means one
        missed edit corrupts everything after it. An empty sequence is a
        meaningful, recorded observation: it says the book was seen flat, which
        is a different fact from never having looked.
        """
        moment = self._moment(at)
        with self._transaction() as cur:
            cur.execute("INSERT INTO position_snapshots (taken_at) VALUES (?)", (moment,))
            snapshot_id = int(cur.lastrowid or 0)
            cur.executemany(
                "INSERT INTO snapshot_positions (snapshot_id, symbol, spreads, "
                "max_loss, unrealised_pnl, net_delta, client_order_id, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        snapshot_id,
                        p.symbol,
                        float(p.spreads),
                        float(p.max_loss),
                        float(p.unrealised_pnl),
                        float(p.net_delta),
                        p.client_order_id,
                        p.detail,
                    )
                    for p in positions
                ],
            )
            return snapshot_id

    def recent_position_books(self, limit: int = 2) -> tuple[PositionBook, ...]:
        """The most recent observations of the book, newest first."""
        rows = self._conn.execute(
            "SELECT id, taken_at FROM position_snapshots ORDER BY taken_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(self._book(row) for row in rows)

    def latest_positions(self) -> PositionBook:
        """The most recent observation of the book, or an unobserved book."""
        books = self.recent_position_books(1)
        return books[0] if books else PositionBook()

    def _book(self, row: sqlite3.Row) -> PositionBook:
        snapshot_id = _whole(row, "id")
        positions = self._conn.execute(
            "SELECT * FROM snapshot_positions WHERE snapshot_id = ? ORDER BY symbol ASC",
            (snapshot_id,),
        ).fetchall()
        return PositionBook(
            id=snapshot_id,
            taken_at=_ts(_text(row, "taken_at")),
            positions=tuple(_to_position(p) for p in positions),
        )

    def position_diff_cursor(self) -> int | None:
        """The newest snapshot whose arrival has been diffed and acted on."""
        row = self._conn.execute(
            "SELECT snapshot_id FROM position_diff_cursor WHERE id = 1"
        ).fetchone()
        return None if row is None else _whole(row, "snapshot_id")

    def undiffed_snapshots(self) -> int:
        """How many snapshots have arrived since the diff last caught up."""
        cursor = self.position_diff_cursor()
        if cursor is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM position_snapshots").fetchone()
            # The first snapshot establishes a baseline rather than a change,
            # so a single one is not a backlog.
            return max(0, _whole(row, "n") - 1)
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM position_snapshots WHERE id > ?", (cursor,)
        ).fetchone()
        return _whole(row, "n")

    def vanished_positions(self) -> tuple[VanishedPosition, ...]:
        """Every position that has left the book since the diff last caught up.

        This is the primary same-day evidence that a position was assigned,
        exercised, expired or liquidated: none of those reach the websocket,
        and on paper none reach the activities feed until the next day. The
        position simply stops being listed.

        Which is why the diff has a durable cursor rather than comparing only
        the two newest snapshots. Comparing the newest pair makes a
        disappearance visible for exactly one polling cycle: record the next
        snapshot before the diff was consumed and the assignment is gone from
        the journal permanently, with nothing anywhere recording that it
        happened. So this walks every consecutive pair from
        `position_diff_cursor()` forward and reports all of them.

        Detection is at-least-once and the recording is idempotent. If the
        process dies after `record_position_event` and before
        `mark_positions_diffed`, the next call re-reports the same
        disappearance and `record_position_event` recognises it by
        `(symbol, trading_day, from_snapshot_id)` rather than filing it twice.
        Crashing the other way round is why the cursor is advanced by the
        caller and never here.

        Each entry carries whatever closing fills of ours landed in that pair's
        window, so an exit we initiated is distinguishable from one that simply
        happened to us.
        """
        books = self._books_since(self.position_diff_cursor())
        vanished: list[VanishedPosition] = []
        for previous, latest in itertools.pairwise(books):
            if previous.taken_at is None or latest.taken_at is None or previous.id is None:
                continue
            held_now = latest.by_symbol
            vanished.extend(
                VanishedPosition(
                    position=position,
                    last_seen_at=previous.taken_at,
                    missing_at=latest.taken_at,
                    from_snapshot_id=previous.id,
                    closing_fills=self._fills_between(
                        symbol=position.symbol,
                        start=previous.taken_at,
                        end=latest.taken_at,
                    ),
                )
                for symbol, position in previous.by_symbol.items()
                if symbol not in held_now
            )
        return tuple(vanished)

    def mark_positions_diffed(self, snapshot_id: int, *, at: datetime | None = None) -> int:
        """Advance the diff cursor, once the vanishes up to here are recorded.

        Called by the caller rather than by `vanished_positions`, so that a
        crash between reading the diff and acting on it re-reports rather than
        silently consuming it. The cursor never moves backwards: re-running an
        older diff must not make an already-handled disappearance pending
        again.
        """
        moment = self._moment(at)
        with self._transaction() as cur:
            cur.execute("SELECT 1 FROM position_snapshots WHERE id = ?", (snapshot_id,))
            if cur.fetchone() is None:
                msg = f"no position snapshot with id {snapshot_id!r} to diff up to"
                raise JournalError(msg)
            cur.execute(
                "INSERT INTO position_diff_cursor (id, snapshot_id, advanced_at) "
                "VALUES (1, ?, ?) ON CONFLICT (id) DO UPDATE SET "
                "snapshot_id = MAX(snapshot_id, excluded.snapshot_id), "
                "advanced_at = excluded.advanced_at",
                (snapshot_id, moment),
            )
            cur.execute("SELECT snapshot_id FROM position_diff_cursor WHERE id = 1")
            return _whole(cur.fetchone(), "snapshot_id")

    def _books_since(self, cursor: int | None) -> tuple[PositionBook, ...]:
        """Snapshots from the cursor forward, oldest first, cursor included.

        The cursor snapshot itself is the `previous` half of the first
        undiffed pair, so it has to come back with them.
        """
        if cursor is None:
            rows = self._conn.execute(
                "SELECT id, taken_at FROM position_snapshots ORDER BY taken_at ASC, id ASC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, taken_at FROM position_snapshots WHERE id >= ? "
                "ORDER BY taken_at ASC, id ASC",
                (cursor,),
            ).fetchall()
        return tuple(self._book(row) for row in rows)

    def _fills_between(
        self, *, symbol: str, start: datetime, end: datetime
    ) -> tuple[SpreadFill, ...]:
        """Our own fills on a symbol within a window.

        Only `JOURNALLED` fills count as an explanation. A broker-initiated
        sell-out is exactly the thing we are trying to detect, so letting it
        explain the disappearance would defeat the check.
        """
        rows = self._conn.execute(
            "SELECT * FROM spread_fills WHERE symbol = ? AND attribution = ? "
            "AND occurred_at > ? AND occurred_at <= ? ORDER BY occurred_at ASC, fill_id ASC",
            (symbol, str(FillAttribution.JOURNALLED), _iso(start), _iso(end)),
        ).fetchall()
        return tuple(_to_spread_fill(row) for row in rows)

    def record_position_event(
        self,
        *,
        symbol: str,
        trading_day: date,
        cause: PositionEventCause,
        evidence: PositionEventEvidence = PositionEventEvidence.INFERRED_FROM_SNAPSHOT,
        spreads: float = 0.0,
        from_snapshot_id: int | None = None,
        activity_id: str | None = None,
        detail: str = "",
        at: datetime | None = None,
    ) -> PositionEvent:
        """Record why a position left the book, including "we do not know".

        `UNKNOWN` with `INFERRED_FROM_SNAPSHOT` is the honest same-day answer
        for a position that vanished with no closing fill of ours, and it is
        the value this is expected to be called with most often. Recording such
        an exit as a clean close would quietly turn a broker liquidation, an
        assignment or an expiry into a trade we meant to make.

        Re-detecting the same disappearance from the same snapshot returns the
        existing event rather than filing a second one, so a monitor that runs
        twice in a cycle cannot inflate the count.
        """
        moment = self._moment(at)
        confirmed = moment if evidence is PositionEventEvidence.CONFIRMED_BY_ACTIVITY else None
        with self._transaction() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO position_events (detected_at, trading_day, symbol, "
                "cause, evidence, spreads, from_snapshot_id, activity_id, confirmed_at, "
                "detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    moment,
                    trading_day.isoformat(),
                    symbol,
                    str(cause),
                    str(evidence),
                    float(spreads),
                    from_snapshot_id,
                    activity_id,
                    confirmed,
                    detail,
                ),
            )
            if cur.rowcount == 1:
                cur.execute(
                    "SELECT * FROM position_events WHERE id = ?", (int(cur.lastrowid or 0),)
                )
                return _to_position_event(cur.fetchone())
            cur.execute(
                "SELECT * FROM position_events WHERE symbol = ? AND trading_day = ? "
                "AND from_snapshot_id IS ?",
                (symbol, trading_day.isoformat(), from_snapshot_id),
            )
            return _to_position_event(cur.fetchone())

    def confirm_position_event(
        self,
        *,
        activity_id: str,
        symbol: str,
        trading_day: date,
        cause: PositionEventCause,
        spreads: float | None = None,
        detail: str = "",
        at: datetime | None = None,
    ) -> PositionEvent:
        """Attach the next day's activity record to an exit we already inferred.

        Paper accounts sync non-trade activities at the start of the following
        day, so the authoritative record of an assignment or an expiry always
        arrives after we have already noticed the position disappear. It
        belongs on the same event rather than beside it -- two rows for one
        assignment would read as two assignments.

        Idempotent on `activity_id`: the feed can be replayed as often as it
        likes. When nothing was inferred -- the position came and went between
        two snapshots, or the agent was down -- the activity is recorded as a
        confirmed event in its own right.
        """
        if not activity_id.strip():
            msg = "activity_id must be non-empty; it is what makes confirmation idempotent"
            raise ValueError(msg)
        moment = self._moment(at)
        with self._transaction() as cur:
            cur.execute("SELECT * FROM position_events WHERE activity_id = ?", (activity_id,))
            already = cur.fetchone()
            if already is not None:
                return _to_position_event(already)

            cur.execute(
                "SELECT * FROM position_events WHERE symbol = ? AND trading_day = ? "
                "AND activity_id IS NULL ORDER BY detected_at DESC, id DESC LIMIT 1",
                (symbol, trading_day.isoformat()),
            )
            inferred = cur.fetchone()
            if inferred is None:
                cur.execute(
                    "INSERT INTO position_events (detected_at, trading_day, symbol, cause, "
                    "evidence, spreads, activity_id, confirmed_at, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        moment,
                        trading_day.isoformat(),
                        symbol,
                        str(cause),
                        str(PositionEventEvidence.CONFIRMED_BY_ACTIVITY),
                        0.0 if spreads is None else float(spreads),
                        activity_id,
                        moment,
                        detail,
                    ),
                )
                cur.execute(
                    "SELECT * FROM position_events WHERE id = ?", (int(cur.lastrowid or 0),)
                )
                return _to_position_event(cur.fetchone())

            cur.execute(
                "UPDATE position_events SET cause = ?, evidence = ?, activity_id = ?, "
                "confirmed_at = ?, spreads = COALESCE(?, spreads), "
                "detail = CASE WHEN ? = '' THEN detail ELSE ? END WHERE id = ?",
                (
                    str(cause),
                    str(PositionEventEvidence.CONFIRMED_BY_ACTIVITY),
                    activity_id,
                    moment,
                    None if spreads is None else float(spreads),
                    detail,
                    detail,
                    _whole(inferred, "id"),
                ),
            )
            cur.execute("SELECT * FROM position_events WHERE id = ?", (_whole(inferred, "id"),))
            return _to_position_event(cur.fetchone())

    def position_events(
        self, *, trading_day: date | None = None, limit: int = 100
    ) -> tuple[PositionEvent, ...]:
        clause = "" if trading_day is None else "WHERE trading_day = ? "
        params: list[object] = [] if trading_day is None else [trading_day.isoformat()]
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM position_events {clause}"  # noqa: S608
            "ORDER BY detected_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return tuple(_to_position_event(row) for row in rows)

    def unexplained_position_events(self) -> tuple[PositionEvent, ...]:
        """Exits we inferred and cannot yet account for, oldest first.

        An exit we made ourselves is explained the moment it is recorded. Every
        other one waits for tomorrow's activity record, and until it arrives
        the book has changed for a reason nobody has verified.
        """
        rows = self._conn.execute(
            "SELECT * FROM position_events WHERE activity_id IS NULL AND cause != ? "
            "ORDER BY detected_at ASC, id ASC",
            (str(PositionEventCause.CLOSED_BY_US),),
        ).fetchall()
        return tuple(_to_position_event(row) for row in rows)

    # -- reconciliation ---------------------------------------------------

    def record_reconciliation(
        self,
        *,
        scope: ReconciliationScope,
        ok: bool,
        detail: str = "",
        at: datetime | None = None,
    ) -> int:
        """Record a pass of comparing our view against the broker's.

        Failed passes are recorded too, and deliberately do not refresh the
        clock: `last_reconciliation` reports the last pass that actually
        *succeeded*, because an attempt that errored out tells us nothing about
        the broker and everything about the network.
        """
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO reconciliations (at, scope, ok, detail) VALUES (?, ?, ?, ?)",
                (self._moment(at), str(scope), int(ok), detail),
            )
            return int(cur.lastrowid or 0)

    def last_reconciliation(
        self, scope: ReconciliationScope = ReconciliationScope.FULL
    ) -> ReconciliationRecord | None:
        """The most recent SUCCESSFUL pass over this scope, if there was one."""
        row = self._conn.execute(
            "SELECT * FROM reconciliations WHERE scope = ? AND ok = 1 "
            "ORDER BY at DESC, id DESC LIMIT 1",
            (str(scope),),
        ).fetchone()
        return None if row is None else _to_reconciliation(row)

    def view_age(
        self, *, now: datetime, scope: ReconciliationScope = ReconciliationScope.FULL
    ) -> timedelta | None:
        """How long ago our picture of the broker was last confirmed.

        `None` means never, which is staler than any number and must not be
        compared against a threshold as though it were zero.
        """
        last = self.last_reconciliation(scope)
        if last is None:
            return None
        if now.tzinfo is None or now.utcoffset() is None:
            msg = f"now must be timezone-aware, got {now!r}"
            raise ValueError(msg)
        return now.astimezone(UTC) - last.at

    # -- regime ----------------------------------------------------------

    def record_regime_verdict(
        self,
        *,
        allowed: bool,
        blocks: Sequence[str] = (),
        detail: Sequence[str] = (),
        context: Mapping[str, object] | None = None,
        at: datetime | None = None,
    ) -> int:
        """Record one evaluation of the global regime filter.

        The filter should be judged on whether it fired at the right times, not
        on P&L, so its verdicts are journalled whether or not they blocked
        anything.
        """
        if not allowed and not blocks:
            msg = "a regime block must name at least one reason"
            raise ValueError(msg)
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO regime_verdicts (at, allowed, blocks, detail, context) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    self._moment(at),
                    int(allowed),
                    _dumps_list(blocks),
                    _dumps_list(detail),
                    _dumps_obj(context or {}, what="regime context"),
                ),
            )
            return int(cur.lastrowid or 0)

    def regime_history(self, limit: int = 50) -> tuple[RegimeVerdictRecord, ...]:
        rows = self._conn.execute(
            "SELECT * FROM regime_verdicts ORDER BY at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return tuple(_to_regime(row) for row in rows)

    # -- P&L and session equity ------------------------------------------

    def record_pnl(
        self,
        *,
        trading_day: date,
        source: PnlSource,
        realised_pnl: float,
        unrealised_pnl: float = 0.0,
        equity: float | None = None,
        detail: str = "",
        at: datetime | None = None,
    ) -> int:
        """Append a P&L reading on one of the two series."""
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO pnl_snapshots (at, trading_day, source, realised_pnl, "
                "unrealised_pnl, equity, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self._moment(at),
                    trading_day.isoformat(),
                    str(source),
                    _finite(realised_pnl, what="realised_pnl"),
                    _finite(unrealised_pnl, what="unrealised_pnl"),
                    None if equity is None else _finite(equity, what="equity"),
                    detail,
                ),
            )
            return int(cur.lastrowid or 0)

    def latest_pnl(
        self, *, trading_day: date, source: PnlSource = PnlSource.OFFICIAL
    ) -> PnlSnapshot | None:
        row = self._conn.execute(
            "SELECT * FROM pnl_snapshots WHERE trading_day = ? AND source = ? "
            "ORDER BY at DESC, id DESC LIMIT 1",
            (trading_day.isoformat(), str(source)),
        ).fetchone()
        return None if row is None else _to_pnl(row)

    def record_session_open_equity(
        self,
        *,
        trading_day: date,
        equity: float,
        at: datetime | None = None,
    ) -> float:
        """Record the session's opening equity, once per trading day.

        The first write for a day wins and later ones are ignored; the value of
        record is returned either way. This is deliberate. The daily loss stop
        measures the day's P&L against session-open equity, so re-recording it
        mid-session would let the baseline drift with P&L and the stop would
        never trigger -- see the same reasoning in `risk.AccountState`.

        A non-finite or non-positive equity is refused outright. It would be
        stored as a baseline that silently disables the stop, and the risk
        engine already treats unreadable equity as a denial rather than a zero.
        """
        if _finite(equity, what="session-open equity") <= 0:
            msg = f"session-open equity must be a positive finite number, got {equity!r}"
            raise ValueError(msg)
        with self._transaction() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO session_equity (trading_day, equity, recorded_at) "
                "VALUES (?, ?, ?)",
                (trading_day.isoformat(), float(equity), self._moment(at)),
            )
            cur.execute(
                "SELECT equity FROM session_equity WHERE trading_day = ?",
                (trading_day.isoformat(),),
            )
            return _real(cur.fetchone(), "equity")

    def session_open_equity(self, trading_day: date) -> float | None:
        """The recorded session-open equity, or None if the day never opened."""
        row = self._conn.execute(
            "SELECT equity FROM session_equity WHERE trading_day = ?",
            (trading_day.isoformat(),),
        ).fetchone()
        return None if row is None else _real(row, "equity")

    # -- recovery --------------------------------------------------------

    def recover(
        self,
        trading_day: date,
        *,
        now: datetime | None = None,
        max_view_age: timedelta = DEFAULT_MAX_VIEW_AGE,
    ) -> RecoveryState:
        """Everything a restart needs before the agent may trade again.

        Every question it cannot answer produces a displayable gap rather than
        a comfortable default:

        1. Which orders are unsettled? Anything non-terminal, including bare
           intents from a crash between journalling and submitting, and
           partially filled parents with spreads still working. Each must be
           looked up at the broker by `client_order_id`.
        2. What do we hold? The most recent observed book. Never observed but
           orders exist is a gap, because flat and unknown are different.
        3. What has today realised? The newest official P&L snapshot -- but
           only if no fill has landed since it was taken. A snapshot that
           predates a fill is stale, and a stale realised figure understates
           the loss the daily stop is measuring.
        4. What did the session open at? Required by the daily loss stop, and
           unrecoverable after the fact, so its absence is a gap.
        5. How old is our view? Anything past `max_view_age`, or never
           reconciled at all, means reconcile before trading. At a restart this
           is expected rather than alarming.
        6. What do we half-know? Stream-only fills, fills belonging to no order
           of ours, and positions that left the book unexplained. These are
           scoped by whether they are resolved, not by date -- an unconfirmed
           fill does not become acceptable because the clock passed midnight.
        7. What have we not looked at? Snapshots recorded since the diff last
           caught up, and the disappearances sitting in them. On paper that
           diff is the only same-day evidence of an assignment, so a backlog
           is a hole in the record rather than a chore.
        """
        gaps: list[RecoveryGap] = []
        detail: list[str] = []

        unreconciled = self.unreconciled_orders()
        if unreconciled:
            gaps.append(RecoveryGap.UNRECONCILED_ORDERS)
            intents = sum(1 for o in unreconciled if o.status is OrderStatus.INTENT)
            working = sum(o.spreads_working for o in unreconciled)
            detail.append(
                f"{len(unreconciled)} order(s) unsettled and {working:g} spread(s) "
                f"possibly working, {intents} never confirmed submitted. Reconcile "
                "each against the broker by client_order_id before trading."
            )

        book = self.latest_positions()
        if not book.observed and self.order_history(1):
            gaps.append(RecoveryGap.POSITIONS_UNOBSERVED)
            detail.append(
                "Orders exist but the book has never been snapshotted, so the "
                "open position set is unknown rather than empty."
            )

        realised, pnl_gap = self._realised_today(trading_day)
        if pnl_gap is not None:
            gaps.append(RecoveryGap.REALISED_PNL_UNKNOWN)
            detail.append(pnl_gap)

        opening = self.session_open_equity(trading_day)
        if opening is None:
            gaps.append(RecoveryGap.SESSION_EQUITY_MISSING)
            detail.append(
                f"No session-open equity recorded for {trading_day.isoformat()}; "
                "the daily loss stop has nothing to measure against."
            )

        last = self.last_reconciliation()
        age = self.view_age(now=now) if now is not None else None
        if last is None or age is None or age > max_view_age:
            gaps.append(RecoveryGap.VIEW_STALE)
            detail.append(
                "The broker has never been reconciled; our view of orders and "
                "positions is unverified."
                if last is None
                else f"Last successful reconciliation was {last.at.isoformat()}"
                + (
                    "; supply `now` to age it."
                    if age is None
                    else f", {age} ago, beyond the {max_view_age} limit."
                )
            )

        unconfirmed = self.unconfirmed_fills()
        if unconfirmed:
            gaps.append(RecoveryGap.UNCONFIRMED_FILLS)
            detail.append(
                f"{len(unconfirmed)} fill(s) came from the trade-updates stream and "
                "were never confirmed by a REST read. The stream reconnects without "
                "a cursor, so it is a latency optimisation, not the record."
            )

        unattributed = self.unattributed_fills()
        if unattributed:
            gaps.append(RecoveryGap.UNATTRIBUTED_FILLS)
            detail.append(
                f"{len(unattributed)} fill(s) belong to no order of ours -- a broker "
                "liquidation before expiry, or an order submitted without being "
                "journalled first."
            )

        unexplained = self.unexplained_position_events()
        if unexplained:
            gaps.append(RecoveryGap.UNEXPLAINED_POSITION_EXITS)
            symbols = ", ".join(sorted({e.symbol for e in unexplained}))
            detail.append(
                f"{len(unexplained)} position(s) left the book without a closing fill "
                f"of ours ({symbols}). On paper the activity record arrives tomorrow, "
                "so until then the cause is inferred, not known."
            )

        pending = self.vanished_positions()
        backlog = self.undiffed_snapshots()
        if pending or backlog:
            gaps.append(RecoveryGap.POSITION_DIFFS_PENDING)
            unexplained_pending = [v for v in pending if not v.explained]
            detail.append(
                f"{backlog} position snapshot(s) recorded since the diff last "
                f"caught up, holding {len(pending)} departure(s), "
                f"{len(unexplained_pending)} of them with no closing fill of ours. "
                "On paper that diff is the only same-day evidence of an "
                "assignment or an expiry."
            )

        return RecoveryState(
            trading_day=trading_day,
            unreconciled_orders=unreconciled,
            book=book,
            realised_pnl_today=realised,
            session_open_equity=opening,
            unconfirmed_fills=unconfirmed,
            unattributed_fills=unattributed,
            unexplained_exits=unexplained,
            pending_vanishes=pending,
            undiffed_snapshots=backlog,
            last_reconciled_at=None if last is None else last.at,
            view_age=age,
            gaps=tuple(gaps),
            detail=tuple(detail),
        )

    def _realised_today(self, trading_day: date) -> tuple[float | None, str | None]:
        """Today's realised P&L, or None with a reason it cannot be trusted.

        Sourced from the broker's own figure rather than derived from fills.
        Summing fills would count the credit on a position that is open and not
        yet earned, which reads as profit right up until the spread is bought
        back.
        """
        snapshot = self.latest_pnl(trading_day=trading_day)
        row = self._conn.execute(
            "SELECT MAX(occurred_at) AS last_fill FROM spread_fills WHERE trading_day = ?",
            (trading_day.isoformat(),),
        ).fetchone()
        last_fill = None if row is None else _opt_text(row, "last_fill")

        if snapshot is None:
            if last_fill is None:
                # Nothing filled today and nothing was reported: the day has
                # realised nothing, and that is a fact rather than a guess.
                return 0.0, None
            return None, (
                f"Fills landed on {trading_day.isoformat()} but no official P&L "
                "snapshot was ever recorded, so today's realised figure is unknown."
            )

        if last_fill is not None and last_fill > _iso(snapshot.at):
            return None, (
                f"The newest official P&L snapshot for {trading_day.isoformat()} "
                f"predates a fill at {last_fill}, so its realised figure is stale."
            )
        return snapshot.realised_pnl, None
