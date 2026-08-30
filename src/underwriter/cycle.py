"""One pass of the agent's state machine, from what we hold to what we send.

Every other module in this package answers one question well and refuses to
answer anything else. This one is the only place that knows the *order* those
answers must be produced in, and the order is the whole safety argument:

    observe -> baseline -> reconcile -> scan -> rank -> regime -> EXIT
            -> select -> veto -> risk -> journal -> execute -> review

`baseline` is the session-open equity, written second because it is the one
fact that cannot be recovered later; everything else in the pass can be
retried next cycle.

Four properties are load-bearing, and each is enforced by construction rather
than by remembering.

**The book is observed first, and a cycle that cannot establish it opens
nothing.** Every gate downstream -- open risk, aggregate delta, the position
cap, the duplicate-symbol check -- is a function of what we hold. Reasoning
about a book we could not read is not conservative, it is arbitrary: it reads
as an empty account, which is the most permissive state there is. So a failed
observation halts entries with a displayable reason, while exits of positions
we cannot see are moot by definition.

**Exits outrank entries, structurally.** `_open_positions` takes an `ExitPass`
as an argument and `_close_positions` is the only thing that can produce one,
so the two stages cannot be transposed without a type error. Managing what we
hold matters more than adding to it, and `decide_exits` has already put the
deadline-driven closes at the front of the queue.

**Nothing is submitted before it is journalled.** `record_intent` commits in
its own transaction before `execution.submit` is called, on every path, for
opens and closes alike. That ordering is the entire crash-recovery story: an
order the broker holds and we have no record of cannot be reconciled, because
reconciliation needs an identifier to reconcile by (docs/GOTCHAS.md #9).

**One bad symbol never aborts the cycle.** Per-symbol work is fenced, and a
failure becomes a recorded `SymbolFailure` rather than an exception that takes
the remaining candidates -- and, far worse, the pending exits -- with it.

Two deliberate departures from a naive reading of the state machine:

1. The scan, the ranking and the regime verdict run *before* the exits, not
   after. `decide_exit` consumes a `RegimeVerdict`, so the verdict has to
   exist by then. Reading the market is not acting on it: nothing is opened
   until every exit has been decided and placed.
2. The scan runs even when entries are already barred. The ranking and the
   regime verdict are what a judge reads to see the filters firing, and a
   cycle that stands down without showing its work is indistinguishable from
   one that is broken.

Everything the cycle touches outside this process arrives by injection -- a
market source, a broker, an execution adapter, a journal, a clock -- so a full
cycle runs in a test with no network and no credentials. That is not a testing
convenience; it is how the ordering above is *proved* rather than asserted.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from underwriter.chain import (
    Contract,
    ContractType,
    CreditPolicy,
    CreditSpread,
    ExpiryWindow,
    LiquidityPolicy,
    Quote,
    SpreadEconomics,
    select_credit_vertical,
)
from underwriter.config import RiskLimits
from underwriter.data import (
    Bars,
    SnapshotLike,
    atm_implied_vol,
    contracts_from_chain,
    delta_from,
    quote_from,
    term_structure_from,
)
from underwriter.execution import (
    MultiLegOrder,
    OrderResult,
    Reason,
    build_closing_order,
    build_opening_order,
)
from underwriter.exits import (
    ExitDecision,
    ExitPolicy,
    ExitReason,
    closing_debit,
    decide_exits,
)
from underwriter.journal import (
    EXCHANGE_TZ,
    IntentLeg,
    Journal,
    JournalError,
    OrderRecord,
    OrderStatus,
    PnlSource,
    ReconciliationScope,
    RecoveryGap,
    RecoveryState,
    Stage,
    trading_day_of,
)
from underwriter.occ import parse as parse_occ
from underwriter.positions import (
    BookObservation,
    OpenSpread,
    RawOptionPosition,
    observe_book,
    reassemble_spreads,
)
from underwriter.preflight import AccountLike, PreflightReport
from underwriter.regime import (
    BENCHMARK,
    Blocked,
    RegimeBlock,
    RegimePolicy,
    RegimeVerdict,
    TermStructure,
    evaluate_regime,
)
from underwriter.risk import AccountState, OpenPosition, max_risk_dollars
from underwriter.risk import evaluate as evaluate_risk
from underwriter.shadow import shadow_for_day
from underwriter.universe import symbols as universe_symbols
from underwriter.volatility import Skip, Skipped, VolPolicy, VolRanking, rank_instrument

# The far leg of the term structure. `min_term_structure_gap_days` requires the
# two samples to be meaningfully apart, so the far window starts well beyond
# the entry window rather than adjacent to it. See the strategy spec: near-dated
# implied vol is compared against "the next two months", not the next fortnight.
FAR_EXPIRY_MIN_DTE = 30
FAR_EXPIRY_MAX_DTE = 60

# Whether a failed submission provably created nothing at the broker.
#
# This used to be inferred from a set of reason codes, which could not be
# right: `Reason.API_ERROR` is emitted on both the terminal 4xx branch and the
# unknown 5xx branch, so the reason alone cannot distinguish "rejected before
# it reached the order system" from "we have no idea what happened".
#
# `OrderResult.proven_absent` now carries the observation instead. It is true
# only where absence was seen -- a terminal backend outcome, or a lookup that
# positively returned ABSENT -- and false for every genuinely unknown outcome,
# which leaves the order unreconciled and blocks the symbol next cycle. Being
# wrong toward unknown costs a missed entry; being wrong the other way submits
# the same spread twice.


def _never_reached_broker(result: OrderResult) -> bool:
    """True only when the broker is known to hold no such order."""
    return result.proven_absent


def _ranking_context(ranking: VolRanking | None) -> dict[str, object] | None:
    """The measured volatility figures, as data a reader can use.

    Returned as None when there is no ranking, so a decision carries either the
    real numbers or nothing -- never a placeholder that would read as a
    measurement.
    """
    if ranking is None:
        return None
    return {
        "vrp_ratio": round(ranking.vrp_ratio, 4),
        "implied_vol": round(ranking.implied_vol, 6),
        "realised_vol": round(ranking.realised_vol, 6),
        "realised_is_expanding": ranking.realised_is_expanding,
    }


class Halt(StrEnum):
    """Why a cycle opened nothing. Displayed verbatim, recorded every time."""

    BOOK_UNKNOWN = "book_unknown"
    ACCOUNT_UNREADABLE = "account_unreadable"
    PREFLIGHT_MISSING = "preflight_missing"
    PREFLIGHT_FAILED = "preflight_failed"
    KILL_SWITCH = "kill_switch"
    REGIME_BLOCKED = "regime_blocked"
    VETO_UNAVAILABLE = "veto_unavailable"
    URGENT_EXIT_UNPLACED = "urgent_exit_unplaced"
    NO_CANDIDATES = "no_candidates"


class Action(StrEnum):
    """Which side of the book an order moves."""

    OPEN = "open"
    CLOSE = "close"


class Failed(StrEnum):
    """Why a step gave up without reaching a verdict.

    Deliberately distinct from every rejection enum in the package: those mean
    "we looked and the answer is no", these mean "we could not look". The two
    must never read alike in the audit log, because one is the strategy working
    and the other is the strategy blind.
    """

    OBSERVE_ERROR = "observe_error"
    SCAN_ERROR = "scan_error"
    EXIT_ERROR = "exit_error"
    ENTRY_ERROR = "entry_error"
    RECONCILE_ERROR = "reconcile_error"
    ACCOUNT_ERROR = "account_error"


class Refusal(StrEnum):
    """Refusals the cycle itself owns, because no other module sees the seam.

    Each one is a decision, not a failure: we looked, and the answer is no.
    """

    UNRECONCILED_ORDER = "unreconciled_order"
    EXIT_ALREADY_WORKING = "exit_already_working"
    EXIT_UNPRICEABLE = "exit_unpriceable"
    NO_WHOLE_SPREAD = "no_whole_spread"
    NO_SPREAD_AVAILABLE = "no_spread_available"
    VETO_CATALYST = "veto_catalyst"


# --------------------------------------------------------------------------
# The ET clock
# --------------------------------------------------------------------------


def session_time_et(moment: datetime) -> time:
    """The exchange-local wall clock, which is what every session rule means.

    `risk.evaluate` takes a bare `time` and compares it against the 15:00 ET
    entry cutoff; `exits.past_flatten_cutoff` does the same against the flatten
    window. Handing either a UTC time would move both rules by four or five
    hours depending on the month, and the failure is silent in exactly one
    direction: entries continue past the cutoff.
    """
    return moment.astimezone(EXCHANGE_TZ).time()


def cycle_id_for(moment: datetime) -> str:
    """A stable, sortable identifier for one pass of the state machine.

    `{trading day}-{HHMMSS}`, both on the exchange's clock so the whole id
    belongs to one session rather than straddling two. Sortable lexicographic-
    ally, readable in a dashboard, and derived purely from the injected clock,
    so a test with a frozen clock gets a deterministic id.
    """
    local = moment.astimezone(EXCHANGE_TZ)
    return f"{trading_day_of(moment).isoformat()}-{local.strftime('%H%M%S')}"


# --------------------------------------------------------------------------
# Injection points
# --------------------------------------------------------------------------


class Clock(Protocol):
    """The only source of "now" in the cycle.

    Injected rather than called directly so a test can run a full cycle at
    14:55 ET on an expiration Friday without waiting for one.
    """

    def now(self) -> datetime:
        """The current moment, timezone-aware. A naive datetime is refused."""
        ...


@dataclass(frozen=True, slots=True)
class SystemClock:
    """The real clock. UTC, because a naive local time is not a time."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class AccountView(AccountLike, Protocol):
    """The account fields the cycle reads.

    Extends `preflight.AccountLike` rather than declaring a second, competing
    shape -- preflight already owns the description of an Alpaca account, and
    two protocols over the same object drift apart. The one addition is
    `last_equity`: the broker's own previous-close equity, which is the only
    honest basis for the day's P&L, and which preflight has no use for.
    """

    @property
    def last_equity(self) -> object: ...


