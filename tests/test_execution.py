"""Execution adapter tests.

Weighted heavily toward the paths where money is lost silently. Two of them
matter more than the rest:

- **The ambiguous outcome.** A timeout, an unreadable response, or an
  unrecognised CLI error must never produce a second order unless the broker
  has positively confirmed the first does not exist. Several tests here exist
  purely to count submissions.
- **The payload.** A spread submitted with the wrong limit price sign, a
  top-level symbol, or a market type does not error -- it fills, wrongly. So
  the payload is asserted byte for byte rather than field by field.

No network, no credentials, no subprocess. The CLI is a scripted fake runner
and the SDK is a scripted fake client.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from underwriter.chain import Contract, ContractType, CreditSpread, Quote
from underwriter.config import LiveTradingBlocked
from underwriter.execution import (
    DRY_RUN_STATUS,
    Backend,
    BackendOutcome,
    CliBackend,
    CompletedCommand,
    ExecutionAdapter,
    Kind,
    MultiLegOrder,
    OrderLeg,
    PositionIntent,
    Reason,
    SdkBackend,
    Side,
    _AbsenceProof,
    _resubmit,
    assert_paper_only,
    build_adapter,
    build_closing_order,
    build_opening_order,
    client_order_id,
    paper_environment,
    reduce_ratios,
    to_limit_price,
    validate,
)

SHORT_SYMBOL = "XLE260918P00082000"
LONG_SYMBOL = "XLE260918P00080000"
NOW = datetime(2026, 8, 28, 15, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_live_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from an environment that cannot route to live."""
    monkeypatch.delenv("ALPACA_LIVE_TRADE", raising=False)


def contract(symbol: str, strike: float) -> Contract:
    return Contract(
        symbol=symbol,
        underlying="XLE",
        expiry=date(2026, 9, 18),
        strike=strike,
        contract_type=ContractType.PUT,
        quote=Quote(bid=1.0, ask=1.1, as_of=NOW),
    )


def spread(credit: float = 0.4278) -> CreditSpread:
    return CreditSpread(
        short_leg=contract(SHORT_SYMBOL, 82.0),
        long_leg=contract(LONG_SYMBOL, 80.0),
        credit=credit,
        contract_type=ContractType.PUT,
    )


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


@dataclass
class Invocation:
    argv: tuple[str, ...]
    env: dict[str, str]


@dataclass
class FakeCli:
    """A scripted `alpaca` binary.

    Responses are queued per subcommand so a test reads as "the submit times
    out, then the lookup 404s" rather than as an opaque list. Running out of
    scripted responses is an error: it means the code under test made a call
    the test did not expect, which is exactly the bug these tests hunt.
    """

    submit_responses: list[CompletedCommand] = field(default_factory=list)
    lookup_responses: list[CompletedCommand] = field(default_factory=list)
    submits: list[Invocation] = field(default_factory=list)
    lookups: list[Invocation] = field(default_factory=list)
    # Optional sink recording the interleaving of calls, for tests that care
    # about ordering rather than counts.
    on_call: Callable[[str], None] | None = None

    def __call__(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str]
    ) -> CompletedCommand:
        call = Invocation(argv=tuple(argv), env=dict(env))
        kind = "submit" if "submit" in call.argv else "lookup"
        if self.on_call is not None:
            self.on_call(kind)
        if kind == "submit":
            self.submits.append(call)
            queue, responses = self.submits, self.submit_responses
        else:
            self.lookups.append(call)
            queue, responses = self.lookups, self.lookup_responses
        index = len(queue) - 1
        if index >= len(responses):
            msg = f"unscripted CLI call: {call.argv}"
            raise AssertionError(msg)
        return responses[index]


def cli_ok(
    order_id: str = "ord-1", status: str = "accepted", client_id: str = "cid"
) -> CompletedCommand:
    body = {"id": order_id, "client_order_id": client_id, "status": status}
    return CompletedCommand(returncode=0, stdout=json.dumps(body))


def cli_api_error(status: int, error: str = "boom") -> CompletedCommand:
    body = {"error": error, "code": 0, "status": status, "hint": ""}
    return CompletedCommand(returncode=1, stderr=json.dumps(body))


CLI_TIMEOUT = CompletedCommand(returncode=-1, timed_out=True)
CLI_AUTH = CompletedCommand(returncode=2, stderr='{"error":"unauthorized","status":401}')
CLI_UNSTRUCTURED = CompletedCommand(returncode=1, stderr="Error: unknown flag: --legs\nUsage:\n")


class ReadTimeout(Exception):
    """Stands in for the SDK transport timeout, matched on class name."""


class FakeApiError(Exception):
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message or f"status {status_code}")
        self.status_code = status_code


@dataclass
class FakeSdk:
    post_responses: list[object] = field(default_factory=list)
    get_responses: list[object] = field(default_factory=list)
    posts: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)
    gets: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)

    def post(self, path: str, data: dict[str, object] | None = None) -> object:
        self.posts.append((path, data))
        return _next(self.post_responses, len(self.posts) - 1, path)

    def get(self, path: str, data: dict[str, object] | None = None) -> object:
        self.gets.append((path, data))
        return _next(self.get_responses, len(self.gets) - 1, path)


def _next(responses: list[object], index: int, path: str) -> object:
    if index >= len(responses):
        msg = f"unscripted SDK call: {path}"
        raise AssertionError(msg)
    value = responses[index]
    if isinstance(value, Exception):
        raise value
    return value


