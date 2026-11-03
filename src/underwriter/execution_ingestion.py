"""Idempotent reconciliation of genuine paper-account execution activities.

Raw leg IDs, prices, quantities and transaction times come from the broker.
Balanced daily parent views retain all source IDs and use one real constituent
as their stable anchor. No broker order can be submitted through this module.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from underwriter.execution_closures import ExecutionClosureMatch, plan_execution_closures
from underwriter.execution_events import (
    ActivityBatch,
    ExecutionActivity,
    ExecutionReadError,
    paper_activity_reader,
    parse_execution_activity,
)
from underwriter.journal import (
    MEMORY,
    DerivedSpreadEvidence,
    ExecutionLegEvidence,
    ExecutionOrderEvidence,
    FillAttribution,
    FillSource,
    Journal,
    JournalError,
    OrderRecord,
    OrderStatus,
    ReconciliationScope,
    _dumps_obj,
    _iso,
    _to_leg_fill,
    _to_order,
    _to_spread_fill,
    trading_day_of,
)

RAW_KIND = "alpaca_leg_activity_v1"
GROUP_KIND = "alpaca_leg_activity_aggregate_v1"
RECEIPT_KIND = "alpaca_execution_ingestion_v1"
PRICE_EPSILON = Decimal("0.00000001")


class ExecutionReader(Protocol):
    def fetch(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
        order_id: str | None = None,
        page_token: str | None = None,
        max_pages: int = 100,
        page_size: int = 100,
    ) -> ActivityBatch: ...

    def get_order(self, order_id: str) -> Mapping[str, object]: ...


def _number(value: object, *, signed: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise ExecutionReadError("Missing numeric evidence")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ExecutionReadError("Invalid numeric evidence") from None
    if not result.is_finite() or (not signed and result < 0):
        raise ExecutionReadError("Invalid numeric evidence")
    return result


def _body(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExecutionReadError("Invalid timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ExecutionReadError("Invalid timestamp") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ExecutionReadError("Timestamp needs a timezone")
    return result.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class IngestionPlan:
    legs: tuple[ExecutionLegEvidence, ...]
    groups: tuple[DerivedSpreadEvidence, ...]
    orders: tuple[ExecutionOrderEvidence, ...]
    summary: Mapping[str, object]
    closures: tuple[ExecutionClosureMatch, ...] = ()

    @property
    def complete(self) -> bool:
        return self.summary["status"] == "complete"


@dataclass(slots=True)
class ExecutionIngestor:
    journal: Journal
    reader: ExecutionReader
    lookback: timedelta = timedelta(days=7)
    max_pages: int = 100
    page_size: int = 100

    def plan(self, now: datetime, *, all_orders: bool = False) -> IngestionPlan:
        """Read and validate without changing broker or journal."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Execution reconciliation needs an aware clock")
        now = now.astimezone(UTC)
        known = self.journal.execution_orders()
        raw_rows = self.journal.execution_leg_rows()
        raw_by_id = {row.fill_id: row for row in raw_rows}
        checkpoint = self.journal.execution_ingestion_status(checkpoint=True)
        successful = self.journal.execution_ingestion_status(successful=True, checkpoint=True)
        previous_until = _timestamp(successful.get("until"))
        after = (previous_until or now) - self.lookback
        missing = []
        for order in known:
            if (
                order.status is OrderStatus.ABANDONED
                and order.spreads_filled == 0
                and order.broker_order_id is None
            ):
                # Reconciliation proved this intent never reached the broker.
                continue
            covered = sum(
                row.spreads
                for row in self.journal.spread_fills_for(order.client_order_id)
                if row.confirmed_at is not None
            )
            if (
                all_orders
                or not order.status.is_terminal
                or order.spreads_filled > covered + 1e-8
                or (
                    order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
                    and (order.spreads_filled <= 0 or order.net_price_per_spread is None)
                )
            ):
                missing.append(order)
        if missing:
            after = min(after, min(order.intent_at for order in missing) - timedelta(days=1))
        if previous_until is None and known:
            after = min(after, min(order.intent_at for order in known) - timedelta(days=1))
        until, cursor = now, None
        if not all_orders and isinstance(checkpoint.get("next_page_token"), str):
            cursor = str(checkpoint["next_page_token"])
            after = _timestamp(checkpoint.get("after")) or after
            until = _timestamp(checkpoint.get("until")) or until
        batch = self.reader.fetch(
            after=after,
            until=until,
            page_token=cursor,
            max_pages=self.max_pages,
            page_size=self.page_size,
        )
        issues: Counter[str] = Counter()
        issues["parent_identity_pending"] += sum(order.broker_order_id is None for order in missing)
        if not issues["parent_identity_pending"]:
            del issues["parent_identity_pending"]
        if not batch.complete:
            issues["pagination_pending"] += 1

        child_map: dict[str, tuple[str, str]] = {}
        for row in raw_rows:
            children = _body(row.detail).get("child_order_ids")
            if row.client_order_id and isinstance(children, dict):
                for identity, symbol in children.items():
                    if isinstance(identity, str) and isinstance(symbol, str):
                        child_map[identity] = (row.client_order_id, symbol)
        by_client = {order.client_order_id: order for order in known}
        snapshots: dict[str, Mapping[str, object]] = {}
        children_by_client: dict[str, dict[str, str]] = {}
        parent_evidence: dict[str, ExecutionOrderEvidence] = {}

        def read_parent(order: OrderRecord) -> None:
            if order.client_order_id in snapshots or not order.broker_order_id:
                return
            snapshot = self.reader.get_order(order.broker_order_id)
            if (
                snapshot.get("id") != order.broker_order_id
                or snapshot.get("client_order_id") != order.client_order_id
                or str(snapshot.get("order_class", "")).lower() != "mleg"
            ):
                raise ExecutionReadError("Nested parent identity differs from the journal")
            expected = {leg.occ_symbol: leg for leg in self.journal.legs_for(order.client_order_id)}
            raw_legs = snapshot.get("legs")
            if not isinstance(raw_legs, list) or len(raw_legs) != len(expected):
                raise ExecutionReadError("Nested order does not expose every intent leg")
            children: dict[str, str] = {}
            for child in raw_legs:
                if not isinstance(child, Mapping):
                    raise ExecutionReadError("Nested leg is unreadable")
                identity, symbol, side = child.get("id"), child.get("symbol"), child.get("side")
                if not isinstance(identity, str) or not identity or symbol not in expected:
                    raise ExecutionReadError("Nested leg identity differs from intent")
                assert isinstance(symbol, str)
                intent = expected[symbol]
                if (
                    side != intent.side
                    or _number(child.get("ratio_qty")) != intent.ratio_qty
                    or child.get("position_intent") != intent.position_intent
                ):
                    raise ExecutionReadError("Nested leg side or ratio differs from intent")
                mapping = (order.client_order_id, symbol)
                if identity in child_map and child_map[identity] != mapping:
                    raise ExecutionReadError("Child identity maps to different parents")
                child_map[identity], children[identity] = mapping, symbol
            if set(children.values()) != set(expected):
                raise ExecutionReadError("Nested children do not cover intent")
            quantity, ordered = _number(snapshot.get("filled_qty")), _number(snapshot.get("qty"))
            if ordered != _number(order.spreads_ordered) or quantity > ordered:
                raise ExecutionReadError("Parent quantities differ from intent units")
            price = (
                None
                if snapshot.get("filled_avg_price") is None
                else _number(snapshot["filled_avg_price"], signed=True)
            )
            status = OrderStatus.from_broker(str(snapshot.get("status", "")))
            if order.status.is_terminal and status is not order.status:
                raise ExecutionReadError("Terminal parent statuses disagree")
            if quantity < _number(order.spreads_filled):
                raise ExecutionReadError("Cumulative quantity moved backwards")
            if status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and quantity <= 0:
                issues["parent_quantity_pending"] += 1
            if quantity > 0 and price is None:
                issues["parent_price_missing"] += 1
            snapshots[order.client_order_id] = snapshot
            children_by_client[order.client_order_id] = children
            parent_evidence[order.client_order_id] = ExecutionOrderEvidence(
                order.client_order_id,
                order.broker_order_id,
                status,
                float(quantity),
                None if price is None else float(price),
            )

        targets = {order.client_order_id for order in missing}
        targets.update(
            child_map[row.broker_order_id][0]
            for row in batch.activities
            if row.broker_order_id in child_map
        )
        for order in known:
            if order.client_order_id in targets or order.intent_at >= after:
                read_parent(order)
        if any(row.is_option and row.broker_order_id not in child_map for row in batch.activities):
            # Exact nested child identities prove attribution; symbols do not.
            for order in known:
                read_parent(order)

        evidence: dict[str, ExecutionLegEvidence] = {}
        for row in raw_rows:
            detail = _body(row.detail)
            if detail.get("kind") == RAW_KIND:
                evidence[row.fill_id] = ExecutionLegEvidence(
                    row.fill_id,
                    str(detail.get("broker_order_id", "")),
                    row.client_order_id,
                    row.occ_symbol,
                    row.side,
                    row.contracts,
                    row.premium_per_contract,
                    row.occurred_at,
                    {key: value for key, value in detail.items() if key != "kind"},
                )
        incoming: list[ExecutionLegEvidence] = []
        for activity in batch.activities:
            if not activity.is_option:
                continue
            binding = child_map.get(activity.broker_order_id)
            if binding is None:
                issues["unmapped_option_activity"] += 1
                continue
            client_id, symbol = binding
            order = by_client[client_id]
            if activity.symbol != symbol:
                issues["activity_symbol_conflict"] += 1
                continue
            activity_intent = next(
                leg for leg in self.journal.legs_for(client_id) if leg.occ_symbol == symbol
            )
            if activity.normalized_side != activity_intent.side:
                issues["activity_side_conflict"] += 1
                continue
            if activity.price <= 0:
                issues["zero_premium_unsupported"] += 1
                continue
            if client_id not in snapshots:
                read_parent(order)
            snapshot = snapshots[client_id]
            submitted = _timestamp(snapshot.get("submitted_at")) or _timestamp(
                snapshot.get("created_at")
            )
            if submitted is not None and activity.occurred_at < submitted - timedelta(seconds=1):
                issues["activity_time_conflict"] += 1
                continue
            metadata: dict[str, object] = {
                "broker_order_id": activity.broker_order_id,
                "parent_broker_order_id": order.broker_order_id,
                "child_order_ids": children_by_client[client_id],
                "raw_side": activity.side,
                "raw_quantity": str(activity.quantity),
                "raw_price": str(activity.price),
                "transaction_time": activity.transaction_time,
                "execution_type": activity.execution_type,
                "cumulative_quantity": (
                    None
                    if activity.cumulative_quantity is None
                    else str(activity.cumulative_quantity)
                ),
                "leaves_quantity": (
                    None if activity.leaves_quantity is None else str(activity.leaves_quantity)
                ),
                "group_id": activity.group_id,
            }
            leg = ExecutionLegEvidence(
                activity.activity_id,
                activity.broker_order_id,
                client_id,
                activity.symbol,
                activity.normalized_side,
                float(activity.quantity),
                float(activity.price),
                activity.occurred_at,
                metadata,
            )
            old = raw_by_id.get(activity.activity_id)
            if old is not None and (
                old.occ_symbol != leg.occ_symbol
                or old.side != leg.side
                or old.contracts != leg.contracts
                or old.premium_per_contract != leg.premium_per_contract
                or old.occurred_at != leg.occurred_at
                or old.client_order_id not in (None, client_id)
                or _body(old.detail).get("broker_order_id") not in (None, leg.broker_order_id)
            ):
                issues["activity_replay_conflict"] += 1
                continue
            evidence[leg.activity_id] = leg
            incoming.append(leg)

        per_order: dict[str, list[ExecutionLegEvidence]] = defaultdict(list)
        for leg in evidence.values():
            if leg.client_order_id is not None:
                per_order[leg.client_order_id].append(leg)
        groups: list[DerivedSpreadEvidence] = []
        verified: list[ExecutionOrderEvidence] = []
        reconciled_spreads = Decimal(0)
        for client_id, parent in parent_evidence.items():
            order = by_client[client_id]
            intent_legs = self.journal.legs_for(client_id)
            rows = per_order.get(client_id, [])
            raw_children = snapshots[client_id]["legs"]
            assert isinstance(raw_children, list)
            snapshot_legs = {
                str(child["symbol"]): child for child in raw_children if isinstance(child, Mapping)
            }
            cumulative, full = _number(parent.spreads_filled), True
            if (
                parent.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
                and cumulative <= 0
            ):
                continue
            for expected_leg in intent_legs:
                actual = sum(
                    (
                        _number(row.contracts)
                        for row in rows
                        if row.occ_symbol == expected_leg.occ_symbol
                    ),
                    Decimal(0),
                )
                expected = _number(snapshot_legs[expected_leg.occ_symbol].get("filled_qty"))
                if actual > 0 and actual == expected:
                    leg_cash = sum(
                        (
                            _number(row.contracts) * _number(row.premium_per_contract)
                            for row in rows
                            if row.occ_symbol == expected_leg.occ_symbol
                        ),
                        Decimal(0),
                    )
                    leg_average = snapshot_legs[expected_leg.occ_symbol].get("filled_avg_price")
                    if leg_average is None:
                        issues["leg_price_pending"] += 1
                        full = False
                    elif abs(leg_cash / actual - _number(leg_average)) > PRICE_EPSILON:
                        issues["leg_price_conflict"] += 1
                        full = False
                if actual != cumulative * expected_leg.ratio_qty or actual != expected:
                    code = "leg_quantity_pending" if actual < expected else "leg_quantity_conflict"
                    issues[code] += 1
                    full = False
            if not full:
                continue
            cash = sum(
                (
                    _number(row.contracts)
                    * _number(row.premium_per_contract)
                    * (1 if row.side == "buy" else -1)
                    for row in rows
                ),
                Decimal(0),
            )
            if cumulative > 0 and (
                parent.net_price_per_spread is None
                or abs(cash / cumulative - _number(parent.net_price_per_spread, signed=True))
                > PRICE_EPSILON
            ):
                issues["parent_price_conflict"] += 1
                continue
            days: dict[date, list[ExecutionLegEvidence]] = defaultdict(list)
            for component in rows:
                days[trading_day_of(component.occurred_at)].append(component)
            daily_groups: list[DerivedSpreadEvidence] = []
            for day, components in days.items():
                quantities = [
                    sum(
                        (
                            _number(row.contracts)
                            for row in components
                            if row.occ_symbol == leg.occ_symbol
                        ),
                        Decimal(0),
                    )
                    / leg.ratio_qty
                    for leg in intent_legs
                ]
                if len(set(quantities)) != 1 or quantities[0] <= 0:
                    issues["daily_ratio_pending"] += 1
                    full = False
                    continue
                spreads, ids = quantities[0], tuple(sorted(row.activity_id for row in components))
                existing = [
                    fill
                    for fill in self.journal.spread_fills_for(client_id)
                    if fill.trading_day == day and _body(fill.detail).get("kind") == GROUP_KIND
                ]
                if len(existing) > 1:
                    issues["aggregate_identity_conflict"] += 1
                    full = False
                    continue
                anchor = existing[0].fill_id if existing else ids[0]
                if anchor not in ids:
                    issues["aggregate_identity_conflict"] += 1
                    full = False
                    continue
                daily_cash = sum(
                    (
                        _number(row.contracts)
                        * _number(row.premium_per_contract)
                        * (1 if row.side == "buy" else -1)
                        for row in components
                    ),
                    Decimal(0),
                )
                daily_groups.append(
                    DerivedSpreadEvidence(
                        anchor,
                        client_id,
                        order.symbol,
                        float(spreads),
                        float(daily_cash / spreads),
                        max(row.occurred_at for row in components),
                        ids,
                    )
                )
            if full:
                groups.extend(daily_groups)
                verified.append(parent)
                reconciled_spreads += cumulative

        conflict = any("conflict" in code for code in issues)
        status = "conflict" if conflict else "pending" if issues else "complete"
        new_legs = sum(leg.activity_id not in raw_by_id for leg in incoming)
        new_groups, updated_groups = 0, 0
        for group in groups:
            previous_group = self.journal.spread_fill(group.anchor_activity_id)
            if previous_group is None:
                new_groups += 1
            elif (
                previous_group.spreads != group.spreads
                or previous_group.net_price_per_spread != group.net_price_per_spread
                or set(_body(previous_group.detail).get("activity_ids", []))
                != set(group.activity_ids)
            ):
                updated_groups += 1
        closures = plan_execution_closures(self.journal, groups, tuple(evidence.values()))
        rejected_activity = any(
            code.startswith("activity_")
            or code in ("unmapped_option_activity", "zero_premium_unsupported")
            for code in issues
        )
        summary: dict[str, object] = {
            "position_exits_confirmed": len(closures),
            "filled_orders_reconciled": sum(parent.spreads_filled > 0 for parent in verified),
            "status": status,
            "after": _iso(after),
            "until": _iso(until),
            "activities": len(batch.activities),
            "pages": batch.pages,
            "leg_events": new_legs,
            "derived_groups": new_groups,
            "updated_groups": updated_groups,
            "orders_reconciled": len(verified),
            "reconciled_spreads": str(reconciled_spreads),
            "issues": [{"code": code, "count": count} for code, count in sorted(issues.items())],
            "next_page_token": cursor if (rejected_activity or conflict) else batch.next_page_token,
        }
        return IngestionPlan(tuple(incoming), tuple(groups), tuple(verified), summary, closures)

    def sync(self, now: datetime, *, all_orders: bool = False) -> IngestionPlan:
        """Apply a validated read, or persist a safe failure receipt."""
        try:
            plan = self.plan(now, all_orders=all_orders)
            if plan.summary["status"] == "conflict":
                self.journal.record_execution_ingestion_status(plan.summary, at=now)
            else:
                apply_execution_plan(self.journal, plan, at=now)
            return plan
        except Exception:
            summary: dict[str, object] = {
                "status": "failed",
                "issues": [{"code": "execution_read_or_apply_failed", "count": 1}],
                "activities": 0,
                "pages": 0,
                "leg_events": 0,
                "derived_groups": 0,
                "orders_reconciled": 0,
            }
            self.journal.record_execution_ingestion_status(summary, at=now)
            return IngestionPlan((), (), (), summary)


