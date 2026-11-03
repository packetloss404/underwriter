"""Read real execution activities from Alpaca's paper account.

Account activities identify individual executions with their own IDs and
transaction times. Multi-leg option activities can name child order IDs and
carry contract quantities and positive leg prices. They are not cumulative
parent fills. Joining child IDs to nested order snapshots and deriving balanced
spread groups belongs to reconciliation, outside this read-only module.

Official contracts:
https://docs.alpaca.markets/us/docs/account-activities
https://docs.alpaca.markets/us/reference/getaccountactivities-2
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol
from urllib.parse import quote

from underwriter.account import bound_read_timeout
from underwriter.occ import OccParseError
from underwriter.occ import parse as parse_occ


class ExecutionReadError(ValueError):
    """The broker read or execution evidence cannot be interpreted safely."""


class ActivityApi(Protocol):
    """Only idempotent GET requests are available to this reader."""

    def get(self, path: str, data: dict[str, str] | None = None) -> object: ...


def _text(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 1024
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ExecutionReadError(f"{name} must be a nonempty broker token")
    return value


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    # API numbers are strings. Integers and Decimal are exact injectable
    # values; booleans and binary floats cannot silently become contracts.
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise ExecutionReadError(f"{name} must be an exact numeric value")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ExecutionReadError(f"{name} is not a decimal") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ExecutionReadError(f"{name} must be finite and nonnegative")
    return result


def _moment(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ExecutionReadError(f"{name} must be a broker timestamp")
    try:
        result = datetime.fromisoformat(value)
    except (ValueError, OverflowError):
        raise ExecutionReadError(f"{name} is not an ISO timestamp") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ExecutionReadError(f"{name} must have an explicit timezone")
    return result.astimezone(UTC)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ExecutionReadError("The broker response must contain JSON objects")
    return dict(value)


@dataclass(frozen=True, slots=True)
class ExecutionActivity:
    """One broker execution, retaining its actual identity and exact numbers."""

    activity_id: str
    broker_order_id: str
    symbol: str
    side: Literal["buy", "sell", "sell_short"]
    quantity: Decimal
    price: Decimal
    occurred_at: datetime
    transaction_time: str
    cumulative_quantity: Decimal | None = None
    leaves_quantity: Decimal | None = None
    execution_type: Literal["fill", "partial_fill"] = "fill"
    group_id: str | None = None

    @property
    def normalized_side(self) -> Literal["buy", "sell"]:
        """Cash direction while retaining the broker's actual side above."""
        return "buy" if self.side == "buy" else "sell"

    @property
    def is_option(self) -> bool:
        try:
            parse_occ(self.symbol)
        except OccParseError:
            return False
        return True


def parse_execution_activity(raw: Mapping[str, object]) -> ExecutionActivity:
    """Validate an activity without inferring any execution facts.

    Equity activities can coexist with options and remain distinguishable.
    Option quantities are contracts and must be whole. Zero-price executions
    are permitted; a zero quantity, invalid number or unzoned time is not.
    """
    if raw.get("activity_type") != "FILL":
        raise ExecutionReadError("Expected a FILL account activity")
    execution_type = raw.get("type")
    if execution_type not in ("fill", "partial_fill"):
        raise ExecutionReadError("Expected an execution type of fill or partial_fill")
    side = raw.get("side")
    if side not in ("buy", "sell", "sell_short"):
        raise ExecutionReadError("Execution side must be buy, sell or sell_short")
    activity_id = _text(raw.get("id"), "id")
    order_id = _text(raw.get("order_id"), "order_id")
    symbol = _text(raw.get("symbol"), "symbol")
    timestamp = raw.get("transaction_time")
    occurred_at = _moment(timestamp, "transaction_time")
    assert isinstance(timestamp, str)
    quantity = _decimal(raw.get("qty"), "qty", positive=True)
    price = _decimal(raw.get("price"), "price")
    cumulative = (
        None if raw.get("cum_qty") is None else _decimal(raw["cum_qty"], "cum_qty")
    )
    leaves = (
        None if raw.get("leaves_qty") is None else _decimal(raw["leaves_qty"], "leaves_qty")
    )
    if cumulative is not None and cumulative < quantity:
        raise ExecutionReadError("Cumulative quantity is below this execution quantity")
    try:
        parse_occ(symbol)
    except OccParseError:
        pass
    else:
        if any(
            number is not None and number != number.to_integral_value()
            for number in (quantity, cumulative, leaves)
        ):
            raise ExecutionReadError("Option execution quantities must be whole contracts")
    group_id = None if raw.get("group_id") is None else _text(raw["group_id"], "group_id")
    # The membership checks above narrow object to literal strings at runtime.
    assert side == "buy" or side == "sell" or side == "sell_short"
    assert execution_type == "fill" or execution_type == "partial_fill"
    return ExecutionActivity(
        activity_id=activity_id,
        broker_order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        occurred_at=occurred_at,
        transaction_time=timestamp,
        cumulative_quantity=cumulative,
        leaves_quantity=leaves,
        execution_type=execution_type,
        group_id=group_id,
    )