SDK_ORDER = {"id": "sdk-1", "client_order_id": "cid", "status": "accepted"}


def adapter(
    cli: FakeCli, *, sdk: FakeSdk | None = None, binary: str = "alpaca"
) -> ExecutionAdapter:
    """A CLI-backed adapter, with an SDK fallback only when a test asks for one.

    `unavailable_reason` shells out to `shutil.which`, so the default binary is
    one that exists on any machine running these tests.
    """
    return ExecutionAdapter(
        primary=CliBackend(binary=binary, runner=cli, timeout=5.0),
        fallback=None if sdk is None else SdkBackend(client=sdk),
    )


# --------------------------------------------------------------------------


class TestPaperOnly:
    @pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on", "maybe", " TRUE "])
    def test_any_live_flag_refuses_to_execute(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("ALPACA_LIVE_TRADE", value)
        with pytest.raises(LiveTradingBlocked):
            assert_paper_only()

    @pytest.mark.parametrize("value", ["", "false", "FALSE", "0", "no", "off"])
    def test_unambiguously_false_values_are_permitted(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("ALPACA_LIVE_TRADE", value)
        assert_paper_only()

    def test_submission_is_blocked_before_any_backend_is_touched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = FakeCli(submit_responses=[cli_ok()])
        order = build_opening_order(spread(), contracts=1, now=NOW)
        monkeypatch.setenv("ALPACA_LIVE_TRADE", "true")
        with pytest.raises(LiveTradingBlocked):
            adapter(cli).submit(order)
        assert cli.submits == []

    def test_child_environment_is_forced_to_paper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even if something upstream set it, the CLI never sees a live value.
        monkeypatch.setenv("ALPACA_LIVE_TRADE", "true")
        assert paper_environment()["ALPACA_LIVE_TRADE"] == "false"
        assert paper_environment({"ALPACA_LIVE_TRADE": "true"})["ALPACA_LIVE_TRADE"] == "false"

    def test_extra_environment_is_passed_through(self) -> None:
        assert paper_environment({"ALPACA_API_KEY": "k"})["ALPACA_API_KEY"] == "k"

    def test_output_format_is_pinned_not_inherited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # .env can set ALPACA_OUTPUT, and csv output on the order path would
        # make a successful submission look like an unreadable response.
        monkeypatch.setenv("ALPACA_OUTPUT", "csv")
        assert paper_environment()["ALPACA_OUTPUT"] == "json"

    def test_neither_csv_nor_jq_is_ever_passed(self) -> None:
        # --csv combined with --jq prints nothing at all and still exits 0.
        cli = FakeCli(submit_responses=[cli_ok()], lookup_responses=[cli_ok()])
        order = build_opening_order(spread(), contracts=1, now=NOW)
        backend = CliBackend(runner=cli, timeout=5.0)
        backend.submit(order)
        backend.lookup(order.client_order_id)
        for call in [*cli.submits, *cli.lookups]:
            assert "--csv" not in call.argv
            assert "--jq" not in call.argv
            assert call.env["ALPACA_OUTPUT"] == "json"


class TestLimitPriceSign:
    """Pinned convention. Do not "fix" these tests -- read the reason first.

    For `mleg`, `limit_price` is the SIGNED NET price. Verbatim from the OAS
    `CreateOrderRequest.limit_price` description: "A positive value indicates a
    debit, representing a cost or payment to be made. A negative value
    signifies a credit, reflecting an amount to be received."

    So a put credit spread we expect to collect $1.20 for is submitted as
    "-1.20", and closing it flips positive because buying the spread back is a
    debit. Corroborated by a filled mleg parent's `filled_avg_price` equalling
    the signed net of its legs, and by the Level 3 cost-basis example stating
    that a $5 credit "becomes -$5 in the order's net debit/credit calculation".
    The all-positive examples on the Level 3 page are debit structures and
    rolls; none of them exercises the credit case, so none of them contradicts
    this.

    Getting it backwards does not raise. A positive limit on a credit spread
    reads as "I will pay $1.20 to enter this", which is plausibly filled and
    visible only as inexplicable P&L. See docs/GOTCHAS.md #7.
    """

    def test_a_credit_of_one_twenty_is_submitted_as_negative_one_twenty(self) -> None:
        assert to_limit_price(1.20, credit=True) == Decimal("-1.20")

    def test_the_credit_convention_reaches_the_wire(self) -> None:
        # The pinning test that matters: not the helper, the payload.
        order = build_opening_order(spread(credit=1.20), contracts=1, now=NOW)
        assert order.as_payload()["limit_price"] == "-1.20"

    def test_closing_that_same_spread_flips_positive(self) -> None:
        order = build_closing_order(spread(credit=1.20), contracts=1, debit=0.60, now=NOW)
        assert order.as_payload()["limit_price"] == "0.60"

    def test_there_is_no_way_to_configure_the_sign(self) -> None:
        # A switch here would only ever be a way to get it wrong.
        import underwriter.execution as execution

        assert not hasattr(execution, "LimitPriceConvention")

    def test_a_debit_is_positive(self) -> None:
        assert to_limit_price(1.20, credit=False) == Decimal("1.20")

    def test_a_credit_rounds_toward_less_premium(self) -> None:
        # 0.4278 collected becomes a demand for 0.42, never 0.43: we ask for
        # slightly less than modelled, so the fill can only beat the model.
        assert to_limit_price(0.4278, credit=True) == Decimal("-0.42")

    def test_a_debit_rounds_toward_paying_more(self) -> None:
        assert to_limit_price(0.4212, credit=False) == Decimal("0.43")

    def test_non_finite_input_survives_to_validation(self) -> None:
        price = to_limit_price(float("nan"), credit=True)
        assert not price.is_finite()


class TestPayloadConstruction:
    def test_opening_payload_is_exact(self) -> None:
        order = build_opening_order(spread(credit=0.4278), contracts=3, now=NOW)
        assert order.as_payload() == {
            "order_class": "mleg",
            "qty": "3",
            "type": "limit",
            "limit_price": "-0.42",
            "time_in_force": "day",
            "client_order_id": order.client_order_id,
            "legs": [
                {
                    "symbol": SHORT_SYMBOL,
                    "ratio_qty": "1",
                    "side": "sell",
                    "position_intent": "sell_to_open",
                },
                {
                    "symbol": LONG_SYMBOL,
                    "ratio_qty": "1",
                    "side": "buy",
                    "position_intent": "buy_to_open",
                },
            ],
        }

    def test_legs_json_is_byte_stable(self) -> None:
        order = build_opening_order(spread(), contracts=1, now=NOW)
        assert order.legs_json() == (
            '[{"symbol":"XLE260918P00082000","ratio_qty":"1","side":"sell",'
            '"position_intent":"sell_to_open"},'
            '{"symbol":"XLE260918P00080000","ratio_qty":"1","side":"buy",'
            '"position_intent":"buy_to_open"}]'
        )

    def test_payload_has_no_top_level_symbol_or_side(self) -> None:
        # For mleg these live on the legs; sending a top-level symbol is an
        # error. See docs/GOTCHAS.md #6.
        payload = build_opening_order(spread(), contracts=1, now=NOW).as_payload()
        assert "symbol" not in payload
        assert "side" not in payload
        assert "position_intent" not in payload

    def test_every_numeric_value_is_a_string(self) -> None:
        payload = build_opening_order(spread(), contracts=2, now=NOW).as_payload()
        legs = payload["legs"]
        assert isinstance(legs, list)
        assert isinstance(payload["qty"], str)
        assert isinstance(payload["limit_price"], str)
        assert all(isinstance(leg["ratio_qty"], str) for leg in legs)

    def test_credit_order_is_marked_as_a_credit(self) -> None:
        assert build_opening_order(spread(), contracts=1, now=NOW).is_credit

    def test_explicit_credit_overrides_the_modelled_one(self) -> None:
        order = build_opening_order(spread(credit=0.50), contracts=1, credit=0.61, now=NOW)
        assert order.as_payload()["limit_price"] == "-0.61"


class TestTimeInForce:
    def test_the_default_is_day(self) -> None:
        # Nothing should rest overnight unmonitored.
        order = build_opening_order(spread(), contracts=1, now=NOW)
        assert order.as_payload()["time_in_force"] == "day"

    def test_gtc_is_a_parameter_not_a_forbidden_value(self) -> None:
        # Both are valid for multi-leg options: there is one TimeInForce schema
        # in the spec with no mleg variant. See docs/GOTCHAS.md #11.
        order = build_opening_order(spread(), contracts=1, now=NOW, time_in_force="gtc")
        assert order.as_payload()["time_in_force"] == "gtc"
        assert validate(order) is None

    def test_gtc_reaches_the_command_line(self) -> None:
        order = build_closing_order(spread(), contracts=1, debit=0.2, now=NOW, time_in_force="gtc")
        argv = CliBackend().submit_argv(order)
        assert argv[argv.index("--time-in-force") + 1] == "gtc"


class TestFillSemantics:
    """Parent units are not leg units, and conflating them corrupts P&L."""

    def test_parent_fill_fields_are_surfaced(self) -> None:
        body = {
            "id": "ord-1",
            "client_order_id": "cid",
            "status": "filled",
            # Parent: strategy units, and the signed net per unit.
            "filled_qty": "5",
            "filled_avg_price": "-1.20",
        }
        cli = FakeCli(submit_responses=[CompletedCommand(returncode=0, stdout=json.dumps(body))])
        result = adapter(cli).submit(build_opening_order(spread(), contracts=5, now=NOW))
        assert result.ok
        assert result.filled_qty == Decimal("5")
        # Negative: a filled credit spread received money.
        assert result.filled_avg_price == Decimal("-1.20")

    def test_a_partial_fill_is_reported_as_the_broker_stated_it(self) -> None:
        # All-or-nothing binds the ratio, not the quantity: with qty 5, two
        # spreads can fill while three keep working.
        body = {
            "id": "ord-1",
            "client_order_id": "cid",
            "status": "partially_filled",
            "filled_qty": "2",
            "filled_avg_price": "-1.18",
        }
        cli = FakeCli(submit_responses=[CompletedCommand(returncode=0, stdout=json.dumps(body))])
        result = adapter(cli).submit(build_opening_order(spread(), contracts=5, now=NOW))
        assert result.ok
        assert result.status == "partially_filled"
        assert result.filled_qty == Decimal("2")

    def test_an_unreported_fill_is_none_never_zero(self) -> None:
        # Zero and "not reported" mean opposite things on a fill.
        cli = FakeCli(submit_responses=[cli_ok(status="new")])
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert result.filled_qty is None
        assert result.filled_avg_price is None

    def test_an_unparseable_fill_does_not_become_zero(self) -> None:
        body = {"id": "o", "client_order_id": "c", "status": "new", "filled_qty": "nonsense"}
        cli = FakeCli(submit_responses=[CompletedCommand(returncode=0, stdout=json.dumps(body))])
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert result.filled_qty is None


class TestClosingOrder:
    def test_intents_and_sides_both_flip(self) -> None:
        order = build_closing_order(spread(), contracts=2, debit=0.21, now=NOW)
        legs = order.legs
        assert legs[0].symbol == SHORT_SYMBOL
        assert legs[0].side is Side.BUY
        assert legs[0].position_intent is PositionIntent.BUY_TO_CLOSE
        assert legs[1].symbol == LONG_SYMBOL
        assert legs[1].side is Side.SELL
        assert legs[1].position_intent is PositionIntent.SELL_TO_CLOSE

    def test_closing_a_credit_spread_is_a_debit(self) -> None:
        order = build_closing_order(spread(), contracts=2, debit=0.21, now=NOW)
        assert order.as_payload()["limit_price"] == "0.21"
        assert not order.is_credit

    def test_closing_order_gets_a_distinct_id(self) -> None:
        opening = build_opening_order(spread(), contracts=2, now=NOW)
        closing = build_closing_order(spread(), contracts=2, debit=0.21, now=NOW)
        assert closing.client_order_id != opening.client_order_id
        assert closing.client_order_id.startswith("uw-close-XLE-20260828-")


class TestClientOrderId:
    def test_is_deterministic(self) -> None:
        first = build_opening_order(spread(), contracts=2, now=NOW)
        second = build_opening_order(spread(), contracts=2, now=NOW)
        assert first.client_order_id == second.client_order_id

    def test_a_different_size_is_a_different_order(self) -> None:
        two = build_opening_order(spread(), contracts=2, now=NOW)
        three = build_opening_order(spread(), contracts=3, now=NOW)
        assert two.client_order_id != three.client_order_id

    def test_a_different_price_is_a_different_order(self) -> None:
        a = build_opening_order(spread(), contracts=2, now=NOW)
        b = build_opening_order(spread(), contracts=2, credit=0.99, now=NOW)
        assert a.client_order_id != b.client_order_id

    def test_a_nonce_deliberately_permits_a_second_identical_position(self) -> None:
        a = build_opening_order(spread(), contracts=2, now=NOW)
        b = build_opening_order(spread(), contracts=2, now=NOW, nonce="second-position")
        assert a.client_order_id != b.client_order_id

    def test_a_different_day_is_a_different_order(self) -> None:
        a = build_opening_order(spread(), contracts=2, now=NOW)
        b = build_opening_order(
            spread(), contracts=2, now=datetime(2026, 8, 29, 15, 30, tzinfo=UTC)
        )
        assert a.client_order_id != b.client_order_id

    def test_fits_the_128_character_limit(self) -> None:
        order = build_opening_order(spread(), contracts=1, now=NOW)
        assert 0 < len(order.client_order_id) <= 128

    def test_is_readable(self) -> None:
        order = build_opening_order(spread(), contracts=1, now=NOW)
        assert order.client_order_id.startswith("uw-open-XLE-20260828-")

    def test_naive_datetime_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            client_order_id(
                action="open",
                underlying="XLE",
                legs=(),
                qty=1,
                limit_price=Decimal("-0.42"),
                now=datetime(2026, 8, 28, 15, 30),  # noqa: DTZ001 - the point of the test
            )


class TestRatioReduction:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ((1, 1), (1, 1)),
            ((4, 2), (2, 1)),
            ((3, 1), (3, 1)),
            ((6, 4, 2), (3, 2, 1)),
            ((2, 2, 2, 2), (1, 1, 1, 1)),
            ((), ()),
        ],
    )
    def test_reduces_by_gcd(self, raw: tuple[int, ...], expected: tuple[int, ...]) -> None:
        assert reduce_ratios(raw) == expected

    def test_unreduced_ratios_are_rejected_before_submission(self) -> None:
        order = MultiLegOrder(
            client_order_id="uw-open-XLE-20260828-abc",
            qty=1,
            limit_price=Decimal("-0.42"),
            legs=(
                OrderLeg(SHORT_SYMBOL, Side.SELL, PositionIntent.SELL_TO_OPEN, ratio_qty=4),
                OrderLeg(LONG_SYMBOL, Side.BUY, PositionIntent.BUY_TO_OPEN, ratio_qty=2),
            ),
        )
        problem = validate(order)
        assert problem is not None
        assert "simplest form" in problem