def apply_execution_plan(journal: Journal, plan: IngestionPlan, *, at: datetime) -> None:
    """Commit all evidence atomically; immutable-ID or ratio conflicts roll back."""
    if plan.summary["status"] == "conflict":
        raise JournalError("Conflicting evidence cannot be applied")
    recorded = _iso(at)
    with journal._transaction() as cursor:
        for parent in plan.orders:
            row = cursor.execute(
                "SELECT * FROM orders WHERE client_order_id=?", (parent.client_order_id,)
            ).fetchone()
            if row is None:
                raise JournalError("Execution parent is no longer journalled")
            old = _to_order(row)
            if old.broker_order_id != parent.broker_order_id:
                raise JournalError("Broker parent identity changed before apply")
            if old.status.is_terminal and old.status is not parent.status:
                raise JournalError("Terminal parent status changed before apply")
            filled = journal._checked_filled(old, parent.spreads_filled)
            if (filled, old.net_price_per_spread, old.status) != (
                old.spreads_filled,
                parent.net_price_per_spread,
                parent.status,
            ):
                cursor.execute(
                    "UPDATE orders SET status=?,spreads_filled=?,net_price_per_spread=?,"
                    "status_at=?,reconciled_at=? WHERE client_order_id=?",
                    (
                        str(parent.status),
                        filled,
                        parent.net_price_per_spread,
                        recorded,
                        recorded,
                        parent.client_order_id,
                    ),
                )
        for leg in plan.legs:
            if leg.contracts <= 0 or leg.premium_per_contract <= 0:
                raise JournalError("Unsupported nonpositive leg activity")
            if not all(math.isfinite(value) for value in (leg.contracts, leg.premium_per_contract)):
                raise JournalError("Nonfinite leg activity")
            if leg.client_order_id is None or leg.side not in ("buy", "sell"):
                raise JournalError("Option activity lacks verified parent binding")
            intent = {
                item.occ_symbol: item for item in journal._legs(cursor, leg.client_order_id)
            }.get(leg.occ_symbol)
            if intent is None or intent.side != leg.side:
                raise JournalError("Raw activity does not match intent leg")
            detail = _dumps_obj({"kind": RAW_KIND, **leg.metadata}, what="leg provenance")
            existing = cursor.execute(
                "SELECT * FROM leg_fills WHERE fill_id=?", (leg.activity_id,)
            ).fetchone()
            if existing is not None:
                old_leg = _to_leg_fill(existing)
                if old_leg.client_order_id not in (None, leg.client_order_id) or (
                    old_leg.occ_symbol,
                    old_leg.side,
                    old_leg.contracts,
                    old_leg.premium_per_contract,
                    old_leg.occurred_at,
                ) != (
                    leg.occ_symbol,
                    leg.side,
                    leg.contracts,
                    leg.premium_per_contract,
                    leg.occurred_at,
                ):
                    raise JournalError("Real execution ID changed immutable facts")
                old_metadata = _body(old_leg.detail)
                if old_metadata.get("kind") == RAW_KIND and old_metadata != json.loads(detail):
                    raise JournalError("Real execution ID changed provenance")
                cursor.execute(
                    "UPDATE leg_fills SET client_order_id=?,source=?,"
                    "confirmed_at=COALESCE(confirmed_at,?),detail=? WHERE fill_id=?",
                    (leg.client_order_id, str(FillSource.REST), recorded, detail, leg.activity_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO leg_fills(fill_id,parent_fill_id,client_order_id,occ_symbol,"
                    "trading_day,occurred_at,contracts,premium_per_contract,side,source,"
                    "confirmed_at,recorded_at,detail) VALUES (?,NULL,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        leg.activity_id,
                        leg.client_order_id,
                        leg.occ_symbol,
                        trading_day_of(leg.occurred_at).isoformat(),
                        _iso(leg.occurred_at),
                        leg.contracts,
                        leg.premium_per_contract,
                        leg.side,
                        str(FillSource.REST),
                        recorded,
                        recorded,
                        detail,
                    ),
                )

        for group in plan.groups:
            if (
                not group.activity_ids
                or len(set(group.activity_ids)) != len(group.activity_ids)
                or group.anchor_activity_id not in group.activity_ids
                or group.spreads <= 0
            ):
                raise JournalError("Derived group must retain real constituent identities")
            rows = [
                cursor.execute("SELECT * FROM leg_fills WHERE fill_id=?", (identity,)).fetchone()
                for identity in group.activity_ids
            ]
            if any(row is None for row in rows):
                raise JournalError("Derived group lacks raw constituents")
            components = [_to_leg_fill(row) for row in rows if row is not None]
            day = trading_day_of(group.occurred_at)
            if (
                any(
                    leg.client_order_id != group.client_order_id
                    or leg.confirmed_at is None
                    or trading_day_of(leg.occurred_at) != day
                    or leg.parent_fill_id not in (None, group.anchor_activity_id)
                    for leg in components
                )
                or max(leg.occurred_at for leg in components) != group.occurred_at
            ):
                raise JournalError("Derived group attribution or execution time is inconsistent")
            group_legs = journal._legs(cursor, group.client_order_id)
            if {leg.occ_symbol for leg in components} != {leg.occ_symbol for leg in group_legs}:
                raise JournalError("Derived group does not cover intent legs")
            cash = 0.0
            for expected in group_legs:
                matching = [leg for leg in components if leg.occ_symbol == expected.occ_symbol]
                if (
                    any(leg.side != expected.side for leg in matching)
                    or abs(
                        sum(leg.contracts for leg in matching) - group.spreads * expected.ratio_qty
                    )
                    > 1e-8
                ):
                    raise JournalError("Derived group is not ratio-balanced")
                cash += sum(
                    leg.contracts * leg.premium_per_contract * (1 if leg.side == "buy" else -1)
                    for leg in matching
                )
            if abs(cash / group.spreads - group.net_price_per_spread) > 1e-8:
                raise JournalError("Derived price differs from actual leg cash flow")
            row = cursor.execute(
                "SELECT * FROM spread_fills WHERE fill_id=?", (group.anchor_activity_id,)
            ).fetchone()
            if row is not None:
                old_group, previous = _to_spread_fill(row), _body(row["detail"])
                if (
                    previous.get("kind") != GROUP_KIND
                    or old_group.client_order_id != group.client_order_id
                    or old_group.trading_day != day
                    or not set(previous.get("activity_ids", [])).issubset(group.activity_ids)
                    or old_group.spreads > group.spreads
                ):
                    raise JournalError("Derived group cannot replace unrelated evidence")
            journal._check_fill_against_order(
                cursor,
                client_order_id=group.client_order_id,
                fill_id=group.anchor_activity_id,
                spreads=group.spreads,
                net_price_per_spread=group.net_price_per_spread,
            )
            detail = _dumps_obj(
                {
                    "kind": GROUP_KIND,
                    "activity_ids": list(group.activity_ids),
                    "anchor_is_constituent_activity": True,
                    "timestamp_semantics": "latest_component_execution_time",
                    "raw_activity_count": len(group.activity_ids),
                },
                what="derived provenance",
            )
            parent_row = cursor.execute(
                "SELECT broker_order_id FROM orders WHERE client_order_id=?",
                (group.client_order_id,),
            ).fetchone()
            cursor.execute(
                "INSERT INTO spread_fills(fill_id,client_order_id,broker_order_id,symbol,"
                "trading_day,occurred_at,spreads,net_price_per_spread,source,attribution,"
                "confirmed_at,recorded_at,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(fill_id) DO UPDATE SET occurred_at=excluded.occurred_at,"
                "spreads=excluded.spreads,net_price_per_spread=excluded.net_price_per_spread,"
                "confirmed_at=excluded.confirmed_at,detail=excluded.detail",
                (
                    group.anchor_activity_id,
                    group.client_order_id,
                    parent_row["broker_order_id"],
                    group.symbol,
                    day.isoformat(),
                    _iso(group.occurred_at),
                    group.spreads,
                    group.net_price_per_spread,
                    str(FillSource.REST),
                    str(FillAttribution.JOURNALLED),
                    recorded,
                    recorded,
                    detail,
                ),
            )
            for identity in group.activity_ids:
                cursor.execute(
                    "UPDATE leg_fills SET parent_fill_id=? WHERE fill_id=?",
                    (group.anchor_activity_id, identity),
                )
        persisted_activities = []
        for raw_row in journal.execution_leg_rows():
            metadata = _body(raw_row.detail)
            if metadata.get("kind") == RAW_KIND:
                persisted_activities.append(
                    ExecutionLegEvidence(
                        raw_row.fill_id,
                        str(metadata["broker_order_id"]),
                        raw_row.client_order_id,
                        raw_row.occ_symbol,
                        raw_row.side,
                        raw_row.contracts,
                        raw_row.premium_per_contract,
                        raw_row.occurred_at,
                        {key: value for key, value in metadata.items() if key != "kind"},
                    )
                )
        valid = plan_execution_closures(journal, plan.groups, persisted_activities)
        for closure in plan.closures:
            # Re-evaluate against the same writer transaction to reject stale
            # source snapshots or source activities before applying the plan.
            if closure not in valid:
                raise JournalError("Closure source evidence changed before apply")
            detail = _dumps_obj(
                {
                    "kind": "alpaca_execution_closure_v1",
                    "closing_client_order_id": closure.closing_client_order_id,
                    "opening_client_order_id": closure.opening_client_order_id,
                    "source_snapshot_id": closure.source_snapshot_id,
                    "source_activity_ids": list(closure.source_activity_ids),
                    "latest_component_execution_time": _iso(closure.occurred_at),
                },
                what="execution closure provenance",
            )
            cursor.execute(
                "UPDATE position_events SET cause=?,evidence=?,activity_id=?,confirmed_at=?,"
                "detail=? WHERE id=? AND cause=? AND activity_id IS NULL",
                (
                    "closed_by_us",
                    "confirmed_by_activity",
                    closure.activity_id,
                    recorded,
                    detail,
                    closure.position_event_id,
                    "unknown",
                ),
            )
            if cursor.rowcount != 1:
                raise JournalError("Closure event changed before apply")
        cursor.execute(
            "INSERT INTO reconciliations(at,scope,ok,detail) VALUES (?,?,?,?)",
            (
                recorded,
                str(ReconciliationScope.ACTIVITIES),
                int(plan.complete),
                _dumps_obj({"kind": RECEIPT_KIND, **plan.summary}, what="execution receipt"),
            ),
        )


@dataclass(slots=True)
class RecordedExecutionReader:
    """An explicit offline capture for review and reproducible replay."""

    activities: tuple[ExecutionActivity, ...]
    orders: Mapping[str, Mapping[str, object]]

    @classmethod
    def from_file(cls, path: Path) -> RecordedExecutionReader:
        body = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            tuple(parse_execution_activity(row) for row in body["activities"]),
            {row["id"]: row for row in body["broker_orders"]},
        )

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
        # A supplied capture is explicit; execution time cannot emulate creation-time filters.
        del after, until
        rows = self.activities
        if order_id is not None:
            rows = tuple(row for row in rows if row.broker_order_id == order_id)
        start = 0
        if page_token is not None:
            ids = [row.activity_id for row in rows]
            if page_token not in ids:
                raise ExecutionReadError("Captured pagination token is absent")
            start = ids.index(page_token) + 1
        count = max_pages * page_size
        selected, complete = rows[start : start + count], start + count >= len(rows)
        return ActivityBatch(
            selected,
            max(1, (len(selected) + page_size - 1) // page_size),
            complete,
            None if complete else selected[-1].activity_id,
        )

    def get_order(self, order_id: str) -> Mapping[str, object]:
        if order_id not in self.orders:
            raise ExecutionReadError("Parent absent from captured evidence")
        return self.orders[order_id]


def _readonly_journal_copy(path: Path) -> Journal:
    if not path.is_file():
        raise ValueError("Input journal must already exist")
    copy = Journal(MEMORY)
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source:
            source.backup(copy._conn)
        return copy
    except BaseException:
        copy.close()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan or apply paper execution reconciliation.")
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--records", type=Path, help="private captured broker activity/order JSON")
    parser.add_argument("--apply", action="store_true", help="commit evidence; default only plans")
    parser.add_argument("--until", help="aware ISO timestamp; defaults to current UTC")
    args = parser.parse_args()
    now = _timestamp(args.until) if args.until else datetime.now(UTC)
    assert now is not None
    reader: ExecutionReader
    if args.records:
        reader = RecordedExecutionReader.from_file(args.records)
    else:
        from underwriter.config import Settings

        settings = Settings()
        reader = paper_activity_reader(
            settings.alpaca_api_key.get_secret_value(),
            settings.alpaca_secret_key.get_secret_value(),
        )
    if args.apply and not args.journal.is_file():
        parser.error("--apply requires an existing journal")
    journal = Journal(args.journal) if args.apply else _readonly_journal_copy(args.journal)
    try:
        ingestor = ExecutionIngestor(journal, reader)
        plan = (
            ingestor.sync(now, all_orders=True)
            if args.apply
            else ingestor.plan(now, all_orders=True)
        )
        public = {
            key: value
            for key, value in plan.summary.items()
            if key not in ("next_page_token", "unpersisted_activities")
        }
        public["mode"] = "apply" if args.apply else "plan"
        print(json.dumps(public, sort_keys=True))  # noqa: T201 - explicit CLI output
        return 0 if plan.complete else 1
    finally:
        journal.close()


if __name__ == "__main__":
    raise SystemExit(main())
