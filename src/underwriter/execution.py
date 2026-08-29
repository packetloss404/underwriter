"""Order execution: the only code in this project that can create an order.

Four properties matter more than anything else here, and each one is enforced
structurally rather than by convention.

**It cannot reach live trading.** There is no branch, flag, or argument that
selects a live endpoint. `assert_paper_only` runs before every submission and
raises rather than returning False, and the environment handed to the CLI
always carries `ALPACA_LIVE_TRADE=false` regardless of what the parent process
had. A live route is not disabled here; it does not exist.

**It never duplicates a spread.** A duplicate credit spread doubles real risk
silently -- the position simply looks twice as big, with nothing in the logs
saying why. So an ambiguous outcome (a timeout, an unreadable response, a 5xx,
an unrecognised CLI error) is *never* retried blindly. The adapter looks the
order up by `client_order_id` first and resubmits only when the broker has
positively confirmed the order does not exist. If absence cannot be proven, the
adapter gives up and says so. Losing an order is recoverable; duplicating one
is not.

That lookup is load-bearing rather than merely careful. Duplicate
`client_order_id` handling on POST /v2/orders is undocumented and Alpaca
publishes real idempotency only on other endpoints (docs/GOTCHAS.md #9), so the
broker cannot be assumed to de-duplicate anything; and the CLI binary carries
retry machinery whose behaviour on a POST could not be verified. `_AbsenceProof`
therefore encodes the rule in the type system: the resubmission path cannot be
called without evidence a lookup produced.

**Our validation is the only validation.** `--dry-run` is a local JSON
pretty-printer -- no HTTP call, and exit 0 for payloads Alpaca rejects
outright, including a missing `--type`, a stray `--symbol`, five legs, or no
legs at all. It is a way to show a human the payload and nothing more.
`validate()` runs before every submission, dry or real.

**Every failure carries a displayable reason.** Same rule as the risk engine: a
silent failure is indistinguishable from a broken adapter.

**Two backends, one interface.** The Alpaca CLI is primary -- the hackathon
requires the MCP server or the CLI, the MCP server's multi-leg serialization is
broken (docs/GOTCHAS.md #12), and the CLI's `--legs` is verified working. But the
CLI is stamped *"Alpha Preview ... Do not depend on current behavior"*, so an
alpaca-py fallback exists behind the same interface. Which one ran is recorded
on every result. The fallback engages only where it is provably duplicate-safe:
when the CLI cannot be executed at all, or when a lookup has proven no order
was created.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from hashlib import blake2s
from typing import Final, Protocol

from underwriter.chain import CreditSpread
from underwriter.config import PAPER_TRADING_HOST, LiveTradingBlocked

# --------------------------------------------------------------------------
# Invariants of the multi-leg options order, all verified against the OpenAPI
# spec. See docs/research/revalidation-alpaca-2026-08-28.md sections 6 and 7.
# --------------------------------------------------------------------------

ORDER_CLASS: Final = "mleg"
# Never "market". The default for `alpaca order submit` is market, and a market
# order on a spread is the exact thing the strategy spec forbids.
# See docs/GOTCHAS.md #6.
ORDER_TYPE: Final = "limit"
# `day` and `gtc` are both valid for options (docs/GOTCHAS.md #11), but every
# order this module builds is `day`: nothing should rest overnight unmonitored,
# and a resting spread is one nobody is watching when the gap happens.
TIME_IN_FORCE: Final = "day"
VALID_TIME_IN_FORCE: Final = frozenset({"day", "gtc"})

MIN_LEGS: Final = 2
MAX_LEGS: Final = 4
MAX_CLIENT_ORDER_ID_CHARS: Final = 128
# Conservative: the CLI's own default HTTP timeout is 30s, and it retries 429
# and 5xx up to three times, so the wall clock for one submit can exceed a
# single request. This bounds the whole subprocess.
DEFAULT_TIMEOUT_SECONDS: Final = 45.0
# One resubmission, and only after the broker has confirmed absence.
DEFAULT_MAX_SUBMIT_ATTEMPTS: Final = 2

# The status reported for a successful dry run. Named to be unmistakable
# wherever it surfaces: the CLI parsed our flags, and nothing more than that
# was established. See CliBackend._interpret_dry_run.
DRY_RUN_STATUS: Final = "dry_run_unvalidated"

LIVE_TRADE_ENV_VAR: Final = "ALPACA_LIVE_TRADE"
OUTPUT_ENV_VAR: Final = "ALPACA_OUTPUT"
# Values the Alpaca CLI itself treats as "not live". Anything outside this set
# -- including a typo -- blocks, because an unrecognised value is exactly the
# case where we cannot say what the CLI would do.
_UNAMBIGUOUSLY_NOT_LIVE: Final = frozenset({"", "false", "0", "no", "off"})

_CLIENT_ORDER_ID_RE: Final = re.compile(r"^[A-Za-z0-9._:-]+$")
_TICKER_RE: Final = re.compile(r"[^A-Z0-9]")


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class PositionIntent(StrEnum):
    """Verbatim from the OAS `PositionIntent` enum.

    Schema-optional on a leg, but the options docs treat it as required for
    multi-leg. Always sent: without it the broker infers intent from the
    account's existing position, which is how a closing order silently opens a
    new one.
    """

    BUY_TO_OPEN = "buy_to_open"
    BUY_TO_CLOSE = "buy_to_close"
    SELL_TO_OPEN = "sell_to_open"
    SELL_TO_CLOSE = "sell_to_close"


class Backend(StrEnum):
    """Which transport actually reached the broker. Recorded on every result."""

    CLI = "cli"
    SDK = "sdk"


class Reason(StrEnum):
    """Why a submission did not succeed. Displayed verbatim."""

    LIVE_TRADING_BLOCKED = "live_trading_blocked"
    INVALID_PAYLOAD = "invalid_payload"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    AUTH = "auth"
    API_ERROR = "api_error"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    DUPLICATE = "duplicate"
    MALFORMED_RESPONSE = "malformed_response"
    # The most important one: we tried, we could not read the outcome, and we
    # could not prove the order is absent. Nothing was retried.
    UNKNOWN_OUTCOME = "unknown_outcome"


# The sign convention for a multi-leg `limit_price`. This is a fact, not a
# setting, and there is deliberately no way to configure it.
#
# Verbatim from the OAS `CreateOrderRequest.limit_price` description:
#
#     In case of `mleg`, the limit_price parameter is expressed with the
#     following notation:
#     - A positive value indicates a debit, representing a cost or payment to
#       be made.
#     - A negative value signifies a credit, reflecting an amount to be
#       received.
#
# Corroborated twice over: a filled mleg order's parent `filled_avg_price`
# equals the signed net of its legs, and the Level 3 cost-basis worked example
# states that a $5 credit "becomes -$5 in the order's net debit/credit
# calculation". The all-positive examples on the Level 3 page are not
# counter-evidence -- every one of them is a debit structure or a roll, so none
# exercises the credit case.
#
# An opening credit spread therefore submits a NEGATIVE limit price, and
# closing it flips POSITIVE because buying the spread back costs money.
# See docs/GOTCHAS.md #7. Getting this backwards does not error: a positive
# limit on a credit spread reads as "I will pay the width to enter", which is
# a gift to whoever takes the other side and shows up only as inexplicable P&L.


def assert_paper_only() -> None:
    """Refuse to execute anything if live trading could be in play.

    Raises rather than returning a bool: a caller can ignore a False, and
    `assert` statements vanish under `python -O`. This is also deliberately
    stricter than `Settings`, which tolerates an explicit `false`. Here an
    unrecognised value blocks, because "we could not tell" and "it is safe"
    are not the same statement.
    """
    raw = os.environ.get(LIVE_TRADE_ENV_VAR)
    if raw is not None and raw.strip().lower() not in _UNAMBIGUOUSLY_NOT_LIVE:
        msg = (
            f"{LIVE_TRADE_ENV_VAR}={raw!r} is set. This module is paper-only by "
            "hackathon rule and by construction, and it will not submit an order "
            "while that variable could route to live trading. Unset it."
        )
        raise LiveTradingBlocked(msg)


def paper_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a child process is allowed to see.

    Two variables are pinned last and unconditionally, so nothing inherited
    from the parent or from `.env` can change them.

    `ALPACA_LIVE_TRADE=false` means even a parent that had it set to something
    exotic cannot route the CLI to the live endpoint. Note that our own rule
    and the CLI's differ: the CLI matches the exact string "true"
    case-insensitively, so it would treat `ALPACA_LIVE_TRADE=1` as paper, while
    `Settings` parses "1" as True and refuses to start. We are stricter, which
    is the safe direction, but the two do not agree and neither should be
    assumed from the other.

    `ALPACA_OUTPUT=json` matters more than it looks. `.env` can set that
    variable, and `--csv` combined with `--jq` prints empty stdout, empty
    stderr, and exit 0 -- an outcome this module would read as "exit 0 but the
    response is unreadable". Silent data loss on the order path is not
    acceptable even when it fails safe, so the format is pinned rather than
    inherited, and neither `--csv` nor `--jq` is ever passed.

    Note also that `alpaca doctor` is not a safety indicator: its "active
    profile: paper" line names the profile, not the environment, and it prints
    it even against the live host. `ALPACA_PAPER_TRADE` is not read by the CLI
    at all. Neither is used here as a guard.
    """
    env = dict(os.environ)
    if extra:
        env.update(extra)
    env[LIVE_TRADE_ENV_VAR] = "false"
    env[OUTPUT_ENV_VAR] = "json"
    return env