DEFAULT_LEGS = (
    OrderLeg(SHORT_SYMBOL, Side.SELL, PositionIntent.SELL_TO_OPEN),
    OrderLeg(LONG_SYMBOL, Side.BUY, PositionIntent.BUY_TO_OPEN),
)


def order_with(
    *,
    cid: str = "uw-open-XLE-20260828-abc",
    qty: int = 1,
    limit_price: Decimal = Decimal("-0.42"),
    legs: tuple[OrderLeg, ...] = DEFAULT_LEGS,
    time_in_force: str = "day",
) -> MultiLegOrder:
    return MultiLegOrder(
        client_order_id=cid,
        qty=qty,
        limit_price=limit_price,
        legs=legs,
        time_in_force=time_in_force,
    )


class TestValidation:
    def test_a_well_formed_order_passes(self) -> None:
        assert validate(order_with()) is None

    def test_one_leg_is_not_a_spread(self) -> None:
        problem = validate(
            order_with(legs=(OrderLeg(SHORT_SYMBOL, Side.SELL, PositionIntent.SELL_TO_OPEN),))
        )
        assert problem is not None
        assert "2-4 legs" in problem

    def test_more_than_four_legs_is_rejected(self) -> None:
        legs = tuple(
            OrderLeg(f"XLE260918P0008{i}000", Side.SELL, PositionIntent.SELL_TO_OPEN)
            for i in range(5)
        )
        problem = validate(order_with(legs=legs))
        assert problem is not None
        assert "2-4 legs" in problem

    def test_duplicate_leg_symbols_are_rejected(self) -> None:
        legs = (
            OrderLeg(SHORT_SYMBOL, Side.SELL, PositionIntent.SELL_TO_OPEN),
            OrderLeg(SHORT_SYMBOL, Side.BUY, PositionIntent.BUY_TO_OPEN),
        )
        problem = validate(order_with(legs=legs))
        assert problem is not None
        assert "unique" in problem

    @pytest.mark.parametrize("qty", [0, -1])
    def test_nonpositive_quantity_is_rejected(self, qty: int) -> None:
        problem = validate(order_with(qty=qty))
        assert problem is not None
        assert "at least 1" in problem

    def test_zero_limit_price_is_neither_debit_nor_credit(self) -> None:
        problem = validate(order_with(limit_price=Decimal("0.00")))
        assert problem is not None
        assert "zero" in problem

    def test_non_finite_limit_price_is_rejected(self) -> None:
        problem = validate(order_with(limit_price=Decimal("NaN")))
        assert problem is not None
        assert "finite" in problem

    def test_unquantised_limit_price_is_rejected(self) -> None:
        problem = validate(order_with(limit_price=Decimal("-0.4278")))
        assert problem is not None
        assert "cent" in problem

    def test_market_time_in_force_values_are_rejected(self) -> None:
        problem = validate(order_with(time_in_force="ioc"))
        assert problem is not None
        assert "time_in_force" in problem

    def test_overlong_client_order_id_is_rejected(self) -> None:
        problem = validate(order_with(cid="x" * 129))
        assert problem is not None
        assert "128" in problem

    def test_client_order_id_with_awkward_characters_is_rejected(self) -> None:
        problem = validate(order_with(cid="uw open/XLE"))
        assert problem is not None
        assert "characters" in problem

    def test_invalid_payload_never_reaches_a_backend(self) -> None:
        cli = FakeCli()
        result = adapter(cli).submit(order_with(qty=0))
        assert not result.ok
        assert result.reason is Reason.INVALID_PAYLOAD
        assert result.backend is None
        assert cli.submits == []