class Broker(Protocol):
    """Account state and the raw position list, as the broker reports them."""

    def account(self) -> AccountView: ...

    def positions(self) -> Sequence[RawOptionPosition]:
        """Individual option contracts, not spreads. Reassembly is ours."""
        ...


class MarketSource(Protocol):
    """Prices. `data.MarketData` satisfies the first two members today."""

    def daily_closes(self, symbols: Sequence[str]) -> Bars: ...

    def chain(self, underlying: str, window: ExpiryWindow) -> Mapping[str, SnapshotLike]:
        """Raw chain slice. Both expiry bounds are mandatory (GOTCHAS #1)."""
        ...

    def option_snapshots(self, symbols: Sequence[str]) -> Mapping[str, SnapshotLike]:
        """Snapshots for named contracts, whatever their expiry.

        Held positions routinely sit outside the entry window, so the exit path
        cannot price itself from the chains the scan already fetched.
        """
        ...


class Executor(Protocol):
    """The order path. `execution.ExecutionAdapter` satisfies this exactly."""

    def submit(self, order: MultiLegOrder, *, dry_run: bool = False) -> OrderResult: ...


@dataclass(frozen=True, slots=True)
class BrokerOrderView:
    """The broker's own answer about one order, for the reconciliation sweep."""

    status: str
    order_id: str | None = None
    # Parent units: spreads, and the SIGNED net per spread (GOTCHAS #8).
    filled_qty: float | None = None
    filled_avg_price: float | None = None
    detail: str = ""


class OrderReader(Protocol):
    """Reads an order back from the broker by our own client order id.

    Optional. Without it the cycle can still journal and submit, but it can
    never *settle* an order whose outcome was unknown, so that symbol stays
    blocked -- which is the fail-closed direction and is reported as such.

    Note for implementers: listing orders requires `nested=true` or the legs
    come back as separate flat orders and the join goes wrong without
    complaint (docs/GOTCHAS.md #8).
    """

    def order_status(self, client_order_id: str) -> BrokerOrderView | None:
        """The broker's view, or None if it positively has no such order.

        None means *proven absent* and nothing else. A lookup that timed out,
        errored, or could not be completed must raise: returning None for it
        would abandon an intent that may be a live order, which is the one
        mistake this whole module is arranged to prevent.
        """
        ...


@dataclass(frozen=True, slots=True)
class VetoVerdict:
    """The catalyst veto's answer for one candidate."""

    vetoed: bool
    catalyst: str = ""
    detail: str = ""


class CatalystVeto(Protocol):
    """SEAM -- the AI catalyst veto. Deliberately not implemented here.

    The model answers exactly one question per candidate: is there an
    identifiable reason this instrument's implied volatility is elevated? It
    may only *remove* candidates, never add them, so a hallucinated catalyst
    costs an opportunity and cannot cost money.

    Two rules the implementer inherits and this module already enforces around
    them: a raised exception is treated as a veto rather than as an approval,
    and with `Cycle.veto_required` set, no veto being wired at all bars entries
    outright. The failure mode of an unavailable model is that the agent trades
    less, never that it trades unguarded.
    """

    def screen(self, *, symbol: str, ranking: VolRanking, spread: CreditSpread) -> VetoVerdict: ...


# --------------------------------------------------------------------------
# What a cycle reports
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountFacts:
    """The account as the cycle read it, with unreadable fields kept unreadable.

    `equity` is NaN rather than 0.0 when it could not be parsed. Zero is a
    number the risk engine can divide by and reason about; NaN is not, and
    `risk.evaluate` turns it into an `UNREADABLE_EQUITY` denial with a reason
    attached. A zero would instead flow through half the gates as a very small
    account and quietly size everything to nothing.
    """

    equity: float
    options_buying_power: float
    last_equity: float | None
    detail: str = ""

    @property
    def readable(self) -> bool:
        return math.isfinite(self.equity) and self.equity > 0


@dataclass(frozen=True, slots=True)
class StageRejection:
    """One candidate turned away at one stage, with the reason as recorded.

    Mirrors exactly what went into `journal.record_decision`, so the dashboard
    renders the same words the audit log holds rather than a second rendering
    of the same event that can drift from it.
    """

    stage: Stage
    symbol: str | None
    reasons: tuple[str, ...]
    detail: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SymbolFailure:
    """A per-symbol step that raised. Collected, never propagated.

    One unparseable chain must not cost us the other nineteen instruments, and
    it must certainly not cost us the exits queued behind it.
    """

    # "*" where the failure is the whole cycle's rather than one instrument's
    # -- an unreadable account, or a position list that never arrived.
    symbol: str
    stage: Stage
    reason: Failed
    detail: str


@dataclass(frozen=True, slots=True)
class Submission:
    """One order this cycle put on the wire, opening or closing.

    Recorded whether or not it succeeded: an order that failed to submit is a
    thing that happened, and the reason it failed is what a judge asks about.
    """

    action: Action
    symbol: str
    client_order_id: str
    spreads: int
    limit_price: Decimal
    status: OrderStatus
    ok: bool
    reason: str = ""
    detail: str = ""
    exit_reason: ExitReason | None = None


