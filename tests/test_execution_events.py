"""Real broker execution identities, strict units and complete cursor reads."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import requests

from underwriter.execution_events import (
    ActivityReader,
    ExecutionReadError,
    paper_activity_reader,
    parse_execution_activity,
)

WHEN = datetime(2026, 10, 20, 14, 30, tzinfo=UTC)


def activity(identity: str = "broker-event-a", **updates: object) -> dict[str, object]:
    return {
        "activity_type": "FILL",
        "id": identity,
        "order_id": "broker-child-order",
        "symbol": "XLE261030P00097000",
        "side": "sell_short",
        "qty": "2",
        "price": "0.61",
        "cum_qty": "2",
        "leaves_qty": "1",
        "transaction_time": "2026-10-20T14:30:00.123456Z",
        "type": "partial_fill",
        **updates,
    }


@pytest.mark.parametrize(
    ("side", "normalized"), [("buy", "buy"), ("sell", "sell"), ("sell_short", "sell")]
)
def test_actual_identity_time_decimal_units_and_raw_side_are_preserved(
    side: str, normalized: str
) -> None:
    raw = activity(side=side, group_id="real-broker-group")
    result = parse_execution_activity(raw)
    assert result.activity_id == raw["id"]
    assert result.broker_order_id == raw["order_id"]
    assert result.symbol == raw["symbol"]
    assert result.side == side
    assert result.normalized_side == normalized
    assert result.quantity == Decimal("2")
    assert result.price == Decimal("0.61")
    assert result.cumulative_quantity == Decimal("2")
    assert result.leaves_quantity == Decimal("1")
    assert result.transaction_time == raw["transaction_time"]
    assert result.occurred_at == WHEN + timedelta(microseconds=123456)
    assert result.execution_type == "partial_fill"
    assert result.group_id == "real-broker-group"
    assert result.is_option


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("id", None), ("id", ""), ("id", " padded"),
        ("order_id", None), ("order_id", "with\nnewline"),
        ("symbol", None), ("activity_type", "OPEXP"),
        ("type", None), ("type", "new"), ("side", "unknown"),
        ("qty", "0"), ("qty", "-1"), ("qty", "NaN"), ("qty", "Infinity"),
        ("qty", True), ("qty", 2.0), ("qty", "0.5"),
        ("price", "-1"), ("price", "NaN"), ("price", "Infinity"),
        ("price", False), ("price", 0.61),
        ("cum_qty", "1"), ("cum_qty", "2.5"),
        ("leaves_qty", "-1"), ("leaves_qty", "0.5"),
        ("transaction_time", "2026-10-20T14:30:00"),
        ("transaction_time", "invalid"), ("transaction_time", WHEN),
        ("group_id", ""),
    ],
)
def test_unreadable_execution_evidence_is_rejected(name: str, bad: object) -> None:
    raw = activity()
    raw[name] = bad
    with pytest.raises(ExecutionReadError):
        parse_execution_activity(raw)


def test_zero_price_is_real_and_equity_quantities_can_be_fractional() -> None:
    result = parse_execution_activity(
        activity(symbol="SPY", qty="0.25", cum_qty=None, leaves_qty=None, price="0")
    )
    assert not result.is_option
    assert result.quantity == Decimal("0.25")
    assert result.price == 0
    assert result.cumulative_quantity is None
    assert result.leaves_quantity is None


def test_source_timestamp_precision_and_offset_are_retained() -> None:
    timestamp = "2026-10-20T10:30:00.123456789-04:00"
    result = parse_execution_activity(activity(transaction_time=timestamp))
    assert result.transaction_time == timestamp
    assert result.occurred_at == WHEN + timedelta(microseconds=123456)


@dataclass
class Pages:
    responses: list[object]
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def get(self, path: str, data: dict[str, str] | None = None) -> object:
        self.calls.append((path, dict(data or {})))
        assert self.responses, "Unexpected extra broker read"
        return self.responses.pop(0)


def test_short_pages_continue_with_real_last_id_until_empty_terminal_page() -> None:
    api = Pages([[activity("event-a")], [activity("event-b")], []])
    result = ActivityReader(api).fetch(after=WHEN, until=WHEN + timedelta(hours=1))
    assert result.complete
    assert result.pages == 3
    assert result.next_page_token is None
    assert [row.activity_id for row in result.activities] == ["event-a", "event-b"]
    assert api.calls[0] == (
        "/account/activities",
        {
            "activity_types": "FILL", "direction": "asc", "page_size": "100",
            "after": WHEN.isoformat(), "until": (WHEN + timedelta(hours=1)).isoformat(),
        },
    )
    assert "page_token" not in api.calls[0][1]
    assert api.calls[1][1]["page_token"] == "event-a"
    assert api.calls[2][1]["page_token"] == "event-b"


def test_page_limit_returns_incomplete_batch_that_can_resume_exact_cursor() -> None:
    api = Pages([[activity("event-a")], [activity("event-b")], []])
    reader = ActivityReader(api)
    first = reader.fetch(order_id="real-parent-order", max_pages=1)
    assert not first.complete
    assert first.next_page_token == "event-a"
    assert [row.activity_id for row in first.activities] == ["event-a"]
    second = reader.fetch(order_id="real-parent-order", page_token=first.next_page_token)
    assert second.complete
    assert [row.activity_id for row in second.activities] == ["event-b"]
    assert all(call[1]["order_id"] == "real-parent-order" for call in api.calls)


def test_identical_page_overlap_is_deduplicated_but_conflicts_and_loops_fail() -> None:
    api = Pages([[activity("a")], [activity("a"), activity("b")], []])
    assert len(ActivityReader(api).fetch().activities) == 2
    with pytest.raises(ExecutionReadError, match="changed"):
        ActivityReader(Pages([[activity("a")], [activity("a", price="0.62")]])).fetch()
    with pytest.raises(ExecutionReadError, match="advance"):
        ActivityReader(Pages([[activity("a")], [activity("a")]])).fetch()


@pytest.mark.parametrize("raw", [{}, None, "[]", [1], [activity(), activity("b")]])
def test_unreadable_or_oversized_pages_never_establish_completion(raw: object) -> None:
    with pytest.raises(ExecutionReadError):
        ActivityReader(Pages([raw])).fetch(page_size=1)


@pytest.mark.parametrize(
    "options",
    [
        {"max_pages": 0}, {"max_pages": True}, {"max_pages": 1001},
        {"page_size": 0}, {"page_size": 101}, {"page_size": True},
        {"after": WHEN.replace(tzinfo=None)},
        {"after": WHEN, "until": WHEN - timedelta(seconds=1)},
        {"page_token": ""},
    ],
)
def test_invalid_query_bounds_and_limits_are_rejected(options: dict[str, Any]) -> None:
    with pytest.raises(ExecutionReadError):
        ActivityReader(Pages([])).fetch(**options)


def test_activity_creation_filter_does_not_relabel_late_execution_time() -> None:
    raw = activity(transaction_time="2026-10-19T14:30:00Z")
    result = ActivityReader(Pages([[raw], []])).fetch(after=WHEN)
    assert result.activities[0].occurred_at == WHEN - timedelta(days=1)


def test_nested_order_read_checks_identity_and_uses_only_get() -> None:
    order = {"id": "broker-parent", "legs": [{"id": "broker-child"}]}
    api = Pages([order])
    assert ActivityReader(api).get_order("broker-parent") == order
    assert api.calls == [("/orders/broker-parent", {"nested": "true"})]
    with pytest.raises(ExecutionReadError, match="different"):
        ActivityReader(Pages([{"id": "other"}])).get_order("broker-parent")
    with pytest.raises(ExecutionReadError):
        ActivityReader(Pages([[]])).get_order("broker-parent")


def test_api_failure_is_not_misreported_as_an_empty_page() -> None:
    class Broken:
        def get(self, path: str, data: dict[str, str] | None = None) -> object:
            raise RuntimeError("private broker diagnostic")

    with pytest.raises(ExecutionReadError, match="activity read failed") as failure:
        ActivityReader(Broken()).fetch()
    assert "private" not in str(failure.value)


def test_real_sdk_reader_is_paper_only_and_forwards_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, str, object]] = []

    def request(self: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
        captured.append((method, url, kwargs.get("timeout")))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps([]).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    reader = paper_activity_reader("isolated-test-key", "isolated-test-secret")
    assert reader.fetch().complete
    assert captured == [
        ("GET", "https://paper-api.alpaca.markets/v2/account/activities", (5, 20))
    ]