class TestCliArgv:
    def test_argv_is_exact(self) -> None:
        order = build_opening_order(spread(credit=0.4278), contracts=3, now=NOW)
        argv = CliBackend().submit_argv(order)
        assert argv == [
            "alpaca",
            "order",
            "submit",
            "--order-class",
            "mleg",
            "--qty",
            "3",
            "--type",
            "limit",
            "--limit-price=-0.42",
            "--time-in-force",
            "day",
            "--client-order-id",
            order.client_order_id,
            "--legs",
            order.legs_json(),
            "--quiet",
        ]

    def test_symbol_is_never_passed(self) -> None:
        argv = CliBackend().submit_argv(build_opening_order(spread(), contracts=1, now=NOW))
        assert "--symbol" not in argv

    def test_limit_type_is_explicit(self) -> None:
        # The CLI defaults to --type market. See docs/GOTCHAS.md #6.
        argv = CliBackend().submit_argv(build_opening_order(spread(), contracts=1, now=NOW))
        assert argv[argv.index("--type") + 1] == "limit"

    def test_negative_limit_price_uses_the_equals_form(self) -> None:
        # A bare "-0.42" would be parsed as a flag, not a value.
        argv = CliBackend().submit_argv(build_opening_order(spread(), contracts=1, now=NOW))
        assert "--limit-price=-0.42" in argv
        assert "-0.42" not in argv

    def test_dry_run_is_only_present_when_asked_for(self) -> None:
        order = build_opening_order(spread(), contracts=1, now=NOW)
        assert "--dry-run" not in CliBackend().submit_argv(order)
        assert "--dry-run" in CliBackend().submit_argv(order, dry_run=True)