@dataclass(frozen=True, slots=True)
class CycleReport:
    """Everything one pass observed, refused, sent and failed to do.

    This is the dashboard's input and the audit log's summary. It carries the
    refusals as prominently as the trades, because a strategy that cannot show
    why it did not trade is indistinguishable from one that was broken.
    """

    cycle_id: str
    started_at: datetime
    trading_day: date
    observation: BookObservation | None = None
    account: AccountFacts | None = None
    recovery_gaps: tuple[RecoveryGap, ...] = ()
    regime: RegimeVerdict | None = None
    term_structure: TermStructure | None = None
    rankings: tuple[VolRanking, ...] = ()
    skips: tuple[Skipped, ...] = ()
    holds: tuple[ExitDecision, ...] = ()
    submissions: tuple[Submission, ...] = ()
    rejections: tuple[StageRejection, ...] = ()
    failures: tuple[SymbolFailure, ...] = ()
    halts: tuple[Halt, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def observed(self) -> bool:
        """Whether the book was established. Nothing opens without this."""
        return self.observation is not None

    @property
    def positions(self) -> tuple[OpenSpread, ...]:
        return () if self.observation is None else tuple(self.observation.positions)

    @property
    def opened(self) -> tuple[Submission, ...]:
        return tuple(s for s in self.submissions if s.action is Action.OPEN)

    @property
    def closed(self) -> tuple[Submission, ...]:
        return tuple(s for s in self.submissions if s.action is Action.CLOSE)

    @property
    def entries_barred(self) -> bool:
        return bool(self.halts)

    @property
    def needs_attention(self) -> bool:
        """Whether a human should look at this cycle.

        Orphans and unexplained departures mean our picture of the book
        disagrees with the broker's; a failure means a step gave up without an
        answer; a recovery gap means something we went looking for is still
        missing. All three are states in which the risk gates may be reasoning
        about something that is not there.
        """
        return bool(
            self.failures
            or self.recovery_gaps
            or (self.observation is not None and self.observation.needs_attention)
            or self.observation is None
        )


@dataclass(frozen=True, slots=True)
class ExitPass:
    """Proof that the exit stage ran. The entry stage requires one.

    This type exists for one reason: `_open_positions` cannot be called without
    it, and only `_close_positions` returns one. "Exits before entries" is
    therefore a property of the type signatures rather than of the order two
    statements happen to appear in, and it survives the next person's
    refactoring.
    """

    decided: tuple[ExitDecision, ...] = ()
    submitted: tuple[Submission, ...] = ()

    @property
    def urgent_unplaced(self) -> tuple[ExitDecision, ...]:
        """Urgent exits that produced no submission. A hazard worth naming."""
        placed = {s.symbol for s in self.submitted}
        return tuple(d for d in self.decided if d.urgent and d.spread.underlying not in placed)


@dataclass(frozen=True, slots=True)
class MarketView:
    """One cycle's read of the market: prices, ranking, and the regime verdict."""

    spots: Mapping[str, float] = field(default_factory=dict)
    contracts: Mapping[str, tuple[Contract, ...]] = field(default_factory=dict)
    rankings: tuple[VolRanking, ...] = ()
    skips: tuple[Skipped, ...] = ()
    expanding: tuple[bool, ...] = ()
    term_structure: TermStructure | None = None
    regime: RegimeVerdict = field(default_factory=RegimeVerdict)


@dataclass(frozen=True, slots=True)
class Book:
    """The observed book, plus the quotes the exit path prices against."""

    observation: BookObservation
    quotes: Mapping[str, Quote] = field(default_factory=dict)

    @property
    def positions(self) -> tuple[OpenSpread, ...]:
        return tuple(self.observation.positions)


# --------------------------------------------------------------------------
# Accumulation
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Ledger:
    """The one mutable object in this module.

    Everything else here is frozen, but a cycle accumulates across stages and
    threading eight growing tuples through eight signatures obscures the
    ordering that the signatures exist to express. It is private, it never
    escapes `run`, and it produces a frozen `CycleReport` at the end.
    """

    journal: Journal
    cycle_id: str
    started_at: datetime
    trading_day: date
    rejections: list[StageRejection] = field(default_factory=list)
    failures: list[SymbolFailure] = field(default_factory=list)
    submissions: list[Submission] = field(default_factory=list)
    halts: list[Halt] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def reject(
        self,
        stage: Stage,
        *,
        symbol: str | None,
        reasons: Sequence[str],
        detail: Sequence[str] = (),
        context: Mapping[str, object] | None = None,
        at: datetime | None = None,
    ) -> None:
        """Record a refusal in the journal and in the report, in that order.

        The journal write comes first because it is the durable one: a crash
        between the two loses a line from a dashboard, not a line from the
        audit trail.
        """
        self.journal.record_decision(
            cycle_id=self.cycle_id,
            stage=stage,
            accepted=False,
            symbol=symbol,
            reasons=list(reasons),
            detail=list(detail),
            context=context,
            at=at,
        )
        self.rejections.append(
            StageRejection(stage=stage, symbol=symbol, reasons=tuple(reasons), detail=tuple(detail))
        )

    def accept(
        self,
        stage: Stage,
        *,
        symbol: str | None,
        detail: Sequence[str] = (),
        reasons: Sequence[str] = (),
        context: Mapping[str, object] | None = None,
        at: datetime | None = None,
    ) -> None:
        """Record a candidate passing a stage, so the trail shows the passes too."""
        self.journal.record_decision(
            cycle_id=self.cycle_id,
            stage=stage,
            accepted=True,
            symbol=symbol,
            reasons=list(reasons),
            detail=list(detail),
            context=context,
            at=at,
        )

    def halt(self, halt: Halt, stage: Stage, detail: str, *, at: datetime | None = None) -> None:
        """Bar entries for this cycle, with the reason on the record."""
        if halt not in self.halts:
            self.halts.append(halt)
        self.reject(stage, symbol=None, reasons=[halt.value], detail=[detail], at=at)

    def fail(self, symbol: str, stage: Stage, reason: Failed, exc: BaseException) -> None:
        """Fence one symbol's failure. The cycle continues without it."""
        detail = f"{type(exc).__name__}: {exc}"
        self.failures.append(SymbolFailure(symbol, stage, reason, detail))
        try:
            self.reject(stage, symbol=symbol, reasons=[reason.value], detail=[detail])
        except (JournalError, ValueError) as journal_exc:  # pragma: no cover - defensive
            # A journal that cannot record the failure must not turn a fenced
            # error into an unfenced one.
            self.notes.append(f"could not journal {symbol} failure: {journal_exc}")

    def note(self, text: str) -> None:
        self.notes.append(text)

    def report(
        self,
        *,
        book: Book | None,
        account: AccountFacts | None,
        recovery: RecoveryState | None,
        market: MarketView | None,
        holds: Sequence[ExitDecision],
    ) -> CycleReport:
        return CycleReport(
            cycle_id=self.cycle_id,
            started_at=self.started_at,
            trading_day=self.trading_day,
            observation=None if book is None else book.observation,
            account=account,
            recovery_gaps=() if recovery is None else recovery.gaps,
            regime=None if market is None else market.regime,
            term_structure=None if market is None else market.term_structure,
            rankings=() if market is None else market.rankings,
            skips=() if market is None else market.skips,
            holds=tuple(holds),
            submissions=tuple(self.submissions),
            rejections=tuple(self.rejections),
            failures=tuple(self.failures),
            halts=tuple(self.halts),
            notes=tuple(self.notes),
        )


# --------------------------------------------------------------------------
# Small conversions
# --------------------------------------------------------------------------


def _as_float(value: object) -> float | None:
    """Alpaca returns numeric fields as strings in places. Parse defensively."""
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def account_facts(account: AccountView) -> AccountFacts:
    """Read the account, keeping "unreadable" distinct from "zero"."""
    equity = _as_float(account.equity)
    obp = _as_float(account.options_buying_power)
    last = _as_float(account.last_equity)
    return AccountFacts(
        equity=float("nan") if equity is None else equity,
        options_buying_power=0.0 if obp is None else obp,
        last_equity=last,
        detail=(
            ""
            if equity is not None
            else f"Equity did not parse as a number: {account.equity!r}. Refusing to trade on it."
        ),
    )


def to_open_positions(spreads: Sequence[OpenSpread]) -> tuple[OpenPosition, ...]:
    """The held book as the risk engine needs to see it.

    Every figure is a POSITION TOTAL in dollars and share equivalents, which is
    what `reassemble_spreads` guarantees and asserts. A per-spread `max_loss`
    fed into an aggregate cap understates open risk by the contract count --
    the direction that lets the book grow past its limit while reporting
    healthy.
    """
    return tuple(
        OpenPosition(
            symbol=s.underlying,
            max_loss=s.max_loss,
            unrealised_pnl=s.unrealised_pnl,
            net_delta=s.net_delta,
        )
        for s in spreads
    )


def closing_spread(spread: OpenSpread) -> CreditSpread:
    """Rebuild the `CreditSpread` shape `build_closing_order` requires.

    The builder wants a `CreditSpread` -- two `Contract`s -- and the journal
    cannot produce one, because what we store about an open position is the two
    OCC symbols and the economics, not the chain objects the spread was chosen
    from. Every field below is *parsed* from the OCC symbol rather than
    invented: root, expiry, strike and right are all in the symbol, exactly and
    unambiguously. Quote, delta and open interest are left None, which is the
    truth -- this reconstruction is for building an order, not for pricing one.

    A `build_closing_order(short_symbol, long_symbol, underlying)` overload in
    `execution.py` would make this function unnecessary (integration review
    C.6 #25).
    """
    short = parse_occ(spread.short_symbol)
    long_ = parse_occ(spread.long_symbol)
    kind = ContractType.CALL if short.is_call else ContractType.PUT
    return CreditSpread(
        short_leg=Contract(
            symbol=short.symbol,
            underlying=spread.underlying,
            expiry=short.expiry,
            strike=short.strike,
            contract_type=kind,
        ),
        long_leg=Contract(
            symbol=long_.symbol,
            underlying=spread.underlying,
            expiry=long_.expiry,
            strike=long_.strike,
            contract_type=ContractType.CALL if long_.is_call else ContractType.PUT,
        ),
        credit=spread.credit_per_spread,
        contract_type=kind,
    )


def intent_legs(order: MultiLegOrder) -> list[IntentLeg]:
    """The legs as the journal stores them.

    Required by `record_intent`, and not a formality: the leg map is the only
    thing that can answer "which spread holds this assigned contract", and an
    order journalled without legs is invisible to that query rather than merely
    unmapped.
    """
    return [
        IntentLeg(
            occ_symbol=leg.symbol,
            side=leg.side.value,
            ratio_qty=leg.ratio_qty,
            position_intent=leg.position_intent.value,
        )
        for leg in order.legs
    ]


def _no_position_taken(submission: Submission) -> bool:
    """Whether a submission definitely left the book unchanged.

    Only the two statuses that say so outright. Anything else -- an UNKNOWN
    outcome included -- counts as a position for the rest of the cycle, so the
    next candidate is sized against a book that may already contain it.
    """
    return submission.status in (OrderStatus.ABANDONED, OrderStatus.REJECTED)


def _status_of(result: OrderResult) -> OrderStatus:
    """What the journal should believe about an order we just submitted."""
    if result.status:
        return OrderStatus.from_broker(result.status)
    return OrderStatus.UNKNOWN


# --------------------------------------------------------------------------
# The cycle
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cycle:
    """One wired-up pass of the state machine. Call `run()`.

    Frozen: the wiring is fixed for the life of the object and every cycle over
    it is a fresh `CycleReport`, so a loop that runs this every few minutes
    carries no state between passes except the journal -- which is the point of
    the journal.
    """

    journal: Journal
    market: MarketSource
    broker: Broker
    execution: Executor
    clock: Clock = field(default_factory=SystemClock)
    orders: OrderReader | None = None
    # SEAM. See `CatalystVeto`. `veto_required` makes an unwired veto bar
    # entries outright, which is what the strategy spec asks for in the judged
    # run; it defaults to False so the rest of the pipeline is runnable while
    # the veto is unwritten.
    veto: CatalystVeto | None = None
    veto_required: bool = False
    limits: RiskLimits = field(default_factory=RiskLimits)
    vol_policy: VolPolicy = field(default_factory=VolPolicy)
    regime_policy: RegimePolicy = field(default_factory=RegimePolicy)
    liquidity_policy: LiquidityPolicy = field(default_factory=LiquidityPolicy)
    credit_policy: CreditPolicy = field(default_factory=CreditPolicy)
    economics: SpreadEconomics = field(default_factory=SpreadEconomics)
    exit_policy: ExitPolicy = field(default_factory=ExitPolicy)
    # A durable kill switch lives in the journal; this is the process-level one
    # from `Settings`, ORed with it. Either engaged bars entries.
    kill_switch: bool = False
    # What we pay over the conservative closing price to get an urgent exit
    # done. A limit that does not fill leaves the position for the broker to
    # sell out at whatever the book offers (GOTCHAS #10), which is the outcome
    # defined risk exists to prevent, so a deadline-driven exit crosses more of
    # the spread than a discretionary one.
    urgent_exit_markup: float = 0.10
    dry_run: bool = False
    universe: tuple[str, ...] = field(default_factory=universe_symbols)

    # -- the pass -------------------------------------------------------

    def run(self, *, preflight: PreflightReport | None = None) -> CycleReport:
        """Run one full pass and return what happened.

        The sequence below is the module's whole contract. Read it as: find out
        what we hold, write down the things that are unrecoverable if missed,
        agree with the broker about what is outstanding, read the market, close
        what needs closing, and only then consider opening anything.
        """
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            msg = f"the clock must return a timezone-aware datetime, got {now!r}"
            raise ValueError(msg)
        day = trading_day_of(now)
        ledger = _Ledger(
            journal=self.journal,
            cycle_id=cycle_id_for(now),
            started_at=now,
            trading_day=day,
        )

        book = self._observe(now, ledger)

        # Before anything can fail: the session-open equity is unrecoverable
        # after the fact, and without it `risk.evaluate` denies every entry for
        # the rest of the day on UNREADABLE_BASELINE. So it is written even on
        # a cycle whose observation failed.
        account = self._read_account(ledger)
        self._record_session_open(account, now, day, ledger)

        recovery = self._reconcile(now, book, ledger)
        market = self._read_market(now, day, ledger)

        exits = self._close_positions(book, market, now, day, ledger)
        holds = tuple(d for d in exits.decided if not d.should_exit)

        self._open_positions(
            exits,
            book=book,
            market=market,
            account=account,
            recovery=recovery,
            preflight=preflight,
            now=now,
            day=day,
            ledger=ledger,
        )

        self._snapshot_pnl(book, ledger, detail="End of cycle.")
        return ledger.report(
            book=book, account=account, recovery=recovery, market=market, holds=holds
        )

    # -- 1. observe -----------------------------------------------------

    def _observe(self, now: datetime, ledger: _Ledger) -> Book | None:
        """Fetch positions, reassemble them into spreads, and file the diff.

        Returns None when the book could not be established, which bars every
        entry. It deliberately does not fall back to "assume flat": an empty
        book is the most permissive state the risk engine can be handed, so
        guessing it after a failed read would turn a network error into
        permission to trade.
        """
        try:
            raw = list(self.broker.positions())
            snapshots = dict(self.market.option_snapshots([p.symbol for p in raw])) if raw else {}
            quotes = {
                symbol: quote
                for symbol, snapshot in snapshots.items()
                if (quote := quote_from(snapshot)) is not None
            }
            mids = {symbol: quote.mid for symbol, quote in quotes.items()}
            deltas = {symbol: delta_from(snapshot) for symbol, snapshot in snapshots.items()}

            holding, legs_of = self._opening_orders(raw)
            spreads, orphans = reassemble_spreads(
                raw,
                orders_holding=holding,
                legs_of=legs_of,
                quotes=mids,
                deltas=deltas,
            )
            observation = observe_book(self.journal, spreads, orphans, at=now)
        except Exception as exc:
            ledger.fail("*", Stage.MONITOR, Failed.OBSERVE_ERROR, exc)
            ledger.halt(
                Halt.BOOK_UNKNOWN,
                Stage.MONITOR,
                "The open book could not be established, so open risk, net delta "
                "and the position cap are all unknown. Refusing to open anything "
                "rather than reasoning about a book we cannot see.",
                at=now,
            )
            return None

        for orphan in observation.orphans:
            # An orphan is a contract we hold and cannot attribute -- most often
            # a long wing that outlived an assigned short leg. It carries real
            # risk under a name we do not know, so it is surfaced rather than
            # dropped, every cycle it persists.
            ledger.reject(
                Stage.MONITOR,
                symbol=orphan.position.symbol,
                reasons=[orphan.reason.value],
                detail=[orphan.detail],
                at=now,
            )
        for event in observation.events:
            ledger.note(
                f"{event.symbol}: {event.spreads:g} spread(s) left the book "
                f"({event.cause}, {event.evidence}). {event.detail}"
            )
        return Book(observation=observation, quotes=quotes)

    def _opening_orders(
        self, raw: Sequence[RawOptionPosition]
    ) -> tuple[dict[str, list[OrderRecord]], dict[str, tuple[IntentLeg, ...]]]:
        """The orders that actually put each held contract on the books.

        `orders_holding` returns every order with a leg on the contract, newest
        intent first -- and a closing order has legs on exactly the same two
        contracts as the opening one. So the moment an exit is journalled it
        sorts ahead of the open, wins the pairing, and the reassembled position
        comes back with `credit_per_spread` of 0.00, because a closing order
        has no opening credit. A zero credit silently disables both the profit
        target and the loss limit: neither trigger can fire against it, and the
        position reads as one we are simply holding.

        A closing order disposes of a position; it does not hold one. Filtering
        on the recorded `position_intent` is what "holding" actually means.
        """
        legs_of: dict[str, tuple[IntentLeg, ...]] = {}
        holding: dict[str, list[OrderRecord]] = {}
        for position in raw:
            openers: list[OrderRecord] = []
            for record in self.journal.orders_holding(position.symbol):
                legs = legs_of.get(record.client_order_id)
                if legs is None:
                    legs = self.journal.legs_for(record.client_order_id)
                    legs_of[record.client_order_id] = legs
                if any(leg.position_intent.endswith("_to_close") for leg in legs):
                    continue
                openers.append(record)
            holding[position.symbol] = openers
        return holding, legs_of

    # -- 2. account and the session baseline ----------------------------

    def _read_account(self, ledger: _Ledger) -> AccountFacts:
        try:
            facts = account_facts(self.broker.account())
        except Exception as exc:
            ledger.fail("*", Stage.RISK, Failed.ACCOUNT_ERROR, exc)
            return AccountFacts(
                equity=float("nan"),
                options_buying_power=0.0,
                last_equity=None,
                detail=f"Account unreadable: {exc}",
            )
        if facts.detail:
            ledger.note(facts.detail)
        return facts

    def _record_session_open(
        self, account: AccountFacts, now: datetime, day: date, ledger: _Ledger
    ) -> None:
        """Write the day's opening equity, once, at the first cycle of the session.

        The daily loss stop measures against this number and nothing else can
        reconstruct it later, so it is written as early in the cycle as it can
        be -- before the reconcile, before the market read, and regardless of
        whether the book was observed. The journal keeps the first write of the
        day and files any later, different figure as a dispute rather than
        overwriting: a baseline that drifts with P&L is a stop that never
        fires.
        """
        if self.journal.session_open_equity(day) is not None:
            return
        if not account.readable:
            ledger.note(
                f"Session-open equity for {day.isoformat()} NOT recorded: equity is "
                "unreadable. The daily loss stop has no baseline for the rest of "
                "the session and will deny every entry until one exists."
            )
            return
        kept = self.journal.record_session_open_equity(equity=account.equity, at=now)
        ledger.note(f"Session-open equity for {day.isoformat()} is {kept:,.2f}.")

    # -- 3. reconcile ---------------------------------------------------

    def _reconcile(self, now: datetime, book: Book | None, ledger: _Ledger) -> RecoveryState:
        """Settle what we and the broker disagree about, then record the pass.

        `record_reconciliation` is what makes the journal's view read as fresh.
        Without it `view_age` stays None, `VIEW_STALE` never clears and
        `RecoveryState.is_clean` is never true -- so the sweep has to be
        recorded even on the cycles where it found nothing to do.

        The pass is recorded before `recover()` reads it back, so this cycle's
        own reconciliation counts toward this cycle's recovery state.
        """
        outstanding = self.journal.unreconciled_orders()
        orders_ok = True
        detail: list[str] = []

        if self.orders is None:
            orders_ok = not outstanding
            if outstanding:
                detail.append(
                    f"{len(outstanding)} order(s) unsettled and no order reader is "
                    "wired, so they cannot be looked up by client_order_id."
                )
        else:
            for record in outstanding:
                try:
                    view = self.orders.order_status(record.client_order_id)
                    if view is None:
                        # Proven absent, and only proven absent: the reader's
                        # contract is that an incomplete lookup raises.
                        self.journal.abandon(
                            record.client_order_id,
                            detail="The broker has no order under this client_order_id. "
                            "The intent was journalled and never reached it.",
                            at=now,
                        )
                    else:
                        self.journal.mark_status(
                            record.client_order_id,
                            OrderStatus.from_broker(view.status),
                            spreads_filled=view.filled_qty,
                            net_price_per_spread=view.filled_avg_price,
                            broker_order_id=view.order_id,
                            detail=view.detail,
                            at=now,
                        )
                except Exception as exc:
                    orders_ok = False
                    ledger.fail(record.symbol, Stage.MONITOR, Failed.RECONCILE_ERROR, exc)

        ok = book is not None and orders_ok
        if book is None:
            detail.append("The position sweep failed, so our view is not confirmed.")
        self.journal.record_reconciliation(
            scope=ReconciliationScope.FULL,
            ok=ok,
            detail=" ".join(detail) if detail else "Positions observed; no order left unsettled.",
            at=now,
        )

        recovery = self.journal.recover(now=now)
        for line in recovery.detail:
            ledger.note(line)
        return recovery

    # -- 4. scan, rank, regime ------------------------------------------

    def _read_market(self, now: datetime, day: date, ledger: _Ledger) -> MarketView:
        """Price the universe, rank it, and judge the regime.

        Ranking is done per instrument rather than through `rank_universe`,
        because that helper returns below-floor instruments as `Skipped` and so
        discards their `realised_is_expanding` flag. The regime's expansion
        check would then be measuring only the instruments rich enough to
        trade -- a biased subset of exactly the wrong shape, since a market-wide
        volatility expansion is what pushes instruments above the floor in the
        first place.
        """
        window = ExpiryWindow.from_dte(
            day, self.limits.min_days_to_expiry, self.limits.max_days_to_expiry
        )
        try:
            bars = self.market.daily_closes([BENCHMARK, *self.universe])
        except Exception as exc:
            ledger.fail(BENCHMARK, Stage.SCAN, Failed.SCAN_ERROR, exc)
            return MarketView(
                regime=RegimeVerdict(
                    blocks=(
                        Blocked(
                            RegimeBlock.BENCHMARK_HISTORY_MISSING,
                            f"Daily bars are unavailable ({exc}), so the regime cannot "
                            "be judged. Entries blocked; open positions are unaffected, "
                            "because a missing read is not a reason to liquidate.",
                        ),
                    )
                )
            )

        spots: dict[str, float] = {}
        contracts: dict[str, tuple[Contract, ...]] = {}
        chains: dict[str, Mapping[str, SnapshotLike]] = {}
        rankings: list[VolRanking] = []
        skips: list[Skipped] = []
        expanding: list[bool] = []

        for symbol in self.universe:
            try:
                skip = self._rank_symbol(
                    symbol,
                    bars=bars,
                    window=window,
                    spots=spots,
                    chains=chains,
                    contracts=contracts,
                    rankings=rankings,
                    expanding=expanding,
                )
            except Exception as exc:
                ledger.fail(symbol, Stage.SCAN, Failed.SCAN_ERROR, exc)
                continue
            if skip is not None:
                skips.append(skip)
                # The measured figures go into the decision as data, not only
                # into its prose. A dashboard that had to parse "Premium ratio
                # 1.21 is below the 1.30 floor" back into a float would depend
                # on a sentence nobody maintains as a format.
                ledger.reject(
                    Stage.RANK,
                    symbol=symbol,
                    reasons=[skip.reason.value],
                    detail=[skip.detail],
                    context=_ranking_context(skip.ranking),
                    at=now,
                )

        rankings.sort(key=lambda r: r.vrp_ratio, reverse=True)
        term = self._term_structure(bars, chains, day, ledger)
        verdict = evaluate_regime(
            benchmark_closes=bars.for_symbol(BENCHMARK),
            expanding_flags=expanding,
            term_structure=term,
            today=day,
            policy=self.regime_policy,
        )
        # Recorded whether or not it blocked. The filter is judged on whether
        # it fired at the right times, which is unanswerable if only its
        # refusals are on disk.
        self.journal.record_regime_verdict(
            allowed=verdict.may_open,
            blocks=[b.reason.value for b in verdict.blocks],
            detail=[b.detail for b in verdict.blocks],
            context={
                "cycle_id": ledger.cycle_id,
                "benchmark_closes": len(bars.for_symbol(BENCHMARK)),
                "expansion_sampled": len(expanding),
                "expanding": sum(expanding),
                "candidates": len(rankings),
            },
            at=now,
        )
        return MarketView(
            spots=spots,
            contracts=contracts,
            rankings=tuple(rankings),
            skips=tuple(skips),
            expanding=tuple(expanding),
            term_structure=term,
            regime=verdict,
        )

    def _rank_symbol(
        self,
        symbol: str,
        *,
        bars: Bars,
        window: ExpiryWindow,
        spots: dict[str, float],
        chains: dict[str, Mapping[str, SnapshotLike]],
        contracts: dict[str, tuple[Contract, ...]],
        rankings: list[VolRanking],
        expanding: list[bool],
    ) -> Skipped | None:
        """Rank one instrument, or say why it produced no ranking.

        The spot price is the last completed daily close, which is roughly
        twenty minutes stale by construction (`data.SIP_EMBARGO`, GOTCHAS #5).
        That is a stated approximation rather than an oversight: it is used for
        a moneyness band a few percent wide and for the term-structure strike
        choice, and on both a twenty-minute-old price is well inside the
        tolerance. It is never used to price an order.
        """
        closes = bars.for_symbol(symbol)
        if not closes:
            return Skipped(symbol, Skip.INSUFFICIENT_HISTORY, "No daily bars returned.")
        spot = closes[-1]
        if spot <= 0:
            return Skipped(symbol, Skip.NON_POSITIVE_PRICE, f"Last close is {spot!r}.")
        spots[symbol] = spot

        chain = self.market.chain(symbol, window)
        chains[symbol] = chain
        atm = atm_implied_vol(chain, underlying_price=spot)
        ranked = rank_instrument(
            symbol,
            closes=closes,
            implied_vol=None if atm is None else atm[0],
            policy=self.vol_policy,
        )
        if isinstance(ranked, Skipped):
            return ranked

        # Sampled across every instrument that produced a ranking, floor or no
        # floor. See `_read_market` for why the floor must not filter this.
        expanding.append(ranked.realised_is_expanding)
        if ranked.vrp_ratio < self.vol_policy.min_vrp_ratio:
            return Skipped(
                symbol,
                Skip.PREMIUM_BELOW_FLOOR,
                f"Premium ratio {ranked.vrp_ratio:.2f} is below the "
                f"{self.vol_policy.min_vrp_ratio:.2f} floor "
                f"(IV {ranked.implied_vol:.1%} vs RV {ranked.realised_vol:.1%}).",
            )

        contracts[symbol] = tuple(contracts_from_chain(chain, underlying=symbol))
        rankings.append(ranked)
        return None

    def _term_structure(
        self,
        bars: Bars,
        chains: Mapping[str, Mapping[str, SnapshotLike]],
        day: date,
        ledger: _Ledger,
    ) -> TermStructure | None:
        """The benchmark's near/far implied volatility curve, or None.

        None blocks entries -- the curve is the only forward-looking input the
        regime filter has, and the spec says a missing one is a block rather
        than a shrug. The near slice is the same chain the scan already fetched
        for the benchmark, so this costs one extra call, not two.
        """
        closes = bars.for_symbol(BENCHMARK)
        if not closes:
            return None
        spot = closes[-1]
        near = chains.get(BENCHMARK)
        try:
            if near is None:
                near = self.market.chain(
                    BENCHMARK,
                    ExpiryWindow.from_dte(
                        day, self.limits.min_days_to_expiry, self.limits.max_days_to_expiry
                    ),
                )
            far = self.market.chain(
                BENCHMARK, ExpiryWindow.from_dte(day, FAR_EXPIRY_MIN_DTE, FAR_EXPIRY_MAX_DTE)
            )
        except Exception as exc:
            ledger.fail(BENCHMARK, Stage.REGIME, Failed.SCAN_ERROR, exc)
            return None
        return term_structure_from(near, far, underlying_price=spot)

    # -- 5. exits, before any entry -------------------------------------

    def _close_positions(
        self,
        book: Book | None,
        market: MarketView,
        now: datetime,
        day: date,
        ledger: _Ledger,
    ) -> ExitPass:
        """Decide every open position and place the closes, urgent ones first.

        `decide_exits` has already ordered the queue so the deadline-driven
        exits go out before the discretionary ones; that matters on a cycle
        where the book is larger than the number of orders we get to place.

        Legs are priced conservatively and asymmetrically, because closing is
        asymmetric: we buy the short leg back at the ask and sell the long wing
        at the bid. Pricing both at mid would say the exit is cheaper than it
        is, which is the direction that holds a losing position.
        """
        if book is None:
            return ExitPass()

        quotes: dict[str, float | None] = {}
        for spread in book.positions:
            short = book.quotes.get(spread.short_symbol)
            long_ = book.quotes.get(spread.long_symbol)
            quotes[spread.short_symbol] = None if short is None else short.ask
            quotes[spread.long_symbol] = None if long_ is None else long_.bid

        decisions = decide_exits(
            list(book.positions),
            quotes=quotes,
            regime=market.regime,
            today=day,
            now_et=session_time_et(now),
            limits=self.limits,
            policy=self.exit_policy,
        )

        submitted: list[Submission] = []
        for decision in decisions:
            if not decision.should_exit:
                continue
            try:
                sub = self._submit_exit(decision, quotes, now, ledger)
            except Exception as exc:
                ledger.fail(decision.spread.underlying, Stage.EXIT, Failed.EXIT_ERROR, exc)
                continue
            if sub is not None:
                submitted.append(sub)
                if sub.ok:
                    # A closing fill realises P&L, and `_realised_today` reads
                    # as unknown until a snapshot is dated after the day's last
                    # fill. Taken on every accepted submission rather than only
                    # on the ones we can see filled: a superset costs one row,
                    # and the subset that misses a fill costs the daily loss
                    # stop its input for the rest of the session.
                    self._snapshot_pnl(book, ledger, detail=f"After closing {sub.symbol}.")

        return ExitPass(decided=tuple(decisions), submitted=tuple(submitted))

    def _submit_exit(
        self,
        decision: ExitDecision,
        quotes: Mapping[str, float | None],
        now: datetime,
        ledger: _Ledger,
    ) -> Submission | None:
        """Journal and place one closing order."""
        spread = decision.spread
        reason = decision.reason
        assert reason is not None  # guarded by the caller; kept for the type

        if self._exit_already_working(spread):
            ledger.reject(
                Stage.EXIT,
                symbol=spread.underlying,
                reasons=[Refusal.EXIT_ALREADY_WORKING.value],
                detail=[
                    f"A closing order for {spread.underlying} is already unsettled. "
                    "Re-placing it would double the exit."
                ],
                at=now,
            )
            return None

        contracts = int(spread.spreads)
        if contracts < 1:
            ledger.reject(
                Stage.EXIT,
                symbol=spread.underlying,
                reasons=[Refusal.NO_WHOLE_SPREAD.value],
                detail=[f"{spread.spreads:g} spread(s) held; nothing whole to close."],
                at=now,
            )
            return None

        base = closing_debit(spread, quotes)
        if base is None:
            # We cannot price the exit, and an unpriced limit is either a gift
            # or an order that never fills. The trigger stands and is retried
            # next cycle with fresh quotes.
            ledger.reject(
                Stage.EXIT,
                symbol=spread.underlying,
                reasons=[Refusal.EXIT_UNPRICEABLE.value, reason.value],
                detail=[
                    "A leg has no quote, so the closing debit cannot be priced. "
                    f"The {reason.value} trigger stands and will be retried."
                ],
                at=now,
            )
            return None

        # Cross more of the spread when the exit is a deadline rather than a
        # preference. A `day` limit that does not fill inside the flatten
        # window hands the price to the broker (GOTCHAS #10).
        debit = base * (1 + self.urgent_exit_markup) if decision.urgent else base
        order = build_closing_order(
            closing_spread(spread), contracts=contracts, debit=max(debit, 0.01), now=now
        )
        ledger.accept(
            Stage.EXIT,
            symbol=spread.underlying,
            reasons=[reason.value],
            detail=[decision.detail],
            at=now,
        )
        return self._journal_then_submit(
            order,
            action=Action.CLOSE,
            symbol=spread.underlying,
            contracts=contracts,
            now=now,
            ledger=ledger,
            exit_reason=reason,
        )

    def _exit_already_working(self, spread: OpenSpread) -> bool:
        """Whether an unsettled closing order for this underlying already exists.

        Read from the recorded legs rather than from the client order id: the
        id encodes the action, but parsing an identifier for meaning is how a
        format change becomes a double exit. `position_intent` is what we
        actually stored, and it is unambiguous.
        """
        for record in self.journal.unreconciled_orders():
            if record.symbol != spread.underlying:
                continue
            legs = self.journal.legs_for(record.client_order_id)
            if any(leg.position_intent.endswith("_to_close") for leg in legs):
                return True
        return False

    # -- 6. entries -----------------------------------------------------

    def _open_positions(
        self,
        exits: ExitPass,
        *,
        book: Book | None,
        market: MarketView,
        account: AccountFacts,
        recovery: RecoveryState,
        preflight: PreflightReport | None,
        now: datetime,
        day: date,
        ledger: _Ledger,
    ) -> None:
        """Consider opening new positions. Requires proof the exits already ran.

        `exits` is not read for its contents so much as for its existence: it
        is the token that makes "exits before entries" a property of the type
        signature. What it *is* read for is the hazard case -- an urgent exit
        that could not be placed means the book is carrying risk we tried and
        failed to shed, and adding to it in the same cycle would be perverse.
        """
        gates = self._entry_gates(
            book=book,
            market=market,
            account=account,
            preflight=preflight,
            exits=exits,
            now=now,
            ledger=ledger,
        )
        if gates or book is None:
            return

        if not market.rankings:
            ledger.halt(
                Halt.NO_CANDIDATES,
                Stage.RANK,
                f"No instrument cleared the {self.vol_policy.min_vrp_ratio:.2f} "
                f"premium floor out of {len(self.universe)} scanned.",
                at=now,
            )
            return

        # The book grows within the cycle. Without this, six candidates each
        # measured against the same empty account would each pass the position
        # cap and the aggregate risk cap, and the caps would be breached in one
        # pass while every individual decision looked compliant.
        held = list(to_open_positions(book.positions))
        buying_power = account.options_buying_power

        for ranking in market.rankings:
            try:
                opened = self._open_one(
                    ranking,
                    market=market,
                    account=account,
                    recovery=recovery,
                    held=held,
                    buying_power=buying_power,
                    now=now,
                    day=day,
                    ledger=ledger,
                )
            except Exception as exc:
                ledger.fail(ranking.symbol, Stage.EXECUTE, Failed.ENTRY_ERROR, exc)
                continue
            if opened is not None:
                held.append(opened[0])
                buying_power -= opened[1]
                # As on the exit path: dated after anything that may have
                # filled, because a stale realised figure understates the loss
                # the daily stop is measuring.
                self._snapshot_pnl(book, ledger, detail=f"After opening {ranking.symbol}.")

    def _entry_gates(
        self,
        *,
        book: Book | None,
        market: MarketView,
        account: AccountFacts,
        preflight: PreflightReport | None,
        exits: ExitPass,
        now: datetime,
        ledger: _Ledger,
    ) -> tuple[Halt, ...]:
        """Every cycle-wide reason not to open anything, all of them recorded.

        Accumulated rather than short-circuited, for the same reason
        `risk.evaluate` accumulates: an operator asking why the agent stood
        down deserves all the answers, not the first one it happened to hit.
        """
        halts: list[Halt] = []

        if book is None:
            # Already recorded by `_observe`, which is where the failure is
            # legible. Reported here so the caller sees the gate.
            halts.append(Halt.BOOK_UNKNOWN)

        if preflight is None:
            halts.append(Halt.PREFLIGHT_MISSING)
            ledger.halt(
                Halt.PREFLIGHT_MISSING,
                Stage.RISK,
                "No preflight report was supplied. Trading without one means "
                "trading without having checked the account, the options level "
                "or the paper-only guarantee.",
                at=now,
            )
        elif not preflight.may_trade:
            halts.append(Halt.PREFLIGHT_FAILED)
            ledger.halt(
                Halt.PREFLIGHT_FAILED,
                Stage.RISK,
                "Preflight failed: "
                + "; ".join(f"{c.name} ({c.detail})" for c in preflight.failures),
                at=now,
            )

        if not account.readable:
            halts.append(Halt.ACCOUNT_UNREADABLE)
            ledger.halt(
                Halt.ACCOUNT_UNREADABLE,
                Stage.RISK,
                account.detail or "Equity could not be read, so nothing can be sized.",
                at=now,
            )

        switch = self.journal.kill_switch()
        if switch.engaged or self.kill_switch:
            halts.append(Halt.KILL_SWITCH)
            ledger.halt(
                Halt.KILL_SWITCH,
                Stage.RISK,
                (
                    f"Kill switch engaged by {switch.actor}: {switch.reason}"
                    if switch.engaged
                    else "Kill switch engaged in process configuration."
                )
                + " No new entries. Exits are unaffected: a stop on opening is "
                "not a stop on managing what we already hold.",
                at=now,
            )

        if not market.regime.may_open:
            halts.append(Halt.REGIME_BLOCKED)
            ledger.halt(
                Halt.REGIME_BLOCKED,
                Stage.REGIME,
                "; ".join(b.detail for b in market.regime.blocks),
                at=now,
            )

        if self.veto is None and self.veto_required:
            halts.append(Halt.VETO_UNAVAILABLE)
            ledger.halt(
                Halt.VETO_UNAVAILABLE,
                Stage.VETO,
                "The catalyst veto is required and not wired. A missing veto is "
                "a veto, never an approval.",
                at=now,
            )

        unplaced = exits.urgent_unplaced
        if unplaced:
            # A cycle that decided to shed risk and could not has no business
            # adding more. The one gate that exists because of the exits rather
            # than in spite of them.
            halts.append(Halt.URGENT_EXIT_UNPLACED)
            ledger.halt(
                Halt.URGENT_EXIT_UNPLACED,
                Stage.RISK,
                "Urgent exit(s) decided and not placed for "
                + ", ".join(sorted({d.spread.underlying for d in unplaced}))
                + ". Not adding to a book we just failed to reduce.",
                at=now,
            )

        return tuple(halts)

    def _open_one(
        self,
        ranking: VolRanking,
        *,
        market: MarketView,
        account: AccountFacts,
        recovery: RecoveryState,
        held: Sequence[OpenPosition],
        buying_power: float,
        now: datetime,
        day: date,
        ledger: _Ledger,
    ) -> tuple[OpenPosition, float] | None:
        """Select, veto, size, journal and submit one entry.

        Returns the position we must now assume we carry and the risk it
        consumes, or None if nothing was sent. "Assume we carry" is deliberate:
        a submission whose outcome we could not read is treated as a position
        for the rest of the cycle, because the alternative is to size the next
        candidate as though this one does not exist.
        """
        symbol = ranking.symbol

        # The entry gate that stops a crash from becoming a double position. An
        # unsettled order on this symbol may be live at the broker; opening a
        # second one would be two positions where the journal shows one.
        if any(o.symbol == symbol for o in recovery.unreconciled_orders):
            ledger.reject(
                Stage.EXECUTE,
                symbol=symbol,
                reasons=[Refusal.UNRECONCILED_ORDER.value],
                detail=[
                    f"An order on {symbol} is unsettled. Until the broker has been "
                    "asked what happened to it, a second one could double the position."
                ],
                at=now,
            )
            return None

        contracts_available = market.contracts.get(symbol, ())
        window = ExpiryWindow.from_dte(
            day, self.limits.min_days_to_expiry, self.limits.max_days_to_expiry
        )
        # Wire the per-trade risk budget into selection. Without it the selector
        # happily returns a structure whose max loss exceeds the budget, sizing
        # floors to zero, and the agent stops trading with nothing on the record.
        credit_policy = replace(
            self.credit_policy,
            max_loss_per_contract=max_risk_dollars(account.equity, self.limits),
        )
        spread, rejection, _screened = select_credit_vertical(
            contracts_available,
            now=now,
            window=window,
            contract_type=ContractType.PUT,
            underlying_price=market.spots.get(symbol),
            policy=self.liquidity_policy,
            credit_policy=credit_policy,
            economics=self.economics,
        )
        if spread is None or rejection is not None:
            ledger.reject(
                Stage.SCAN,
                symbol=symbol,
                reasons=[
                    rejection.value if rejection is not None else Refusal.NO_SPREAD_AVAILABLE.value
                ],
                detail=[
                    f"No put credit spread could be built from {len(contracts_available)} "
                    f"contract(s) in the {window.gte.isoformat()}..{window.lte.isoformat()} "
                    "window."
                ],
                at=now,
            )
            return None

        if not self._passes_veto(symbol, ranking, spread, now, ledger):
            return None

        decision = evaluate_risk(
            symbol=symbol,
            max_loss_per_contract=spread.max_loss,
            account=AccountState(
                equity=account.equity,
                options_buying_power=buying_power,
                # Both are passed through exactly as the journal reports them.
                # An `or 0.0` here would disarm the daily loss stop on the days
                # we can see least, which is when it matters most.
                starting_equity=recovery.session_open_equity,
                realised_pnl_today=recovery.realised_pnl_today,
                open_positions=tuple(held),
            ),
            limits=self.limits,
            now_et=session_time_et(now),
            kill_switch=False,  # already a cycle-wide halt; never reached engaged
            net_delta_per_contract=spread.net_delta_per_spread,
        )
        if not decision.allowed:
            ledger.reject(
                Stage.RISK,
                symbol=symbol,
                reasons=[d.value for d in decision.denials],
                detail=list(decision.detail),
                at=now,
            )
            return None

        ledger.accept(
            Stage.RISK,
            symbol=symbol,
            context=_ranking_context(ranking),
            detail=[
                f"{decision.contracts} spread(s) at {spread.credit:.2f} credit, "
                f"{spread.max_loss:,.2f} risk each, "
                f"premium ratio {ranking.vrp_ratio:.2f}."
            ],
            at=now,
        )
        order = build_opening_order(spread, contracts=decision.contracts, now=now)
        submission = self._journal_then_submit(
            order,
            action=Action.OPEN,
            symbol=symbol,
            contracts=decision.contracts,
            now=now,
            ledger=ledger,
        )
        if submission is None or _no_position_taken(submission):
            return None

        risk_taken = decision.contracts * spread.max_loss
        delta = spread.net_delta_per_spread or 0.0
        return (
            OpenPosition(
                symbol=symbol,
                max_loss=risk_taken,
                unrealised_pnl=0.0,
                net_delta=delta * decision.contracts,
            ),
            risk_taken,
        )

    def _passes_veto(
        self,
        symbol: str,
        ranking: VolRanking,
        spread: CreditSpread,
        now: datetime,
        ledger: _Ledger,
    ) -> bool:
        """The catalyst veto seam. See `CatalystVeto`.

        With no veto wired the candidate passes and the absence is recorded, so
        the audit log shows which cycles ran unscreened rather than implying a
        screen that never happened. `veto_required` turns that absence into a
        cycle-wide halt instead.
        """
        if self.veto is None:
            ledger.accept(
                Stage.VETO,
                symbol=symbol,
                detail=["No catalyst veto is wired; the candidate was not screened."],
                at=now,
            )
            return True
        try:
            verdict = self.veto.screen(symbol=symbol, ranking=ranking, spread=spread)
        except Exception as exc:
            verdict = VetoVerdict(
                vetoed=True,
                detail=f"The catalyst veto could not be reached ({exc}); "
                "an unavailable model is a veto, not an approval.",
            )
        if not verdict.vetoed:
            ledger.accept(
                Stage.VETO,
                symbol=symbol,
                detail=[verdict.detail or "No identifiable catalyst."],
                at=now,
            )
            return True
        ledger.reject(
            Stage.VETO,
            symbol=symbol,
            reasons=[Refusal.VETO_CATALYST.value],
            detail=[verdict.catalyst or verdict.detail or "Catalyst found."],
            at=now,
        )
        return False

    # -- the write-ahead pair -------------------------------------------

    def _journal_then_submit(
        self,
        order: MultiLegOrder,
        *,
        action: Action,
        symbol: str,
        contracts: int,
        now: datetime,
        ledger: _Ledger,
        exit_reason: ExitReason | None = None,
    ) -> Submission:
        """Write the intent, then send it. Nothing may reorder these two.

        The journal write is committed before the POST so that a crash in
        between leaves an intent we can chase by `client_order_id` -- the one
        identifier that survives on both sides. The opposite order leaves a
        window in which the broker holds a live order we have no record of, and
        no later reconciliation can close it, because reconciliation needs
        something to reconcile by.

        `symbol` is carried explicitly because the mleg payload has no
        top-level symbol: for a multi-leg order it lives on the legs
        (GOTCHAS #6), and the journal indexes by underlying.
        """
        self.journal.record_intent(
            client_order_id=order.client_order_id,
            cycle_id=ledger.cycle_id,
            symbol=symbol,
            spreads=float(order.qty),
            payload=order.as_payload(),
            legs=intent_legs(order),
            at=now,
        )

        result = self.execution.submit(order, dry_run=self.dry_run)
        status = self._settle(order, result, ledger)

        submission = Submission(
            action=action,
            symbol=symbol,
            client_order_id=order.client_order_id,
            spreads=contracts,
            limit_price=order.limit_price,
            status=status,
            ok=result.ok,
            reason="" if result.reason is None else result.reason.value,
            detail=result.message,
            exit_reason=exit_reason,
        )
        ledger.submissions.append(submission)
        if not result.ok:
            ledger.reject(
                Stage.EXECUTE,
                symbol=symbol,
                reasons=[submission.reason or "submit_failed"],
                detail=[result.message or "The submission did not succeed."],
                at=now,
            )
        return submission

    def _settle(self, order: MultiLegOrder, result: OrderResult, ledger: _Ledger) -> OrderStatus:
        """Record what the broker said, or that it said nothing legible.

        `abandon` is used only where absence was OBSERVED rather than
        assumed -- a dry run makes no HTTP call at all (GOTCHAS #15), and
        `OrderResult.proven_absent` is set only by a terminal backend outcome
        or a lookup that positively returned ABSENT. Everything else that did
        not clearly succeed is left UNKNOWN, which is non-terminal, which keeps
        the order unreconciled and the symbol blocked until somebody asks the
        broker.
        """
        if result.dry_run:
            self.journal.abandon(
                order.client_order_id,
                detail="Dry run: the payload was built and nothing was sent.",
                at=result.at,
            )
            return OrderStatus.ABANDONED
        if not result.ok and _never_reached_broker(result):
            self.journal.abandon(
                order.client_order_id,
                detail=f"Never reached the broker ({result.reason}): {result.message}",
                at=result.at,
            )
            return OrderStatus.ABANDONED

        status = _status_of(result)
        if not result.ok and result.reason is Reason.REJECTED:
            status = OrderStatus.REJECTED
        try:
            self.journal.mark_status(
                order.client_order_id,
                status,
                spreads_filled=None if result.filled_qty is None else float(result.filled_qty),
                net_price_per_spread=(
                    None if result.filled_avg_price is None else float(result.filled_avg_price)
                ),
                broker_order_id=result.order_id,
                detail=result.message,
                at=result.at,
            )
        except JournalError as exc:
            # The journal and the broker disagree about a settled order. That
            # is a fact worth keeping, not an exception worth crashing on.
            ledger.note(f"{order.client_order_id}: {exc}")
        return status

    # -- 7. review ------------------------------------------------------

    def _snapshot_pnl(self, book: Book | None, ledger: _Ledger, *, detail: str) -> None:
        """Record an official P&L reading, dated now.

        Called after every submission that reached the broker -- a superset of
        the fills -- and once more at the end of the cycle, because
        `journal._realised_today` returns None -- and the daily loss stop then
        denies on UNREADABLE_PNL -- unless a snapshot is dated *after* the day's
        last fill. A snapshot that predates a fill is stale, and a stale
        realised figure understates the loss the stop is measuring.

        The account is re-read rather than reused so the equity genuinely
        postdates the fill. Realised is the broker's own day P&L less the
        mark-to-market on what is still open; those marks are ours, from
        indicative quotes (GOTCHAS #3), so this figure inherits their
        uncertainty -- which is exactly why the conservative shadow series
        exists beside it.
        """
        try:
            facts = account_facts(self.broker.account())
        except Exception as exc:
            ledger.note(f"No P&L snapshot: the account could not be re-read ({exc}).")
            return
        if not facts.readable or facts.last_equity is None:
            ledger.note(
                "No P&L snapshot: equity or last_equity is unreadable, so today's "
                "realised figure would be a guess. Recovery will report the gap."
            )
            return
        unrealised = sum(p.unrealised_pnl for p in (book.positions if book else ()))
        now = self.clock.now()
        self.journal.record_pnl(
            source=PnlSource.OFFICIAL,
            realised_pnl=(facts.equity - facts.last_equity) - unrealised,
            unrealised_pnl=unrealised,
            equity=facts.equity,
            detail=f"{detail} Day P&L from broker equity less open mark-to-market.",
            at=now,
        )
        self._snapshot_shadow_pnl(unrealised, ledger, at=now)

    def _snapshot_shadow_pnl(self, unrealised: float, ledger: _Ledger, *, at: datetime) -> None:
        """Record the conservative series beside the official one.

        Recomputed from our own fills rather than from broker equity: every
        fill is repriced at the worse of what we got and what we asked for,
        then charged an execution haircut the paper engine does not model.

        It is written every time the official figure is, so the two series
        always cover the same moments. A shadow series with gaps where the
        official one has readings would invite a reader to compare a partial
        number against a complete one.

        Unrealised is carried across unchanged. Both series mark open positions
        the same way -- from indicative quotes -- so pretending the shadow mark
        is independently derived would overstate what this series knows.
        """
        try:
            shadow = shadow_for_day(self.journal, trading_day_of(at))
        except Exception as exc:
            ledger.note(f"No shadow P&L snapshot: {type(exc).__name__}: {exc}")
            return
        note = (
            f"Fills repriced at the worse of fill and limit, less "
            f"${shadow.haircut_usd:,.2f} of modelled execution cost."
        )
        if not shadow.complete:
            note += (
                f" {shadow.unpriced_fills} fill(s) had no limit on record, so this "
                "figure is LESS conservative than it claims."
            )
        self.journal.record_pnl(
            source=PnlSource.SHADOW,
            realised_pnl=shadow.realised_usd,
            unrealised_pnl=unrealised,
            detail=note,
            at=at,
        )