# --------------------------------------------------------------------------
# The payload
# --------------------------------------------------------------------------

LegPayload = dict[str, str]
Payload = dict[str, str | list[LegPayload]]


@dataclass(frozen=True, slots=True)
class OrderLeg:
    """One leg of a multi-leg order.

    `ratio_qty` is an int here and a string in the payload: the API takes
    strings for every numeric field, but ratios have to be integers for the
    GCD rule to mean anything.
    """

    symbol: str
    side: Side
    position_intent: PositionIntent
    ratio_qty: int = 1

    def as_payload(self) -> LegPayload:
        # Key order is fixed so the serialized legs are byte-stable, which is
        # what makes the client_order_id digest and the golden tests reproducible.
        return {
            "symbol": self.symbol,
            "ratio_qty": str(self.ratio_qty),
            "side": self.side.value,
            "position_intent": self.position_intent.value,
        }


@dataclass(frozen=True, slots=True)
class MultiLegOrder:
    """A complete, submittable multi-leg order.

    `limit_price` is signed per `LimitPriceConvention` and quantised to the
    cent. `qty` is the number of *spreads*, not contracts; contracts on a leg
    are `qty * ratio_qty`.
    """

    client_order_id: str
    qty: int
    limit_price: Decimal
    legs: tuple[OrderLeg, ...]
    time_in_force: str = TIME_IN_FORCE

    @property
    def is_credit(self) -> bool:
        """True when this order expects to receive money, per the convention."""
        return self.limit_price < 0

    def legs_payload(self) -> list[LegPayload]:
        return [leg.as_payload() for leg in self.legs]

    def legs_json(self) -> str:
        """The exact string handed to `--legs`. Compact and key-stable."""
        return json.dumps(self.legs_payload(), separators=(",", ":"))

    def as_payload(self) -> Payload:
        """The REST body, identical for both backends.

        No top-level `symbol`, `side`, or `position_intent`: for `mleg` all
        three live on the legs, and sending a top-level symbol is an error.
        See docs/GOTCHAS.md #6.
        """
        return {
            "order_class": ORDER_CLASS,
            "qty": str(self.qty),
            "type": ORDER_TYPE,
            "limit_price": format(self.limit_price, "f"),
            "time_in_force": self.time_in_force,
            "client_order_id": self.client_order_id,
            "legs": self.legs_payload(),
        }