class TestHappyPath:
    def test_a_clean_submission_reports_the_broker_order(self) -> None:
        cli = FakeCli(submit_responses=[cli_ok(order_id="ord-9", status="new")])
        order = build_opening_order(spread(), contracts=2, now=NOW)
        result = adapter(cli).submit(order)
        assert result.ok
        assert result.order_id == "ord-9"
        assert result.status == "new"
        assert result.backend is Backend.CLI
        assert result.backends_tried == (Backend.CLI,)
        assert result.attempts == 1
        assert not result.recovered
        assert result.payload == order.as_payload()
        assert cli.lookups == []

    def test_the_child_process_cannot_see_a_live_flag(self) -> None:
        cli = FakeCli(submit_responses=[cli_ok()])
        adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert cli.submits[0].env["ALPACA_LIVE_TRADE"] == "false"


class TestDryRun:
    def test_dry_run_submits_nothing_and_reports_the_body(self) -> None:
        echoed = '{"order_class":"mleg"}'
        cli = FakeCli(submit_responses=[CompletedCommand(returncode=0, stdout=echoed)])
        order = build_opening_order(spread(), contracts=1, now=NOW)
        result = adapter(cli).submit(order, dry_run=True)
        assert result.ok
        assert result.dry_run
        assert result.status == DRY_RUN_STATUS
        assert result.order_id is None
        assert result.message == echoed
        assert result.attempts == 0
        assert "--dry-run" in cli.submits[0].argv
        assert cli.lookups == []

    def test_a_failed_dry_run_never_triggers_a_lookup(self) -> None:
        cli = FakeCli(submit_responses=[cli_api_error(422, "bad legs")])
        result = adapter(cli).submit(
            build_opening_order(spread(), contracts=1, now=NOW), dry_run=True
        )
        assert not result.ok
        assert cli.lookups == []