@dataclass(frozen=True, slots=True)
class ActivityBatch:
    """A bounded read; incomplete pagination must never imply full coverage."""

    activities: tuple[ExecutionActivity, ...]
    pages: int
    complete: bool
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class ActivityReader:
    """Paginate FILL activities using actual broker cursor IDs."""

    client: ActivityApi = field(repr=False)

    def get_order(self, order_id: str) -> Mapping[str, object]:
        """Read nested child IDs and parent evidence without modifying the order."""
        identity = _text(order_id, "order_id")
        try:
            raw = self.client.get(f"/orders/{quote(identity, safe='')}", {"nested": "true"})
        except Exception as exc:
            raise ExecutionReadError("The broker order read failed") from exc
        body = _mapping(raw)
        if body.get("id") != identity:
            raise ExecutionReadError("The broker returned a different order identity")
        return body

    def fetch(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
        order_id: str | None = None,
        page_token: str | None = None,
        max_pages: int = 100,
        page_size: int = 100,
    ) -> ActivityBatch:
        """Read an exact bounded query through an empty terminal page.

        Alpaca's date filters use activity creation time, which may differ from
        execution time. Never relabel transaction_time or reject a late activity
        because its execution happened before the requested creation bound.
        A short page is followed through its last real ID rather than assumed
        to establish completion. A page limit returns a resumable incomplete
        batch; malformed pages, changed IDs and cursor cycles raise.
        """
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= 1000
            or isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 100
        ):
            raise ExecutionReadError("Invalid activity pagination limits")
        parameters = {"activity_types": "FILL", "direction": "asc", "page_size": str(page_size)}
        for name, value in (("after", after), ("until", until)):
            if value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ExecutionReadError("Activity query bounds must have explicit timezones")
                parameters[name] = value.astimezone(UTC).isoformat()
        if after is not None and until is not None and after > until:
            raise ExecutionReadError("Activity query bounds are reversed")
        if order_id is not None:
            parameters["order_id"] = _text(order_id, "order_id")
        cursor = None if page_token is None else _text(page_token, "page_token")
        seen_cursors: set[str] = set()
        if cursor is not None:
            seen_cursors.add(cursor)
        seen: dict[str, ExecutionActivity] = {}
        for page in range(1, max_pages + 1):
            query = dict(parameters)
            if cursor is not None:
                query["page_token"] = cursor
            try:
                raw = self.client.get("/account/activities", query)
            except Exception as exc:
                raise ExecutionReadError("The broker activity read failed") from exc
            if not isinstance(raw, list) or len(raw) > page_size:
                raise ExecutionReadError("The broker activity response is not a bounded page")
            if not raw:
                return ActivityBatch(tuple(seen.values()), page, True, None)
            activities = tuple(parse_execution_activity(_mapping(item)) for item in raw)
            fresh = 0
            for activity in activities:
                existing = seen.get(activity.activity_id)
                if existing is not None and existing != activity:
                    raise ExecutionReadError("An execution ID changed its recorded facts")
                if existing is None:
                    fresh += 1
                    seen[activity.activity_id] = activity
            next_cursor = activities[-1].activity_id
            if next_cursor in seen_cursors or fresh == 0:
                raise ExecutionReadError("The broker activity pagination did not advance")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return ActivityBatch(tuple(seen.values()), max_pages, False, cursor)


def paper_activity_reader(api_key: str, secret_key: str) -> ActivityReader:
    """Construct a separate, bounded reader pinned to the paper host."""
    from alpaca.trading.client import TradingClient

    client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
    bound_read_timeout(client)
    return ActivityReader(client)