def reduce_ratios(ratios: Sequence[int]) -> tuple[int, ...]:
    """Divide leg ratios by their GCD.

    The API requires each `ratio_qty` in simplest form and rejects a set whose
    GCD exceeds 1 -- legs of 4 and 2 are refused where 2 and 1 are accepted.
    Reducing is always safe because `qty` scales the whole strategy.
    """
    if not ratios:
        return ()
    divisor = math.gcd(*ratios) if len(ratios) > 1 else ratios[0]
    if divisor <= 0:
        # Leave a nonsensical set alone; validation names it rather than
        # dividing by zero here.
        return tuple(ratios)
    return tuple(r // divisor for r in ratios)


def validate(order: MultiLegOrder) -> str | None:
    """Return why this order is unsubmittable, or None if it is well formed.

    Every rule here is one the broker enforces too. Catching them locally turns
    an opaque 422 into a displayable reason, and costs nothing.
    """
    if not (MIN_LEGS <= len(order.legs) <= MAX_LEGS):
        return f"an mleg order needs {MIN_LEGS}-{MAX_LEGS} legs, got {len(order.legs)}"

    symbols = [leg.symbol for leg in order.legs]
    if any(not s.strip() for s in symbols):
        return "every leg needs a non-empty symbol"
    if len(set(symbols)) != len(symbols):
        return f"leg symbols must be unique, got {symbols}"

    ratios = [leg.ratio_qty for leg in order.legs]
    if any(r < 1 for r in ratios):
        return f"every ratio_qty must be at least 1, got {ratios}"
    if math.gcd(*ratios) != 1:
        return f"leg ratios must be in simplest form (GCD 1), got {ratios}"

    if order.qty < 1:
        return f"qty is the number of spreads and must be at least 1, got {order.qty}"

    if order.time_in_force not in VALID_TIME_IN_FORCE:
        return (
            f"time_in_force for options must be one of "
            f"{sorted(VALID_TIME_IN_FORCE)}, got {order.time_in_force!r}"
        )

    price = order.limit_price
    if not price.is_finite():
        return f"limit_price must be finite, got {price!r}"
    if price == 0:
        return "limit_price of zero is neither a debit nor a credit"
    if price.as_tuple().exponent != -2:
        return f"limit_price must be quantised to the cent, got {format(price, 'f')}"

    cid = order.client_order_id
    if not cid or len(cid) > MAX_CLIENT_ORDER_ID_CHARS:
        return f"client_order_id must be 1-{MAX_CLIENT_ORDER_ID_CHARS} chars, got {len(cid)}"
    if not _CLIENT_ORDER_ID_RE.match(cid):
        return f"client_order_id has characters we do not send unescaped: {cid!r}"

    return None


def to_limit_price(amount: float | Decimal, *, credit: bool) -> Decimal:
    """Signed, cent-quantised limit price for `amount` dollars per spread.

    `amount` is always the positive magnitude; `credit` says which way it goes.
    A credit comes back negative and a debit positive, per the OAS convention
    documented at the top of this module. There is no switch: a configurable
    sign here would only ever be a way to get it wrong.

    Rounding is `ROUND_CEILING` on the *signed* value in both directions, which
    is one rule that happens to be conservative twice over. A credit of 0.4278
    becomes -0.42, so we demand slightly less premium than modelled. A debit of
    0.4278 becomes 0.43, so we offer slightly more to close. Both lean toward
    getting filled, and both make the realised economics no better than the
    modelled ones -- never better, which is the direction that keeps the
    backtest honest.
    """
    magnitude = Decimal(str(amount))
    signed = -magnitude if credit else magnitude
    if not signed.is_finite():
        # Quantising a NaN raises. Hand the nonsense straight through so
        # `validate` names it as a displayable reason instead of a traceback.
        return signed
    return signed.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def client_order_id(
    *,
    action: str,
    underlying: str,
    legs: Sequence[OrderLeg],
    qty: int,
    limit_price: Decimal,
    now: datetime,
    nonce: str = "",
) -> str:
    """A deterministic, unique, <=128 character idempotency key.

    Deterministic is the load-bearing word. The same intended order rebuilt
    from the same inputs produces the same id, which is what makes
    `order get-by-client-id` a usable answer to "did that submission land?".
    A random id would make the lookup meaningless and every ambiguous timeout
    unresolvable.

    Unique falls out of the digest: distinct legs, quantities, prices, or days
    give distinct ids. Two *identical* orders on the same day therefore collide
    on purpose. Note that the broker is **not** documented to reject a
    duplicate `client_order_id` (docs/GOTCHAS.md #9), so the collision is not
    itself a safety net -- it is what makes the lookup answerable. The safety
    net is the adapter's refusal to resubmit without proof of absence.
    `nonce` is the deliberate escape hatch for genuinely wanting a second
    identical position.

    `now` is required and must be timezone-aware; the date bucket is UTC so the
    id does not depend on the host's local zone.
    """
    if now.tzinfo is None:
        msg = "client_order_id needs a timezone-aware datetime; naive input is ambiguous"
        raise ValueError(msg)
    day = now.astimezone(UTC).strftime("%Y%m%d")
    canonical = json.dumps(
        {
            "action": action,
            "legs": [leg.as_payload() for leg in legs],
            "qty": qty,
            "limit_price": format(limit_price, "f"),
            "day": day,
            "nonce": nonce,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = blake2s(canonical.encode("utf-8"), digest_size=8).hexdigest()
    ticker = _TICKER_RE.sub("", underlying.upper())[:8] or "NA"
    # ~40 characters, well inside the 128 limit, and readable in a broker UI.
    return f"uw-{action}-{ticker}-{day}-{digest}"


def build_opening_order(
    spread: CreditSpread,
    *,
    contracts: int,
    credit: float | None = None,
    now: datetime | None = None,
    time_in_force: str = TIME_IN_FORCE,
    nonce: str = "",
) -> MultiLegOrder:
    """Turn a selected `CreditSpread` into a submittable opening order.

    Short leg `sell_to_open`, long leg `buy_to_open`, ratio 1 each, and a
    negative limit price because we are collecting premium. `credit` overrides
    the spread's modelled credit for callers that want to submit at a different
    price (a marketable-limit probe, say); it is a magnitude in dollars per
    spread, not a signed value.

    `time_in_force` defaults to `day` because nothing should rest overnight
    unmonitored, but `gtc` is equally valid for options and the caller may ask
    for it. See docs/GOTCHAS.md #11.
    """
    legs = (
        OrderLeg(
            symbol=spread.short_leg.symbol,
            side=Side.SELL,
            position_intent=PositionIntent.SELL_TO_OPEN,
        ),
        OrderLeg(
            symbol=spread.long_leg.symbol,
            side=Side.BUY,
            position_intent=PositionIntent.BUY_TO_OPEN,
        ),
    )
    price = to_limit_price(spread.credit if credit is None else credit, credit=True)
    stamp = now or datetime.now(UTC)
    return MultiLegOrder(
        client_order_id=client_order_id(
            action="open",
            underlying=spread.underlying,
            legs=legs,
            qty=contracts,
            limit_price=price,
            now=stamp,
            nonce=nonce,
        ),
        qty=contracts,
        limit_price=price,
        legs=legs,
        time_in_force=time_in_force,
    )


def build_closing_order(
    spread: CreditSpread,
    *,
    contracts: int,
    debit: float,
    now: datetime | None = None,
    time_in_force: str = TIME_IN_FORCE,
    nonce: str = "",
) -> MultiLegOrder:
    """The order that flattens an open credit spread.

    Every intent flips: the short leg is bought back (`buy_to_close`) and the
    protective long leg is sold (`sell_to_close`). Sides flip with them. The
    price flips sign too -- closing a credit spread costs money -- so `debit`
    is the positive magnitude we are willing to pay per spread.

    This exists because we default to `day` orders: nothing rests overnight, so
    an exit is an order the agent actively places each session, not a resting
    one it sets and forgets. See docs/GOTCHAS.md #11.

    Timing note for callers, not enforced here: if buying power is insufficient
    for an in-the-money exercise, Alpaca will sell the position out itself
    within the hour before expiry (docs/GOTCHAS.md #10). A spread still open
    inside that window is a hazard, not a normal state -- the broker, not the
    strategy, decides the exit price. The flatten cutoff that keeps us out of
    that window belongs in session/risk logic, not in the order builder.
    """
    legs = (
        OrderLeg(
            symbol=spread.short_leg.symbol,
            side=Side.BUY,
            position_intent=PositionIntent.BUY_TO_CLOSE,
        ),
        OrderLeg(
            symbol=spread.long_leg.symbol,
            side=Side.SELL,
            position_intent=PositionIntent.SELL_TO_CLOSE,
        ),
    )
    price = to_limit_price(debit, credit=False)
    stamp = now or datetime.now(UTC)
    return MultiLegOrder(
        client_order_id=client_order_id(
            action="close",
            underlying=spread.underlying,
            legs=legs,
            qty=contracts,
            limit_price=price,
            now=stamp,
            nonce=nonce,
        ),
        qty=contracts,
        limit_price=price,
        legs=legs,
        time_in_force=time_in_force,
    )


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    """The part of the broker's order object we act on.

    These are all *parent* fields, and the parent's units are not the legs'
    units (docs/GOTCHAS.md #8). Conflating them silently corrupts P&L:

    - `filled_qty` on the parent counts **strategy units** -- spreads, not
      contracts. On a leg it counts contracts, which is `ratio_qty` times the
      parent qty.
    - `filled_avg_price` on the parent is the **signed net per spread**, so a
      filled credit spread reports it as negative. On a leg it is that leg's
      own premium, always positive.
    - The parent's `side` and `symbol` come back as empty strings, which is why
      neither is read here.

    A partial fill is a real state: "all or nothing" binds the *ratio*, not the
    quantity, so with qty 5 two spreads can fill while three keep working and
    the parent sits at `partially_filled` -- balanced, but smaller than asked
    for. At qty 1 it is genuine all-or-nothing.
    """

    id: str
    client_order_id: str
    status: str
    # Spreads filled, not contracts. None when the broker did not report it.
    filled_qty: Decimal | None = None
    # Signed net per spread. Negative for a filled credit spread.
    filled_avg_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OrderResult:
    """The outcome of one execution attempt, success or failure.

    `payload` is the exact body sent, so the journal can reconstruct what was
    asked for without trusting the broker's echo. `backends_tried` and
    `attempts` make a recovered order legible after the fact: an operator
    reading the audit log can see that a timeout happened and how it resolved.

    `status` is the broker's own word, passed through unaltered. It is not
    necessarily terminal: a multi-leg order can come back `partially_filled`
    with some spreads done and the rest still working (docs/GOTCHAS.md #8).
    Deciding what an open position actually is belongs to reconciliation, not
    here -- this module reports what the broker said and nothing more.

    Reconciliation should also know that listing orders requires `nested=true`,
    or the legs come back as separate flat orders and the join goes wrong
    without complaint. That is not reachable from here: the recovery lookup
    uses `order get-by-client-id`, which has no `--nested` flag on v0.0.14, so
    it returns the parent only.
    """

    ok: bool
    backend: Backend | None
    client_order_id: str
    payload: Payload
    order_id: str | None = None
    status: str | None = None
    # Parent units: spreads filled, and the signed net per spread (negative for
    # a filled credit). See BrokerOrder for the parent/leg unit trap.
    filled_qty: Decimal | None = None
    filled_avg_price: Decimal | None = None
    reason: Reason | None = None
    message: str = ""
    dry_run: bool = False
    recovered: bool = False
    attempts: int = 0
    backends_tried: tuple[Backend, ...] = ()
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


class Kind(StrEnum):
    """What a backend managed to establish about an order. Internal."""

    ACCEPTED = "accepted"
    # The broker positively confirmed no such client_order_id exists. This is
    # the only state from which resubmitting is safe.
    ABSENT = "absent"
    # A definite no. No order was created and none will be.
    TERMINAL = "terminal"
    # We do not know. Never retried without a lookup.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BackendOutcome:
    kind: Kind
    order: BrokerOrder | None = None
    reason: Reason | None = None
    message: str = ""


class OrderBackend(Protocol):
    """One transport to the broker. Both implementations send the same payload."""

    @property
    def name(self) -> Backend: ...

    def unavailable_reason(self) -> str | None:
        """Why this backend cannot be used at all, or None if it can."""
        ...

    def submit(self, order: MultiLegOrder, *, dry_run: bool = False) -> BackendOutcome: ...

    def lookup(self, client_order_id: str) -> BackendOutcome: ...


# --------------------------------------------------------------------------
# Shared response parsing
# --------------------------------------------------------------------------


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _as_decimal(value: object) -> Decimal | None:
    """Alpaca sends numbers as strings. Unparseable stays None, never zero.

    Zero and "not reported" mean opposite things on a fill, and defaulting the
    second to the first would read an unfilled order as one filled at no cost.
    """
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _broker_order_from(body: object) -> BrokerOrder | None:
    """Read an order out of a broker response, or None if it is not one."""
    if not isinstance(body, Mapping):
        return None
    order_id = body.get("id")
    client_id = body.get("client_order_id")
    if not isinstance(order_id, str) or not isinstance(client_id, str):
        return None
    status = body.get("status")
    return BrokerOrder(
        id=order_id,
        client_order_id=client_id,
        status=status if isinstance(status, str) else "unknown",
        filled_qty=_as_decimal(body.get("filled_qty")),
        filled_avg_price=_as_decimal(body.get("filled_avg_price")),
    )


def _classify_status(status: int | None, *, is_lookup: bool, detail: str) -> BackendOutcome:
    """Map an HTTP status onto what we may safely conclude.

    The bias is deliberate and one-directional: anything we cannot confidently
    call a definite failure becomes UNKNOWN, which forces a lookup rather than
    a retry. Being wrong toward UNKNOWN costs one extra API call; being wrong
    toward TERMINAL loses an order we could have found, and being wrong toward
    "safe to retry" doubles a position.
    """
    if status in (401, 403):
        return BackendOutcome(kind=Kind.TERMINAL, reason=Reason.AUTH, message=detail)
    if status == 404:
        if is_lookup:
            # The one positive proof of absence in the whole module.
            return BackendOutcome(kind=Kind.ABSENT, message=detail)
        return BackendOutcome(kind=Kind.TERMINAL, reason=Reason.API_ERROR, message=detail)
    if status == 409:
        # Our client_order_id already exists. Duplicate handling on POST
        # /v2/orders is undocumented (docs/GOTCHAS.md #9), so this may never
        # occur -- but if it does it is not a failure yet, because the order it
        # collides with is almost certainly ours. Go and read it.
        return BackendOutcome(kind=Kind.UNKNOWN, reason=Reason.DUPLICATE, message=detail)
    if status in (400, 422):
        return BackendOutcome(kind=Kind.TERMINAL, reason=Reason.REJECTED, message=detail)
    if status is not None and 400 <= status < 500:
        return BackendOutcome(kind=Kind.TERMINAL, reason=Reason.API_ERROR, message=detail)
    # 5xx, or a status we could not read at all: the request may have been
    # applied server-side. Assume nothing.
    return BackendOutcome(kind=Kind.UNKNOWN, reason=Reason.API_ERROR, message=detail)


# --------------------------------------------------------------------------
# CLI backend
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompletedCommand:
    """The result of running the CLI.

    `timed_out` is a field rather than an exception so that the fake runner in
    the tests is a pure function and no control flow crosses the boundary as an
    exception.
    """

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class CommandRunner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str]
    ) -> CompletedCommand: ...


def subprocess_runner(
    argv: Sequence[str], *, timeout: float, env: Mapping[str, str]
) -> CompletedCommand:
    """Run the CLI with a hard timeout. This never hangs the agent."""
    try:
        completed = subprocess.run(  # noqa: S603 - argv is built here, never shell-interpolated
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=dict(env),
        )
    except subprocess.TimeoutExpired:
        return CompletedCommand(returncode=-1, timed_out=True)
    except OSError as exc:
        return CompletedCommand(returncode=-1, stderr=f"could not execute {argv[0]}: {exc}")
    return CompletedCommand(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


@dataclass(frozen=True, slots=True)
class CliBackend:
    """The Alpaca CLI (v0.0.14), driven with `--legs`.

    Primary because the hackathon requires the MCP server or the CLI on the
    order path and the MCP server's multi-leg support is broken. Alpha preview
    software, hence the fallback.
    """

    binary: str = "alpaca"
    runner: CommandRunner = subprocess_runner
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    env_extra: Mapping[str, str] = field(default_factory=dict)

    @property
    def name(self) -> Backend:
        return Backend.CLI

    def unavailable_reason(self) -> str | None:
        if shutil.which(self.binary) is None:
            return f"the Alpaca CLI ({self.binary}) is not on PATH"
        return None

    def submit_argv(self, order: MultiLegOrder, *, dry_run: bool = False) -> list[str]:
        """The exact argv. Exposed so tests and probes can assert on it.

        `--limit-price` uses the `--flag=value` form because a credit price is
        negative and a bare `-0.42` would be parsed as a flag, not a value. No
        `--symbol`: for mleg it lives on each leg and sending it is an error.
        `--type limit` is explicit because the CLI defaults to market.
        """
        argv = [
            self.binary,
            "order",
            "submit",
            "--order-class",
            ORDER_CLASS,
            "--qty",
            str(order.qty),
            "--type",
            ORDER_TYPE,
            f"--limit-price={format(order.limit_price, 'f')}",
            "--time-in-force",
            order.time_in_force,
            "--client-order-id",
            order.client_order_id,
            "--legs",
            order.legs_json(),
            "--quiet",
        ]
        if dry_run:
            argv.append("--dry-run")
        return argv

    def submit(self, order: MultiLegOrder, *, dry_run: bool = False) -> BackendOutcome:
        # Repeated from the adapter on purpose: a backend used directly must be
        # no more capable of reaching live than one used through the adapter.
        assert_paper_only()
        argv = self.submit_argv(order, dry_run=dry_run)
        completed = self.runner(argv, timeout=self.timeout, env=paper_environment(self.env_extra))
        if dry_run:
            return self._interpret_dry_run(completed)
        return self._interpret(completed, is_lookup=False)

    def lookup(self, client_order_id: str) -> BackendOutcome:
        argv = [
            self.binary,
            "order",
            "get-by-client-id",
            "--client-order-id",
            client_order_id,
            "--quiet",
        ]
        completed = self.runner(argv, timeout=self.timeout, env=paper_environment(self.env_extra))
        return self._interpret(completed, is_lookup=True)

    def _interpret_dry_run(self, completed: CompletedCommand) -> BackendOutcome:
        """A dry run submits nothing, so success is simply exit 0.

        And exit 0 is worth almost nothing. `--dry-run` is a local JSON
        pretty-printer: it makes no HTTP call and exits 0 for payloads Alpaca
        would reject outright -- `--type` omitted (silently market),
        `--symbol` passed alongside `--legs` (emitted rather than stripped),
        `--order-class` omitted, five legs, `--qty` omitted, wrong-case sides,
        unknown leg fields dropped, and `--legs '[]'` which prints an mleg
        order with no legs at all.

        So dry-run is a way to show a human the payload, and worthless as a
        correctness check. `validate()` runs before any submission, dry or
        real, and is the only validation that exists.
        """
        if completed.timed_out:
            return BackendOutcome(
                kind=Kind.TERMINAL, reason=Reason.TIMEOUT, message="dry run timed out"
            )
        if completed.returncode != 0:
            return BackendOutcome(
                kind=Kind.TERMINAL,
                reason=Reason.API_ERROR,
                message=_cli_error_text(completed),
            )
        return BackendOutcome(kind=Kind.ACCEPTED, message=completed.stdout.strip())

    def _interpret(self, completed: CompletedCommand, *, is_lookup: bool) -> BackendOutcome:
        if completed.timed_out:
            return BackendOutcome(
                kind=Kind.UNKNOWN,
                reason=Reason.TIMEOUT,
                message=f"the CLI did not finish within {self.timeout:g}s",
            )

        if completed.returncode == 0:
            order = _broker_order_from(_loads(completed.stdout))
            if order is None:
                # Exit 0 means it very probably worked, but we cannot read the
                # order. Do not call that a failure and do not retry it.
                return BackendOutcome(
                    kind=Kind.UNKNOWN,
                    reason=Reason.MALFORMED_RESPONSE,
                    message=f"exit 0 but stdout is not an order: {completed.stdout.strip()[:300]}",
                )
            return BackendOutcome(kind=Kind.ACCEPTED, order=order)

        # Exit code 2 means HTTP 401 specifically, so it is terminal and it is
        # auth. It is NOT the whole of "auth failed": entirely absent
        # credentials exit 1 with a structured error whose `status` is 0,
        # because no request was ever made. Branching on the code alone is too
        # coarse in that direction, so the body is read below.
        if completed.returncode == 2:
            return BackendOutcome(
                kind=Kind.TERMINAL, reason=Reason.AUTH, message=_cli_error_text(completed)
            )

        body = _loads(completed.stderr) or _loads(completed.stdout)
        detail = _cli_error_text(completed)
        if isinstance(body, Mapping):
            status = _as_int(body.get("status"))
            if status in (None, 0) and _is_missing_credentials(body):
                # The CLI could not resolve credentials, so it never opened a
                # connection and no order can exist. This is the one status-0
                # error we are willing to call terminal: every other one could
                # equally be a connection dropped after the POST was written.
                return BackendOutcome(kind=Kind.TERMINAL, reason=Reason.AUTH, message=detail)
            return _classify_status(status, is_lookup=is_lookup, detail=detail)

        # Unstructured stderr on a non-zero exit. This is what an alpha-preview
        # CLI renaming a flag looks like, and it is also what a crash looks
        # like. We cannot tell them apart, so we conclude nothing.
        return BackendOutcome(
            kind=Kind.UNKNOWN,
            reason=Reason.API_ERROR,
            message=f"exit {completed.returncode} with unstructured output: {detail}",
        )


def _loads(text: str) -> object:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _is_missing_credentials(body: Mapping[str, object]) -> str | None:
    """Recognise the CLI's "no credentials at all" error.

    Verified against v0.0.14: running the binary without credentials emits
    `{"error": "authentication required\nHint: run `alpaca profile login` ...",
    "status": 0}` and exits 1. `status: 0` means no HTTP response was received,
    which on its own proves nothing -- a connection reset after the request was
    written looks the same. The error text is what distinguishes a failure that
    happened before any request from one that may have happened after.
    """
    error = body.get("error")
    if not isinstance(error, str):
        return None
    return error if "authentication required" in error.lower() else None


def _cli_error_text(completed: CompletedCommand) -> str:
    """The broker's own words, verbatim and bounded."""
    raw = completed.stderr.strip() or completed.stdout.strip()
    return raw[:500] or f"exit {completed.returncode} with no output"


# --------------------------------------------------------------------------
# SDK backend
# --------------------------------------------------------------------------


class TradingApiLike(Protocol):
    """The two alpaca-py calls this module uses.

    Deliberately the raw REST methods rather than `submit_order`: it means both
    backends send byte-identical bodies, and it sidesteps alpaca-py typing
    `ratio_qty` as a float, which would serialize `1.0` where the spec wants
    the string `"1"`.
    """

    def post(self, path: str, data: dict[str, object] | None = ...) -> object: ...

    def get(self, path: str, data: dict[str, object] | None = ...) -> object: ...


# alpaca-py 0.44.0 retries internally, and the attribute holding the count is
# private. Named here so the assignment below is not a bare string literal
# pointing at someone else's internals without explanation.
_RETRY_ATTEMPTS_ATTR: Final = "_retry"


def disable_automatic_retries(client: object) -> None:
    """Stop alpaca-py retrying requests by itself, and prove that it worked.

    This is a safety property, not a tuning knob, so it raises rather than
    returning a status. Verified against alpaca-py 0.44.0:

    `RESTClient.__init__` defaults to `DEFAULT_RETRY_ATTEMPTS = 3`,
    `DEFAULT_RETRY_WAIT_SECONDS = 3`, and
    `DEFAULT_RETRY_EXCEPTION_CODES = [429, 504]`, and `_request` runs its retry
    loop for every HTTP method -- POST included. **504 is the dangerous code**:
    a gateway timeout on an order submission means the request very possibly
    reached the order system, and retrying it is precisely the double-submit
    this module exists to prevent.

    Two things block the obvious fix. `TradingClient.__init__` does not expose
    `retry_attempts` at all; and even on `RESTClient`, the constructor applies
    it under `if retry_attempts and retry_attempts > 0`, so passing 0 is falsy,
    is ignored, and silently leaves the default of 3. There is no public way to
    turn this off. Writing the private attribute is the only option, so the
    write is read back and a failure is raised loudly -- an alpaca-py upgrade
    that renames it must break the build, not quietly restore retries on the
    order path.
    """
    try:
        setattr(client, _RETRY_ATTEMPTS_ATTR, 0)
        confirmed = getattr(client, _RETRY_ATTEMPTS_ATTR)
    except AttributeError as exc:
        msg = (
            f"cannot disable alpaca-py's automatic retries: {type(client).__name__} has no "
            f"{_RETRY_ATTEMPTS_ATTR!r}. It retries POSTs on 429 and 504 by default, and a "
            "retried order submission can double a position. Refusing to submit through it."
        )
        raise RuntimeError(msg) from exc
    if confirmed != 0:
        msg = (
            f"alpaca-py retries are still enabled ({_RETRY_ATTEMPTS_ATTR}={confirmed!r}) after "
            "trying to disable them. A retried POST can double a position; refusing to submit."
        )
        raise RuntimeError(msg)


def paper_trading_client(api_key: str, secret_key: str) -> TradingApiLike:
    """An alpaca-py client that can only ever be a paper client, and never retries.

    `paper=True` is a literal. There is no parameter, environment read, or
    branch that could make it False. Imported lazily so CLI-only runs do not
    pay for the SDK import.

    Retries are disabled before the client is handed out, because this is the
    client that carries order submissions. See `disable_automatic_retries`.
    """
    from alpaca.trading.client import TradingClient

    client: TradingApiLike = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
    disable_automatic_retries(client)
    return client


@dataclass(frozen=True, slots=True)
class SdkBackend:
    """alpaca-py fallback, sending the identical REST payload.

    Exists because the CLI's README says, verbatim, *"Commands, flags, and
    output formats may change or be removed without notice between releases. Do
    not depend on current behavior in production workflows."* An order path
    with one alpha-preview transport and no alternative is a single point of
    failure on the only thing that makes money.
    """

    client: TradingApiLike | None = None

    @property
    def name(self) -> Backend:
        return Backend.SDK

    def unavailable_reason(self) -> str | None:
        if self.client is None:
            return "no alpaca-py trading client was supplied"
        return None

    def submit(self, order: MultiLegOrder, *, dry_run: bool = False) -> BackendOutcome:
        assert_paper_only()  # see CliBackend.submit
        if dry_run:
            # There is no server-side dry run on the REST path, so the honest
            # thing is to return the payload we would have sent and submit
            # nothing at all.
            return BackendOutcome(
                kind=Kind.ACCEPTED, message=json.dumps(order.as_payload(), separators=(",", ":"))
            )
        if self.client is None:
            return BackendOutcome(
                kind=Kind.TERMINAL,
                reason=Reason.BACKEND_UNAVAILABLE,
                message="no alpaca-py trading client was supplied",
            )
        try:
            body = self.client.post("/orders", dict(order.as_payload()))
        except Exception as exc:  # deliberately broad: every failure is classified, never raised
            return _classify_exception(exc, is_lookup=False)
        parsed = _broker_order_from(body)
        if parsed is None:
            return BackendOutcome(
                kind=Kind.UNKNOWN,
                reason=Reason.MALFORMED_RESPONSE,
                message=f"POST /orders returned something that is not an order: {body!r}"[:300],
            )
        return BackendOutcome(kind=Kind.ACCEPTED, order=parsed)

    def lookup(self, client_order_id: str) -> BackendOutcome:
        if self.client is None:
            return BackendOutcome(
                kind=Kind.UNKNOWN,
                reason=Reason.BACKEND_UNAVAILABLE,
                message="no alpaca-py trading client was supplied",
            )
        try:
            body = self.client.get(
                "/orders:by_client_order_id", {"client_order_id": client_order_id}
            )
        except Exception as exc:  # see submit: classify, never raise
            return _classify_exception(exc, is_lookup=True)
        parsed = _broker_order_from(body)
        if parsed is None:
            return BackendOutcome(
                kind=Kind.UNKNOWN,
                reason=Reason.MALFORMED_RESPONSE,
                message=f"lookup returned something that is not an order: {body!r}"[:300],
            )
        return BackendOutcome(kind=Kind.ACCEPTED, order=parsed)


def _classify_exception(exc: Exception, *, is_lookup: bool) -> BackendOutcome:
    """Turn an SDK exception into what we may conclude from it.

    alpaca-py raises `APIError` carrying `status_code`, so the HTTP status is
    reachable without importing the SDK here. A transport timeout is detected
    by class name rather than by importing `requests`, which keeps this module
    importable without the SDK's dependency tree; an unrecognised exception
    falls through to UNKNOWN, which is the safe direction.
    """
    name = type(exc).__name__
    if "Timeout" in name:
        return BackendOutcome(kind=Kind.UNKNOWN, reason=Reason.TIMEOUT, message=str(exc)[:500])
    status = _as_int(getattr(exc, "status_code", None))
    if status is None:
        return BackendOutcome(
            kind=Kind.UNKNOWN, reason=Reason.API_ERROR, message=f"{name}: {exc}"[:500]
        )
    return _classify_status(status, is_lookup=is_lookup, detail=f"{name}: {exc}"[:500])


# --------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AbsenceProof:
    """Evidence from the broker that no order with this id exists.

    This type is the whole safety property expressed as a type. The only way to
    obtain one is `from_lookup`, which yields None unless the broker positively
    answered "no such order"; and the only function that will send a second
    order is `_resubmit`, which cannot be called without one. So "submit again
    without checking first" is not a discouraged code path -- it is one that
    cannot be written.

    This matters more than it would if the broker de-duplicated for us. It does
    not: duplicate `client_order_id` handling on POST /v2/orders is entirely
    undocumented, and Alpaca offers real idempotency via an `Idempotency-Key`
    header only on other endpoints (docs/GOTCHAS.md #9). Worse, the CLI itself
    contains retry machinery (`isRetryable`, `doWithRetry`, `Retry-After`) and
    whether it retries a POST could not be verified -- there is no base-URL
    override to point it at a stub. So the lookup is not belt and braces. It is
    the only thing between a timeout and a silently doubled position.
    """

    client_order_id: str
    detail: str

    @classmethod
    def from_lookup(cls, outcome: BackendOutcome, client_order_id: str) -> _AbsenceProof | None:
        if outcome.kind is not Kind.ABSENT:
            return None
        return cls(client_order_id=client_order_id, detail=outcome.message)


def _resubmit(backend: OrderBackend, order: MultiLegOrder, proof: _AbsenceProof) -> BackendOutcome:
    """Send an order a second time. Requires proof the first one does not exist.

    The identity check is not defensive padding: proof for one order must never
    authorise sending a different one.
    """
    if proof.client_order_id != order.client_order_id:
        msg = (
            f"absence proof is for {proof.client_order_id!r} but the order is "
            f"{order.client_order_id!r}; refusing to resubmit"
        )
        raise ValueError(msg)
    return backend.submit(order, dry_run=False)


@dataclass(frozen=True, slots=True)
class _Attempt:
    """A backend's final answer plus whether another backend may be tried."""

    result: OrderResult
    may_fall_back: bool


@dataclass(frozen=True, slots=True)
class ExecutionAdapter:
    """Submits multi-leg orders, and refuses to duplicate them.

    `max_submit_attempts` counts submissions to one backend, and the second one
    is only ever reached through a lookup that proved the first created
    nothing.
    """

    primary: OrderBackend
    fallback: OrderBackend | None = None
    # Which transport answers "does this order exist?". Deliberately separable
    # from the one that submits: the read is idempotent, so it can go through
    # the CLI while the POST goes through the SDK, and a second, independent
    # transport confirming an outcome is better evidence than the one that just
    # failed to report it.
    reconciler: OrderBackend | None = None
    max_submit_attempts: int = DEFAULT_MAX_SUBMIT_ATTEMPTS

    def submit(self, order: MultiLegOrder, *, dry_run: bool = False) -> OrderResult:
        """Submit `order`, or explain why it was not submitted.

        `dry_run` plumbs the CLI's `--dry-run` through: the request body is
        produced and nothing is sent, so an integration probe can diff the real
        payload against the spec without touching the account.
        """
        assert_paper_only()

        problem = validate(order)
        if problem is not None:
            return OrderResult(
                ok=False,
                backend=None,
                client_order_id=order.client_order_id,
                payload=order.as_payload(),
                reason=Reason.INVALID_PAYLOAD,
                message=problem,
                dry_run=dry_run,
            )

        tried: list[Backend] = []
        # `answered` is a result from a backend that actually reached the
        # broker. It outranks an "unavailable" note from a later backend,
        # because "the CLI timed out twice and the order does not exist" is a
        # far more useful thing to show an operator than "there was no SDK
        # client configured".
        answered: OrderResult | None = None
        unusable: OrderResult | None = None
        for backend in self._backends():
            unavailable = backend.unavailable_reason()
            if unavailable is not None:
                tried.append(backend.name)
                unusable = OrderResult(
                    ok=False,
                    backend=backend.name,
                    client_order_id=order.client_order_id,
                    payload=order.as_payload(),
                    reason=Reason.BACKEND_UNAVAILABLE,
                    message=unavailable,
                    dry_run=dry_run,
                    backends_tried=tuple(tried),
                )
                continue

            tried.append(backend.name)
            attempt = self._via(backend, order, dry_run=dry_run, tried=tuple(tried))
            answered = attempt.result
            if attempt.result.ok or not attempt.may_fall_back:
                return attempt.result

        last = answered or unusable
        if last is not None:
            return last
        return OrderResult(
            ok=False,
            backend=None,
            client_order_id=order.client_order_id,
            payload=order.as_payload(),
            reason=Reason.BACKEND_UNAVAILABLE,
            message="no execution backend was configured",
            dry_run=dry_run,
        )

    def _lookup(self, submitter: OrderBackend, client_order_id: str) -> BackendOutcome:
        """Ask the broker whether the order exists, escalating until answered.

        The reconciler is asked first, then the backend that submitted. Only a
        definite answer -- ACCEPTED or ABSENT -- stops the escalation, so a
        reconciler that is missing or itself broken degrades to a second
        opinion rather than to a refusal. Two independent chances to resolve an
        ambiguous submission strictly improves on one, and neither can weaken
        the rule that absence must be positively proven.
        """
        probes: list[OrderBackend] = []
        if self.reconciler is not None and self.reconciler.unavailable_reason() is None:
            probes.append(self.reconciler)
        if submitter not in probes:
            probes.append(submitter)

        last = BackendOutcome(
            kind=Kind.UNKNOWN,
            reason=Reason.BACKEND_UNAVAILABLE,
            message="no backend could look the order up",
        )
        for probe in probes:
            outcome = probe.lookup(client_order_id)
            answered = f"{probe.name} lookup: {outcome.message}"
            if outcome.kind in (Kind.ACCEPTED, Kind.ABSENT):
                return BackendOutcome(
                    kind=outcome.kind,
                    order=outcome.order,
                    reason=outcome.reason,
                    message=answered,
                )
            last = BackendOutcome(kind=outcome.kind, reason=outcome.reason, message=answered)
        return last

    def _backends(self) -> tuple[OrderBackend, ...]:
        return (self.primary,) if self.fallback is None else (self.primary, self.fallback)

    def _via(
        self,
        backend: OrderBackend,
        order: MultiLegOrder,
        *,
        dry_run: bool,
        tried: tuple[Backend, ...],
    ) -> _Attempt:
        """One backend's full submit-and-reconcile loop.

        The shape of this function is the whole point of the module. Read the
        UNKNOWN branch as: we do not know what happened, so we ask the broker,
        and we only ever send a second order when the broker has said in as
        many words that the first one does not exist.
        """
        payload = order.as_payload()

        def result(
            *,
            ok: bool,
            found: BrokerOrder | None = None,
            status: str | None = None,
            reason: Reason | None = None,
            message: str = "",
            recovered: bool = False,
            attempts: int = 0,
        ) -> OrderResult:
            return OrderResult(
                ok=ok,
                backend=backend.name,
                client_order_id=order.client_order_id,
                payload=payload,
                order_id=None if found is None else found.id,
                status=found.status if found is not None else status,
                filled_qty=None if found is None else found.filled_qty,
                filled_avg_price=None if found is None else found.filled_avg_price,
                reason=reason,
                message=message,
                dry_run=dry_run,
                recovered=recovered,
                attempts=attempts,
                backends_tried=tried,
            )

        attempts = 1
        outcome = backend.submit(order, dry_run=dry_run)

        if dry_run:
            ok = outcome.kind is Kind.ACCEPTED
            return _Attempt(
                result=result(
                    ok=ok,
                    status=DRY_RUN_STATUS if ok else None,
                    reason=None if ok else (outcome.reason or Reason.API_ERROR),
                    message=outcome.message,
                    attempts=0,
                ),
                may_fall_back=not ok,
            )

        while True:
            if outcome.kind is Kind.ACCEPTED:
                return _Attempt(
                    result=result(
                        ok=True,
                        found=outcome.order,
                        message=outcome.message,
                        recovered=attempts > 1 or outcome.reason is Reason.DUPLICATE,
                        attempts=attempts,
                    ),
                    may_fall_back=False,
                )

            if outcome.kind is Kind.TERMINAL:
                # The broker gave a definite answer. Another transport would
                # get the same answer, so do not ask it twice.
                return _Attempt(
                    result=result(
                        ok=False,
                        reason=outcome.reason or Reason.API_ERROR,
                        message=outcome.message,
                        attempts=attempts,
                    ),
                    may_fall_back=False,
                )

            # UNKNOWN. Ask the broker what actually happened before doing
            # anything else. This lookup is the single most important call in
            # the module.
            ambiguous = outcome
            probe = self._lookup(backend, order.client_order_id)

            if probe.kind is Kind.ACCEPTED and probe.order is not None:
                # It did land. Report the real order rather than resubmitting.
                return _Attempt(
                    result=result(
                        ok=True,
                        found=probe.order,
                        message=(
                            f"{ambiguous.reason or Reason.UNKNOWN_OUTCOME}: {ambiguous.message}; "
                            "recovered by client_order_id lookup"
                        ),
                        recovered=True,
                        attempts=attempts,
                    ),
                    may_fall_back=False,
                )

            proof = _AbsenceProof.from_lookup(probe, order.client_order_id)
            if proof is None:
                # We could not read the outcome and we could not prove absence.
                # Stop. A duplicate spread is worse than a missed one, and it
                # is worse precisely because nothing would say it happened.
                return _Attempt(
                    result=result(
                        ok=False,
                        reason=Reason.UNKNOWN_OUTCOME,
                        message=(
                            f"{ambiguous.reason or Reason.UNKNOWN_OUTCOME}: {ambiguous.message}. "
                            f"Lookup could not confirm the order's absence ({probe.message}), "
                            "so nothing was resubmitted. Reconcile by hand before trading "
                            f"{order.client_order_id} again."
                        ),
                        attempts=attempts,
                    ),
                    may_fall_back=False,
                )

            # Absence proven: no order exists, so resubmitting cannot duplicate.
            if attempts >= self.max_submit_attempts:
                return _Attempt(
                    result=result(
                        ok=False,
                        reason=ambiguous.reason or Reason.UNKNOWN_OUTCOME,
                        message=(
                            f"{ambiguous.message}. The broker confirmed no order exists "
                            f"after {attempts} attempt(s)."
                        ),
                        attempts=attempts,
                    ),
                    # Nothing was created, so another transport is safe here.
                    may_fall_back=True,
                )

            attempts += 1
            outcome = _resubmit(backend, order, proof)


def build_adapter(
    *,
    primary: Backend = Backend.CLI,
    binary: str = "alpaca",
    runner: CommandRunner = subprocess_runner,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    sdk_client: TradingApiLike | None = None,
    env_extra: Mapping[str, str] | None = None,
    max_submit_attempts: int = DEFAULT_MAX_SUBMIT_ATTEMPTS,
) -> ExecutionAdapter:
    """Wire both backends, with `primary` choosing which one carries the POST.

    The default is CLI-first: the hackathon requires the MCP server or the CLI
    on the order path, and the MCP server's multi-leg support is broken
    (docs/GOTCHAS.md #12).

    `primary=Backend.SDK` is a supported configuration, not a rewrite, and
    there is a real reason to want it. The CLI binary carries retry machinery
    whose behaviour on a POST could not be verified, and the broker does not
    de-duplicate `client_order_id` (docs/GOTCHAS.md #9), so a CLI-internal
    retry of a submission is an unquantified double-submission risk on the most
    dangerous call we make. Routing only the POST through the SDK, where retry
    behaviour is ours, leaves the CLI running everything else -- which still
    satisfies the CLI requirement, since it is used prominently throughout.

    Both orderings send the identical payload and get the identical
    lookup-before-retry protection, so this is a one-argument decision.
    """
    cli = CliBackend(binary=binary, runner=runner, timeout=timeout, env_extra=env_extra or {})
    sdk = SdkBackend(client=sdk_client)
    first, second = (cli, sdk) if primary is Backend.CLI else (sdk, cli)
    return ExecutionAdapter(primary=first, fallback=second, max_submit_attempts=max_submit_attempts)


# Restated here so a reader of this module alone can see it: the only host this
# code path can reach. There is deliberately no live constant to pair it with.
TRADING_HOST: Final = PAPER_TRADING_HOST