class TestTerminalFailures:
    def test_auth_failure_is_terminal_and_never_retried(self) -> None:
        cli = FakeCli(submit_responses=[CLI_AUTH])
        sdk = FakeSdk()
        result = adapter(cli, sdk=sdk).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert not result.ok
        assert result.reason is Reason.AUTH
        assert len(cli.submits) == 1
        assert cli.lookups == []
        # A second transport would fail identically. Do not ask it.
        assert sdk.posts == []

    def test_a_rejected_order_is_terminal(self) -> None:
        cli = FakeCli(submit_responses=[cli_api_error(422, "insufficient options level")])
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert not result.ok
        assert result.reason is Reason.REJECTED
        assert "insufficient options level" in result.message
        assert cli.lookups == []

    def test_a_403_is_read_as_auth(self) -> None:
        cli = FakeCli(submit_responses=[cli_api_error(403, "forbidden")])
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert result.reason is Reason.AUTH

    def test_absent_credentials_exit_one_and_are_still_terminal(self) -> None:
        # Exit code 2 is HTTP 401 only. Entirely absent credentials exit 1 with
        # a structured error whose status is 0, because no request was made.
        # Verified against v0.0.14.
        missing = CompletedCommand(
            returncode=1,
            stderr=json.dumps(
                {
                    "code": 0,
                    "error": "authentication required\nHint: run `alpaca profile login`",
                    "hint": "",
                    "status": 0,
                }
            ),
        )
        cli = FakeCli(submit_responses=[missing])
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert not result.ok
        assert result.reason is Reason.AUTH
        assert cli.lookups == [], "no request was made, so there is nothing to reconcile"

    def test_any_other_status_zero_error_is_reconciled_not_assumed(self) -> None:
        # A connection dropped after the POST was written also reports status 0.
        # Only the credentials signature proves nothing was sent.
        dropped = CompletedCommand(
            returncode=1, stderr=json.dumps({"error": "connection reset by peer", "status": 0})
        )
        cli = FakeCli(submit_responses=[dropped], lookup_responses=[cli_ok(order_id="ord-x")])
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert result.ok
        assert result.order_id == "ord-x"
        assert len(cli.lookups) == 1

    def test_an_unexpected_4xx_is_an_api_error(self) -> None:
        cli = FakeCli(submit_responses=[cli_api_error(429, "rate limited")])
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert result.reason is Reason.API_ERROR
        assert cli.lookups == []


class TestAmbiguousOutcomes:
    """The heart of the module: never duplicate a spread."""

    def test_timeout_then_found_reports_the_existing_order(self) -> None:
        cli = FakeCli(
            submit_responses=[CLI_TIMEOUT],
            lookup_responses=[cli_ok(order_id="ord-live", status="filled")],
        )
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert result.ok
        assert result.recovered
        assert result.order_id == "ord-live"
        assert result.status == "filled"
        assert len(cli.submits) == 1, "the order existed; it must not be sent twice"
        assert len(cli.lookups) == 1

    def test_the_lookup_uses_our_client_order_id(self) -> None:
        cli = FakeCli(submit_responses=[CLI_TIMEOUT], lookup_responses=[cli_ok()])
        order = build_opening_order(spread(), contracts=1, now=NOW)
        adapter(cli).submit(order)
        argv = cli.lookups[0].argv
        assert argv[1:3] == ("order", "get-by-client-id")
        assert argv[argv.index("--client-order-id") + 1] == order.client_order_id

    def test_timeout_then_proven_absent_resubmits_once(self) -> None:
        cli = FakeCli(
            submit_responses=[CLI_TIMEOUT, cli_ok(order_id="ord-2")],
            lookup_responses=[cli_api_error(404, "order not found")],
        )
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert result.ok
        assert result.order_id == "ord-2"
        assert result.attempts == 2
        assert result.recovered
        assert len(cli.submits) == 2

    def test_timeout_then_a_failed_lookup_submits_nothing_further(self) -> None:
        """The single most important test in the suite.

        The submission timed out, so an order may or may not exist. The lookup
        then failed too, so we still do not know. The only acceptable behaviour
        is to stop: one submission was made, and no second one follows.

        The broker does not de-duplicate `client_order_id` (docs/GOTCHAS.md #9)
        and the CLI's own retry behaviour on a POST is unverified, so nothing
        downstream would catch a hopeful retry here. A missed trade costs an
        opportunity; a doubled spread costs double the risk with no record of
        why it happened.
        """
        cli = FakeCli(submit_responses=[CLI_TIMEOUT], lookup_responses=[CLI_TIMEOUT])
        sdk = FakeSdk()
        result = adapter(cli, sdk=sdk).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert len(cli.submits) == 1, "no submission may follow an unresolved outcome"
        assert sdk.posts == [], "and it must not reach a second transport either"
        assert not result.ok
        assert result.reason is Reason.UNKNOWN_OUTCOME
        assert "Reconcile by hand" in result.message

    def test_the_lookup_happens_before_any_second_submission(self) -> None:
        # Ordering, not just counting: the lookup is a precondition of the
        # resubmit, never a parallel afterthought.
        order_of_calls: list[str] = []

        cli = FakeCli(
            submit_responses=[CLI_TIMEOUT, cli_ok()],
            lookup_responses=[cli_api_error(404)],
            on_call=order_of_calls.append,
        )
        adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert order_of_calls == ["submit", "lookup", "submit"]

    def test_a_5xx_lookup_is_inconclusive(self) -> None:
        cli = FakeCli(submit_responses=[CLI_TIMEOUT], lookup_responses=[cli_api_error(503, "down")])
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert not result.ok
        assert result.reason is Reason.UNKNOWN_OUTCOME
        assert len(cli.submits) == 1

    def test_repeated_timeouts_stop_at_the_attempt_limit(self) -> None:
        cli = FakeCli(
            submit_responses=[CLI_TIMEOUT, CLI_TIMEOUT],
            lookup_responses=[cli_api_error(404), cli_api_error(404)],
        )
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert not result.ok
        assert result.reason is Reason.TIMEOUT
        assert result.attempts == 2
        assert len(cli.submits) == 2

    def test_a_5xx_submission_is_reconciled_not_retried(self) -> None:
        cli = FakeCli(
            submit_responses=[cli_api_error(500, "internal")],
            lookup_responses=[cli_ok(order_id="ord-5")],
        )
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert result.ok
        assert result.order_id == "ord-5"
        assert len(cli.submits) == 1

    def test_a_duplicate_client_order_id_resolves_to_the_existing_order(self) -> None:
        cli = FakeCli(
            submit_responses=[cli_api_error(409, "client_order_id must be unique")],
            lookup_responses=[cli_ok(order_id="ord-first", status="filled")],
        )
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert result.ok
        assert result.order_id == "ord-first"
        assert result.recovered
        assert len(cli.submits) == 1

    def test_a_duplicate_that_cannot_be_found_is_reported_not_resubmitted(self) -> None:
        cli = FakeCli(
            submit_responses=[cli_api_error(409, "duplicate")],
            lookup_responses=[CLI_TIMEOUT],
        )
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert not result.ok
        assert result.reason is Reason.UNKNOWN_OUTCOME
        assert len(cli.submits) == 1


class TestAbsenceProof:
    """The safety rule expressed as a type, tested as a type."""

    @pytest.mark.parametrize(
        "outcome",
        [
            BackendOutcome(kind=Kind.UNKNOWN, reason=Reason.TIMEOUT),
            BackendOutcome(kind=Kind.TERMINAL, reason=Reason.AUTH),
            BackendOutcome(kind=Kind.ACCEPTED),
        ],
    )
    def test_only_a_positive_absence_yields_proof(self, outcome: BackendOutcome) -> None:
        assert _AbsenceProof.from_lookup(outcome, "cid") is None

    def test_an_absent_lookup_yields_proof(self) -> None:
        proof = _AbsenceProof.from_lookup(BackendOutcome(kind=Kind.ABSENT), "cid")
        assert proof is not None
        assert proof.client_order_id == "cid"

    def test_proof_for_one_order_cannot_authorise_another(self) -> None:
        cli = FakeCli(submit_responses=[cli_ok()])
        backend = CliBackend(runner=cli, timeout=5.0)
        order = build_opening_order(spread(), contracts=1, now=NOW)
        stale = _AbsenceProof(client_order_id="uw-open-XLE-20260828-somethingelse", detail="")
        with pytest.raises(ValueError, match="refusing to resubmit"):
            _resubmit(backend, order, stale)
        assert cli.submits == []


class TestMalformedResponses:
    def test_exit_zero_with_unreadable_stdout_triggers_a_lookup(self) -> None:
        cli = FakeCli(
            submit_responses=[CompletedCommand(returncode=0, stdout="not json at all")],
            lookup_responses=[cli_ok(order_id="ord-7")],
        )
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert result.ok
        assert result.order_id == "ord-7"
        assert result.recovered
        assert len(cli.submits) == 1

    def test_json_without_an_order_id_is_not_an_order(self) -> None:
        # Valid JSON, but nothing we can act on. Two rounds of that, with the
        # broker confirming absence each time, exhausts the attempt limit.
        unreadable = CompletedCommand(returncode=0, stdout='{"status":"accepted"}')
        cli = FakeCli(
            submit_responses=[unreadable, unreadable],
            lookup_responses=[cli_api_error(404), cli_api_error(404)],
        )
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert not result.ok
        assert result.reason is Reason.MALFORMED_RESPONSE
        assert len(cli.submits) == 2

    def test_an_unstructured_cli_error_is_treated_as_unknown(self) -> None:
        # This is what an alpha-preview CLI renaming a flag looks like. It is
        # also what a crash mid-request looks like, so we conclude nothing.
        cli = FakeCli(
            submit_responses=[CLI_UNSTRUCTURED, CLI_UNSTRUCTURED],
            lookup_responses=[cli_api_error(404), cli_api_error(404)],
        )
        sdk = FakeSdk(post_responses=[SDK_ORDER])
        result = adapter(cli, sdk=sdk).submit(build_opening_order(spread(), contracts=1, now=NOW))
        # The broker confirmed absence after every attempt, so the resubmit and
        # then the fallback are both duplicate-safe.
        assert len(cli.submits) == 2
        assert result.ok
        assert result.backend is Backend.SDK
        assert result.backends_tried == (Backend.CLI, Backend.SDK)


class TestBackendSelection:
    def test_a_missing_cli_falls_through_to_the_sdk(self) -> None:
        cli = FakeCli()
        sdk = FakeSdk(post_responses=[SDK_ORDER])
        result = adapter(cli, sdk=sdk, binary="definitely-not-installed-abc123").submit(
            build_opening_order(spread(), contracts=1, now=NOW)
        )
        assert result.ok
        assert result.backend is Backend.SDK
        assert result.backends_tried == (Backend.CLI, Backend.SDK)
        assert cli.submits == []

    def test_both_backends_unavailable_reports_the_reason(self) -> None:
        result = ExecutionAdapter(
            primary=CliBackend(binary="definitely-not-installed-abc123", runner=FakeCli()),
            fallback=SdkBackend(client=None),
        ).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert not result.ok
        assert result.reason is Reason.BACKEND_UNAVAILABLE

    def test_an_unavailable_fallback_does_not_mask_the_real_failure(self) -> None:
        # "The CLI timed out twice and the broker says no order exists" is the
        # thing an operator needs to read, not "there was no SDK client".
        cli = FakeCli(
            submit_responses=[CLI_TIMEOUT, CLI_TIMEOUT],
            lookup_responses=[cli_api_error(404), cli_api_error(404)],
        )
        result = ExecutionAdapter(
            primary=CliBackend(runner=cli, timeout=5.0),
            fallback=SdkBackend(client=None),
        ).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert not result.ok
        assert result.backend is Backend.CLI
        assert result.reason is Reason.TIMEOUT

    def test_no_backend_configured_fails_closed(self) -> None:
        adapter_without = ExecutionAdapter(primary=SdkBackend(client=None), fallback=None)
        result = adapter_without.submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert not result.ok
        assert result.reason is Reason.BACKEND_UNAVAILABLE

    def test_the_default_wiring_is_cli_first(self) -> None:
        built = build_adapter(runner=FakeCli())
        assert built.primary.name is Backend.CLI
        assert built.fallback is not None
        assert built.fallback.name is Backend.SDK

    def test_the_sdk_can_be_made_primary_by_configuration(self) -> None:
        # One argument, no rewrite: the POST goes through the SDK, where retry
        # behaviour is ours, and the CLI stays wired in behind it.
        built = build_adapter(primary=Backend.SDK, runner=FakeCli(), sdk_client=FakeSdk())
        assert built.primary.name is Backend.SDK
        assert built.fallback is not None
        assert built.fallback.name is Backend.CLI

    def test_an_sdk_primary_actually_carries_the_submission(self) -> None:
        cli = FakeCli()
        sdk = FakeSdk(post_responses=[SDK_ORDER])
        order = build_opening_order(spread(), contracts=1, now=NOW)
        result = build_adapter(primary=Backend.SDK, runner=cli, sdk_client=sdk).submit(order)
        assert result.ok
        assert result.backend is Backend.SDK
        assert sdk.posts == [("/orders", dict(order.as_payload()))]
        assert cli.submits == []


class TestSdkBackend:
    def test_it_sends_the_identical_payload(self) -> None:
        sdk = FakeSdk(post_responses=[SDK_ORDER])
        order = build_opening_order(spread(), contracts=2, now=NOW)
        outcome = SdkBackend(client=sdk).submit(order)
        assert outcome.order is not None
        assert outcome.order.id == "sdk-1"
        assert sdk.posts == [("/orders", dict(order.as_payload()))]

    def test_an_api_error_carries_its_status(self) -> None:
        sdk = FakeSdk(post_responses=[FakeApiError(401, "unauthorized")])
        outcome = SdkBackend(client=sdk).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert outcome.reason is Reason.AUTH

    def test_a_transport_timeout_is_unknown_not_failed(self) -> None:
        sdk = FakeSdk(post_responses=[ReadTimeout("timed out")])
        outcome = SdkBackend(client=sdk).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert outcome.reason is Reason.TIMEOUT
        assert outcome.kind is Kind.UNKNOWN

    def test_an_unrecognised_exception_is_unknown(self) -> None:
        sdk = FakeSdk(post_responses=[RuntimeError("who knows")])
        outcome = SdkBackend(client=sdk).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert outcome.kind is Kind.UNKNOWN
        assert outcome.reason is Reason.API_ERROR

    def test_lookup_queries_by_client_order_id(self) -> None:
        sdk = FakeSdk(get_responses=[SDK_ORDER])
        SdkBackend(client=sdk).lookup("uw-open-XLE-20260828-abc")
        assert sdk.gets == [
            ("/orders:by_client_order_id", {"client_order_id": "uw-open-XLE-20260828-abc"})
        ]

    def test_a_404_lookup_proves_absence(self) -> None:
        sdk = FakeSdk(get_responses=[FakeApiError(404, "order not found")])
        outcome = SdkBackend(client=sdk).lookup("cid")
        assert outcome.kind is Kind.ABSENT

    def test_dry_run_sends_nothing(self) -> None:
        sdk = FakeSdk()
        order = build_opening_order(spread(), contracts=1, now=NOW)
        outcome = SdkBackend(client=sdk).submit(order, dry_run=True)
        assert sdk.posts == []
        assert json.loads(outcome.message) == order.as_payload()

    def test_a_nonsense_body_is_not_mistaken_for_an_order(self) -> None:
        sdk = FakeSdk(post_responses=[{"detail": "something else"}])
        outcome = SdkBackend(client=sdk).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert outcome.reason is Reason.MALFORMED_RESPONSE
        assert outcome.kind is Kind.UNKNOWN
