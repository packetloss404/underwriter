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
from alpaca.common.exceptions import APIError
from requests.exceptions import HTTPError

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
    LegFill,
    MultiLegOrder,
    OrderLeg,
    PositionIntent,
    Reason,
    SdkBackend,
    Side,
    _AbsenceProof,
    _as_decimal,
    _resubmit,
    assert_paper_only,
    build_adapter,
    build_closing_order,
    build_opening_order,
    client_order_id,
    disable_automatic_retries,
    expected_price_sign,
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
    get_responses: list[CompletedCommand] = field(default_factory=list)
    submits: list[Invocation] = field(default_factory=list)
    lookups: list[Invocation] = field(default_factory=list)
    gets: list[Invocation] = field(default_factory=list)
    # Optional sink recording the interleaving of calls, for tests that care
    # about ordering rather than counts.
    on_call: Callable[[str], None] | None = None

    def __call__(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str]
    ) -> CompletedCommand:
        call = Invocation(argv=tuple(argv), env=dict(env))
        if "submit" in call.argv:
            kind = "submit"
        elif "get-by-client-id" in call.argv:
            kind = "lookup"
        else:
            kind = "get"
        if self.on_call is not None:
            self.on_call(kind)
        queues = {
            "submit": (self.submits, self.submit_responses),
            "lookup": (self.lookups, self.lookup_responses),
            "get": (self.gets, self.get_responses),
        }
        queue, responses = queues[kind]
        queue.append(call)
        index = len(queue) - 1
        if index >= len(responses):
            msg = f"unscripted CLI call: {call.argv}"
            raise AssertionError(msg)
        return responses[index]


def cli_ok(
    order_id: str = "ord-1", status: str = "accepted", client_id: str = ""
) -> CompletedCommand:
    """A successful CLI response.

    `client_id` defaults to empty, which is the documented-unverified case:
    whether a caller-supplied client_order_id propagates onto an mleg parent is
    not established, so an empty echo must not be read as a mismatch. Use
    `cli_found` when the test needs the broker to echo the real id.
    """
    body = {"id": order_id, "client_order_id": client_id, "status": status}
    return CompletedCommand(returncode=0, stdout=json.dumps(body))


def cli_found(
    order: MultiLegOrder, *, order_id: str = "ord-1", status: str = "accepted"
) -> CompletedCommand:
    """A lookup response that correctly echoes the id we asked about."""
    return cli_ok(order_id=order_id, status=status, client_id=order.client_order_id)


def sdk_found(order: MultiLegOrder, *, order_id: str = "sdk-1") -> dict[str, str]:
    return {"id": order_id, "client_order_id": order.client_order_id, "status": "accepted"}


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
        # slightly less than the model said, so an opening order collects AT
        # MOST the modelled credit. Realised economics can never be better than
        # the four-decimal model -- that is the direction that keeps the
        # backtest honest.
        assert to_limit_price(0.4278, credit=True) == Decimal("-0.42")

    def test_a_debit_rounds_toward_paying_more(self) -> None:
        # The mirror: a closing order pays AT LEAST the modelled debit.
        assert to_limit_price(0.4212, credit=False) == Decimal("0.43")

    def test_the_quantisation_is_worth_reporting_not_ignoring(self) -> None:
        # Up to 0.99 cents per spread. On a 0.42 credit that is ~2.4% of the
        # premium, which is not noise against a sub-1%/week target, so P&L
        # modelling reads the quantised price rather than the modelled one.
        modelled = Decimal("0.4299")
        submitted = to_limit_price(modelled, credit=True)
        assert submitted == Decimal("-0.42")
        given_up = modelled + submitted  # 0.4299 - 0.42
        assert given_up == Decimal("0.0099")
        assert given_up / modelled > Decimal("0.02")

    def test_non_finite_input_survives_to_validation(self) -> None:
        price = to_limit_price(float("nan"), credit=True)
        assert not price.is_finite()


class TestSignIsAsserted:
    """Nothing may reach the wire with a price that contradicts its legs.

    docs/GOTCHAS.md #7's catastrophe does not error and does not reject: an
    opening credit spread priced positive reads as "I will pay the width to
    enter this", plausibly fills, and shows up only as inexplicable P&L. The
    selector cannot be the only thing standing in the way -- that guarantee
    lives in another module, `CreditSpread` is a plain dataclass anyone can
    construct, and the `credit=`/`debit=` overrides bypass selection entirely.
    """

    def test_a_negative_credit_is_refused_at_the_source(self) -> None:
        # This is the exact call that used to produce limit_price "0.50" on an
        # opening credit spread and pass validation clean.
        with pytest.raises(ValueError, match="must be positive"):
            build_opening_order(spread(credit=-0.50), contracts=1, now=NOW)

    def test_the_credit_override_cannot_smuggle_a_sign_in(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            build_opening_order(spread(), contracts=1, credit=-0.50, now=NOW)

    def test_a_negative_closing_debit_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            build_closing_order(spread(), contracts=1, debit=-0.20, now=NOW)

    @pytest.mark.parametrize("bad", [0.0, -0.01])
    def test_a_nonpositive_magnitude_is_refused(self, bad: float) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            to_limit_price(bad, credit=True)

    def test_a_hand_built_credit_spread_priced_positive_is_rejected(self) -> None:
        # The backstop for an order that never went through a builder.
        problem = validate(order_with(limit_price=Decimal("0.42")))
        assert problem is not None
        assert "contradicts the legs" in problem
        assert "GOTCHAS.md #7" in problem

    def test_a_hand_built_closing_order_priced_negative_is_rejected(self) -> None:
        closing = (
            OrderLeg(SHORT_SYMBOL, Side.BUY, PositionIntent.BUY_TO_CLOSE),
            OrderLeg(LONG_SYMBOL, Side.SELL, PositionIntent.SELL_TO_CLOSE),
        )
        problem = validate(order_with(legs=closing, limit_price=Decimal("-0.42")))
        assert problem is not None
        assert "contradicts the legs" in problem

    def test_an_order_that_both_opens_and_closes_is_rejected(self) -> None:
        mixed = (
            OrderLeg(SHORT_SYMBOL, Side.SELL, PositionIntent.SELL_TO_OPEN),
            OrderLeg(LONG_SYMBOL, Side.BUY, PositionIntent.BUY_TO_CLOSE),
        )
        problem = validate(order_with(legs=mixed))
        assert problem is not None
        assert "two orders fused" in problem

    def test_the_expected_sign_follows_the_intents(self) -> None:
        opening = build_opening_order(spread(), contracts=1, now=NOW)
        closing = build_closing_order(spread(), contracts=1, debit=0.2, now=NOW)
        assert expected_price_sign(opening.legs) == -1
        assert expected_price_sign(closing.legs) == 1

    def test_correctly_signed_orders_still_pass(self) -> None:
        assert validate(build_opening_order(spread(), contracts=1, now=NOW)) is None
        assert validate(build_closing_order(spread(), contracts=1, debit=0.2, now=NOW)) is None


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

    def test_a_same_day_re_entry_does_not_collide_with_the_morning(self) -> None:
        """The scenario that used to double-count a fill.

        Open a spread at 10:00 and close it. At 14:00 the strategy re-enters
        the identical structure -- same legs, same size, same price, same day.
        Under day-keyed ids both orders shared one client_order_id, so an
        ambiguous afternoon submission would look up and find the *morning's*
        filled order, report it as recovered, and book a fill that had already
        happened. Two calls now mean two orders.
        """
        morning = build_opening_order(
            spread(), contracts=1, now=datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
        )
        afternoon = build_opening_order(
            spread(), contracts=1, now=datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
        )
        assert morning.client_order_id != afternoon.client_order_id

    def test_the_safe_default_needs_no_nonce(self) -> None:
        # Correctness must not depend on the caller remembering something.
        first = build_opening_order(spread(), contracts=1)
        second = build_opening_order(spread(), contracts=1)
        assert first.client_order_id != second.client_order_id

    def test_a_retry_reuses_the_id_it_was_minted_with(self) -> None:
        # The other half: the adapter must not re-mint between attempts, or the
        # lookup would be asking about an order it never sent.
        order = build_opening_order(spread(), contracts=1, now=NOW)
        cli = FakeCli(
            submit_responses=[CLI_TIMEOUT, cli_found(order)],
            lookup_responses=[cli_api_error(404)],
        )
        adapter(cli).submit(order)
        sent = {c.argv[c.argv.index("--client-order-id") + 1] for c in cli.submits}
        looked_up = {c.argv[c.argv.index("--client-order-id") + 1] for c in cli.lookups}
        assert sent == looked_up == {order.client_order_id}

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
        order = build_opening_order(spread(), contracts=1, now=NOW)
        cli = FakeCli(
            submit_responses=[dropped], lookup_responses=[cli_found(order, order_id="ord-x")]
        )
        result = adapter(cli).submit(order)
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
        order = build_opening_order(spread(), contracts=1, now=NOW)
        cli = FakeCli(
            submit_responses=[CLI_TIMEOUT],
            lookup_responses=[cli_found(order, order_id="ord-live", status="filled")],
        )
        result = adapter(cli).submit(order)
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
        order = build_opening_order(spread(), contracts=1, now=NOW)
        cli = FakeCli(
            submit_responses=[cli_api_error(500, "internal")],
            lookup_responses=[cli_found(order, order_id="ord-5")],
        )
        result = adapter(cli).submit(order)
        assert result.ok
        assert result.order_id == "ord-5"
        assert len(cli.submits) == 1

    def test_a_duplicate_client_order_id_resolves_to_the_existing_order(self) -> None:
        order = build_opening_order(spread(), contracts=1, now=NOW)
        cli = FakeCli(
            submit_responses=[cli_api_error(409, "client_order_id must be unique")],
            lookup_responses=[cli_found(order, order_id="ord-first", status="filled")],
        )
        result = adapter(cli).submit(order)
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


class TestRecoveredOrderMustBeOurs:
    """A lookup answer is adopted only if it is provably about our order.

    The realistic route to a foreign order is our own id, not a broker bug.
    See TestClientOrderId for why re-entry no longer collides; this is the
    backstop for every other way a wrong order could come back.
    """

    def test_a_foreign_order_is_never_reported_as_ours(self) -> None:
        cli = FakeCli(
            submit_responses=[CLI_TIMEOUT],
            lookup_responses=[cli_ok(order_id="someone-elses", client_id="not-our-id")],
        )
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert not result.ok
        assert result.reason is Reason.UNKNOWN_OUTCOME
        assert result.order_id is None, "a foreign order id must not propagate"
        assert result.filled_avg_price is None, "nor a foreign fill"
        assert len(cli.submits) == 1, "and an unproven outcome still resubmits nothing"

    def test_a_matching_echo_is_accepted(self) -> None:
        order = build_opening_order(spread(), contracts=1, now=NOW)
        cli = FakeCli(
            submit_responses=[CLI_TIMEOUT],
            lookup_responses=[cli_found(order, order_id="ours")],
        )
        result = adapter(cli).submit(order)
        assert result.ok
        assert result.order_id == "ours"

    def test_an_empty_echo_is_accepted_because_propagation_is_unverified(self) -> None:
        # Whether a caller-supplied client_order_id reaches the mleg parent is
        # not established, so an empty value is not evidence of a mismatch and
        # must not turn a genuine recovery into a refusal.
        cli = FakeCli(
            submit_responses=[CLI_TIMEOUT], lookup_responses=[cli_ok(order_id="ours", client_id="")]
        )
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert result.ok
        assert result.order_id == "ours"

    def test_a_foreign_order_from_the_reconciler_falls_through_to_the_submitter(self) -> None:
        order = build_opening_order(spread(), contracts=1, now=NOW)
        cli = FakeCli(lookup_responses=[cli_ok(order_id="wrong", client_id="not-ours")])
        sdk = FakeSdk(post_responses=[ReadTimeout("x")], get_responses=[sdk_found(order)])
        result = build_adapter(runner=cli, sdk_client=sdk).submit(order)
        assert result.ok
        assert result.order_id == "sdk-1"
        assert len(sdk.gets) == 1


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
        order = build_opening_order(spread(), contracts=1, now=NOW)
        cli = FakeCli(
            submit_responses=[CompletedCommand(returncode=0, stdout="not json at all")],
            lookup_responses=[cli_found(order, order_id="ord-7")],
        )
        result = adapter(cli).submit(order)
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

    def test_the_default_wiring_puts_the_post_on_the_sdk_and_nowhere_else(self) -> None:
        # The CLI may retry a POST internally and we cannot verify whether it
        # does. Through alpaca-py the retry loop is ours.
        built = build_adapter(runner=FakeCli())
        assert built.primary.name is Backend.SDK
        assert built.fallback is None, "there is no CLI submission fallback by default"

    def test_the_default_wiring_keeps_lookups_on_the_cli(self) -> None:
        built = build_adapter(runner=FakeCli())
        assert built.reconciler is not None
        assert built.reconciler.name is Backend.CLI

    def test_the_sdk_actually_carries_the_submission(self) -> None:
        cli = FakeCli()
        sdk = FakeSdk(post_responses=[SDK_ORDER])
        order = build_opening_order(spread(), contracts=1, now=NOW)
        result = build_adapter(runner=cli, sdk_client=sdk).submit(order)
        assert result.ok
        assert result.backend is Backend.SDK
        assert sdk.posts == [("/orders", dict(order.as_payload()))]
        assert cli.submits == [], "the CLI must not carry the POST by default"

    def test_the_cli_submission_path_stays_available_behind_the_opt_in(self) -> None:
        # Kept complete and tested so the decision is reversible on evidence,
        # but unreachable without saying what you are accepting.
        cli = FakeCli(submit_responses=[cli_ok()])
        built = build_adapter(
            runner=cli,
            sdk_client=None,
            i_accept_undetectable_duplicate_orders_from_the_cli=True,
        )
        result = built.submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert result.ok
        assert result.backend is Backend.CLI
        assert len(cli.submits) == 1


class TestFailsClosedWithoutAnSdkClient:
    """Pinned default. Do not "fix" these tests -- read the reason first.

    An unusable SDK must mean no submission, never a CLI POST. The CLI binary
    may retry a POST internally (unverified, and unverifiable without a
    base-URL override), and duplicate handling on POST /v2/orders is
    undocumented, so two live orders could share one `client_order_id`. Then
    `order get-by-client-id` returns one of them and nothing reveals the other:
    the duplicate would be *undetectable*, not merely detected late.

    A missed trade costs an opportunity. An undetected doubled spread breaks
    the risk model -- six concurrent positions against a 3% aggregate cap have
    no headroom to absorb one silent double.
    """

    def test_no_sdk_client_fails_closed_and_the_cli_submits_nothing(self) -> None:
        cli = FakeCli(submit_responses=[cli_ok()])
        result = build_adapter(runner=cli, sdk_client=None).submit(
            build_opening_order(spread(), contracts=1, now=NOW)
        )
        assert cli.submits == [], "the CLI must never carry a POST by default"
        assert not result.ok
        assert result.reason is Reason.BACKEND_UNAVAILABLE

    def test_an_sdk_that_raises_on_submit_still_never_reaches_the_cli(self) -> None:
        cli = FakeCli(
            lookup_responses=[cli_api_error(404, "not found"), cli_api_error(404, "not found")]
        )
        sdk = FakeSdk(post_responses=[ReadTimeout("x"), ReadTimeout("x")])
        result = build_adapter(runner=cli, sdk_client=sdk).submit(
            build_opening_order(spread(), contracts=1, now=NOW)
        )
        # Absence was proven twice over, which would make a fallback POST
        # duplicate-safe -- and it still does not happen.
        assert cli.submits == []
        assert not result.ok
        assert len(sdk.posts) == 2

    def test_the_cli_still_reconciles_when_it_may_not_submit(self) -> None:
        order = build_opening_order(spread(), contracts=1, now=NOW)
        cli = FakeCli(lookup_responses=[cli_found(order, order_id="ord-cli")])
        sdk = FakeSdk(post_responses=[ReadTimeout("x")])
        result = build_adapter(runner=cli, sdk_client=sdk).submit(order)
        assert result.ok
        assert len(cli.lookups) == 1
        assert cli.submits == []


class TestReconciliationRoutesThroughTheCli:
    """The POST goes out over the SDK; the "did it land?" read goes out over
    the CLI. That keeps the CLI genuinely on the order path, and makes the
    confirmation come from a different transport than the one that just failed
    to report an outcome."""

    def test_an_sdk_timeout_is_reconciled_by_the_cli(self) -> None:
        order = build_opening_order(spread(), contracts=1, now=NOW)
        cli = FakeCli(lookup_responses=[cli_found(order, order_id="ord-cli", status="filled")])
        sdk = FakeSdk(post_responses=[ReadTimeout("read timed out")])
        result = build_adapter(runner=cli, sdk_client=sdk).submit(order)
        assert result.ok
        assert result.recovered
        assert result.order_id == "ord-cli"
        assert len(cli.lookups) == 1, "the CLI answered the lookup"
        assert sdk.gets == [], "and the SDK was not asked"
        assert len(sdk.posts) == 1, "and nothing was submitted twice"

    def test_a_cli_proven_absence_authorises_the_sdk_to_resubmit(self) -> None:
        cli = FakeCli(lookup_responses=[cli_api_error(404, "order not found")])
        sdk = FakeSdk(post_responses=[ReadTimeout("read timed out"), SDK_ORDER])
        result = build_adapter(runner=cli, sdk_client=sdk).submit(
            build_opening_order(spread(), contracts=1, now=NOW)
        )
        assert result.ok
        assert result.attempts == 2
        assert len(sdk.posts) == 2

    def test_a_broken_reconciler_degrades_to_a_second_opinion(self) -> None:
        # The CLI cannot answer, so the submitting backend is asked instead.
        # A missing reconciler must not turn a resolvable outcome into a
        # refusal.
        order = build_opening_order(spread(), contracts=1, now=NOW)
        cli = FakeCli(lookup_responses=[CLI_TIMEOUT])
        sdk = FakeSdk(post_responses=[ReadTimeout("x")], get_responses=[sdk_found(order)])
        result = build_adapter(runner=cli, sdk_client=sdk).submit(order)
        assert result.ok
        assert result.recovered
        assert len(cli.lookups) == 1
        assert len(sdk.gets) == 1

    def test_both_lookups_failing_still_refuses_to_resubmit(self) -> None:
        cli = FakeCli(lookup_responses=[CLI_TIMEOUT])
        sdk = FakeSdk(
            post_responses=[ReadTimeout("x")], get_responses=[ReadTimeout("also timed out")]
        )
        result = build_adapter(runner=cli, sdk_client=sdk).submit(
            build_opening_order(spread(), contracts=1, now=NOW)
        )
        assert not result.ok
        assert result.reason is Reason.UNKNOWN_OUTCOME
        assert len(sdk.posts) == 1, "two failed lookups still authorise nothing"

    def test_the_answering_transport_is_named_in_the_message(self) -> None:
        order = build_opening_order(spread(), contracts=1, now=NOW)
        cli = FakeCli(lookup_responses=[cli_found(order, order_id="ord-cli")])
        sdk = FakeSdk(post_responses=[ReadTimeout("read timed out")])
        result = build_adapter(runner=cli, sdk_client=sdk).submit(order)
        assert "cli lookup" in result.message


NESTED_ORDER = {
    "id": "ord-1",
    "client_order_id": "cid",
    "status": "filled",
    "filled_qty": "2",
    "filled_avg_price": "-1.20",
    "legs": [
        {
            "symbol": SHORT_SYMBOL,
            "side": "sell",
            "position_intent": "sell_to_open",
            # Leg units: CONTRACTS (ratio_qty x parent qty), and the leg's own
            # premium, always positive.
            "filled_qty": "2",
            "filled_avg_price": "2.05",
        },
        {
            "symbol": LONG_SYMBOL,
            "side": "buy",
            "position_intent": "buy_to_open",
            "filled_qty": "2",
            "filled_avg_price": "0.85",
        },
    ],
}


class TestLegDetail:
    """`get-by-client-id` has no --nested, but `order get --order-id` does.

    So leg detail costs one extra call keyed on the broker's order id. It is
    not unavailable, which matters: for an ordinary vertical the parent alone
    already determines the position, and this is only needed for per-leg fill
    prices or when the parent reports a size without a price.
    """

    def test_the_cli_fetches_legs_by_order_id_with_nested(self) -> None:
        cli = FakeCli(get_responses=[CompletedCommand(0, json.dumps(NESTED_ORDER))])
        legs = CliBackend(runner=cli, timeout=5.0).fetch_legs("ord-1")
        assert legs is not None
        argv = cli.gets[0].argv
        assert argv[1:3] == ("order", "get")
        assert "--nested" in argv
        assert argv[argv.index("--order-id") + 1] == "ord-1"

    def test_leg_units_are_contracts_and_premiums_are_positive(self) -> None:
        cli = FakeCli(get_responses=[CompletedCommand(0, json.dumps(NESTED_ORDER))])
        legs = CliBackend(runner=cli, timeout=5.0).fetch_legs("ord-1")
        assert legs == (
            LegFill(
                symbol=SHORT_SYMBOL,
                side="sell",
                position_intent="sell_to_open",
                filled_qty=Decimal("2"),
                filled_avg_price=Decimal("2.05"),
            ),
            LegFill(
                symbol=LONG_SYMBOL,
                side="buy",
                position_intent="buy_to_open",
                filled_qty=Decimal("2"),
                filled_avg_price=Decimal("0.85"),
            ),
        )

    def test_the_legs_reconcile_to_the_parent_signed_net(self) -> None:
        # The property that makes the parent sufficient on its own: the legs'
        # own premiums net to the parent's signed price, so nothing is lost by
        # not fetching them.
        cli = FakeCli(get_responses=[CompletedCommand(0, json.dumps(NESTED_ORDER))])
        legs = CliBackend(runner=cli, timeout=5.0).fetch_legs("ord-1")
        assert legs is not None
        short, long_ = legs
        assert short.filled_avg_price is not None
        assert long_.filled_avg_price is not None
        net = long_.filled_avg_price - short.filled_avg_price
        assert net == Decimal("-1.20") == _as_decimal(NESTED_ORDER["filled_avg_price"])

    def test_the_sdk_fetches_legs_over_rest(self) -> None:
        sdk = FakeSdk(get_responses=[NESTED_ORDER])
        legs = SdkBackend(client=sdk).fetch_legs("ord-1")
        assert legs is not None
        assert sdk.gets == [("/orders/ord-1", {"nested": "true"})]

    def test_the_adapter_prefers_the_cli_for_leg_detail(self) -> None:
        cli = FakeCli(get_responses=[CompletedCommand(0, json.dumps(NESTED_ORDER))])
        sdk = FakeSdk()
        legs = build_adapter(runner=cli, sdk_client=sdk).fetch_legs("ord-1")
        assert legs is not None
        assert sdk.gets == [], "the reconciler answered"

    def test_it_degrades_to_the_sdk_when_the_cli_cannot_answer(self) -> None:
        cli = FakeCli(get_responses=[CLI_TIMEOUT])
        sdk = FakeSdk(get_responses=[NESTED_ORDER])
        legs = build_adapter(runner=cli, sdk_client=sdk).fetch_legs("ord-1")
        assert legs is not None
        assert len(sdk.gets) == 1

    def test_an_unreadable_response_is_none_not_an_empty_tuple(self) -> None:
        # Empty would read as "the order has no legs", which is a different and
        # false statement.
        cli = FakeCli(get_responses=[CompletedCommand(0, '{"id":"o","client_order_id":"c"}')])
        assert CliBackend(runner=cli, timeout=5.0).fetch_legs("ord-1") is None

    def test_a_failed_fetch_is_none(self) -> None:
        cli = FakeCli(get_responses=[cli_api_error(404, "not found")])
        assert CliBackend(runner=cli, timeout=5.0).fetch_legs("ord-1") is None


class TestAuditRecord:
    def test_the_record_names_the_transport_that_placed_the_order(self) -> None:
        sdk = FakeSdk(post_responses=[SDK_ORDER])
        record = (
            build_adapter(runner=FakeCli(), sdk_client=sdk)
            .submit(build_opening_order(spread(), contracts=1, now=NOW))
            .as_record()
        )
        assert record["backend"] == "sdk"
        assert record["backends_tried"] == ["sdk"]
        assert record["ok"] is True
        assert record["payload"] == build_opening_order(spread(), contracts=1, now=NOW).as_payload()

    def test_the_submitted_price_reaches_the_result_and_the_record(self) -> None:
        # So downstream P&L never has to re-derive the rounding.
        cli = FakeCli(submit_responses=[cli_ok()])
        order = build_opening_order(spread(credit=0.4278), contracts=1, now=NOW)
        result = adapter(cli).submit(order)
        assert result.limit_price == Decimal("-0.42") == order.limit_price
        assert result.as_record()["limit_price"] == "-0.42"

    def test_the_price_is_recorded_even_when_the_order_fails(self) -> None:
        cli = FakeCli(submit_responses=[cli_api_error(422, "rejected")])
        result = adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW))
        assert not result.ok
        assert result.limit_price == Decimal("-0.42")

    def test_the_record_is_json_serialisable(self) -> None:
        cli = FakeCli(submit_responses=[cli_api_error(422, "rejected")])
        record = (
            adapter(cli).submit(build_opening_order(spread(), contracts=1, now=NOW)).as_record()
        )
        assert json.loads(json.dumps(record))["reason"] == "rejected"

    def test_decimals_survive_as_exact_strings(self) -> None:
        body = {
            "id": "o",
            "client_order_id": "c",
            "status": "filled",
            "filled_qty": "5",
            "filled_avg_price": "-1.20",
        }
        cli = FakeCli(submit_responses=[CompletedCommand(returncode=0, stdout=json.dumps(body))])
        record = (
            adapter(cli).submit(build_opening_order(spread(), contracts=5, now=NOW)).as_record()
        )
        assert record["filled_avg_price"] == "-1.20"


class TestRetryDisabling:
    """alpaca-py retries POSTs on 429 and 504 by default. It must not."""

    def test_retries_are_switched_off(self) -> None:
        class Client:
            def __init__(self) -> None:
                self._retry = 3

        client = Client()
        disable_automatic_retries(client)
        assert client._retry == 0

    def test_a_client_that_cannot_be_disabled_is_refused(self) -> None:
        class Stubborn:
            __slots__ = ()

        with pytest.raises(RuntimeError, match="cannot disable"):
            disable_automatic_retries(Stubborn())

    def test_it_works_on_a_real_alpaca_py_client(self) -> None:
        """Pinned against the real SDK, so an upgrade that moves this breaks here.

        No network: constructing the client and reading the attribute are both
        local. Verified on alpaca-py 0.44.0, whose defaults are 3 attempts on
        HTTP 429 and 504 with a 3 second wait.
        """
        from alpaca.trading.client import TradingClient

        client = TradingClient(api_key="fake", secret_key="fake", paper=True)
        assert client._retry > 0, "alpaca-py still retries by default"
        assert 504 in client._retry_codes, "and 504 is the dangerous one on a POST"
        disable_automatic_retries(client)
        assert client._retry == 0

    def test_the_constructor_cannot_be_used_to_disable_them(self) -> None:
        """Why the private attribute is written instead of passing a parameter.

        `RESTClient.__init__` applies the value under
        `if retry_attempts and retry_attempts > 0`, so 0 is falsy, is ignored,
        and silently leaves the default of 3. `TradingClient` does not expose
        the parameter at all.
        """
        import inspect

        from alpaca.common.rest import RESTClient
        from alpaca.trading.client import TradingClient

        class Unvalidated(RESTClient):
            @staticmethod
            def _validate_credentials(
                api_key: str | None = None,
                secret_key: str | None = None,
                oauth_token: str | None = None,
            ) -> tuple[str | None, str | None, str | None]:
                return ("k", "s", None)

        client = Unvalidated(
            base_url="https://paper-api.alpaca.markets",
            api_key="k",
            secret_key="s",
            retry_attempts=0,
        )
        assert client._retry == 3, "retry_attempts=0 is falsy and silently ignored"
        assert "retry_attempts" not in inspect.signature(TradingClient.__init__).parameters

    def test_exactly_one_http_attempt_is_made_on_a_retryable_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The behaviour that actually matters, proven without a network.

        A 504 on an order submission may well mean the order reached the order
        system. alpaca-py would retry it three times by default; after
        disabling, the transport is called exactly once and the error is raised
        for us to reconcile.
        """
        from alpaca.trading.client import TradingClient

        client = TradingClient(api_key="fake", secret_key="fake", paper=True)
        disable_automatic_retries(client)

        attempts: list[str] = []

        class GatewayTimeout:
            status_code = 504
            text = '{"message":"gateway timeout"}'

            def raise_for_status(self) -> None:
                raise HTTPError("504 Server Error")

        def counting_request(method: str, url: str, **kwargs: object) -> GatewayTimeout:
            attempts.append(method)
            return GatewayTimeout()

        monkeypatch.setattr(client._session, "request", counting_request)
        with pytest.raises(APIError):
            client.post("/orders", {"qty": "1"})
        assert attempts == ["POST"], "a retried POST can double a position"

    def test_the_backend_that_carries_the_post_enforces_it_itself(self) -> None:
        """The guarantee lives on the object that submits, not one constructor.

        A caller who builds `SdkBackend(client=...)` with their own client must
        not keep alpaca-py's retry-on-429/504 behaviour on the order path. 504
        is the dangerous one: a gateway timeout on a submission very possibly
        reached the order system.
        """
        from alpaca.trading.client import TradingClient

        client = TradingClient(api_key="fake", secret_key="fake", paper=True)
        assert client._retry == 3, "alpaca-py hands back a retrying client"
        SdkBackend(client=client)
        assert client._retry == 0, "constructing the backend switched them off"

    def test_a_backend_refuses_to_construct_around_an_unsafe_client(self) -> None:
        class Ignores:
            @property
            def _retry(self) -> int:
                return 3

            @_retry.setter
            def _retry(self, value: int) -> None:
                return

            def post(self, path: str, data: dict[str, object] | None = None) -> object:
                return None

            def get(self, path: str, data: dict[str, object] | None = None) -> object:
                return None

        with pytest.raises(RuntimeError, match="still enabled"):
            SdkBackend(client=Ignores())

    def test_a_backend_with_no_client_constructs_fine(self) -> None:
        # Nothing to make unsafe, and this is the fail-closed default wiring.
        assert SdkBackend(client=None).unavailable_reason() is not None

    def test_a_silently_ignored_setting_is_refused(self) -> None:
        # The failure mode that matters: alpaca-py's own constructor treats
        # retry_attempts=0 as "unset" and keeps its default of 3.
        class Ignores:
            @property
            def _retry(self) -> int:
                return 3

            @_retry.setter
            def _retry(self, value: int) -> None:
                return

        with pytest.raises(RuntimeError, match="still enabled"):
            disable_automatic_retries(Ignores())


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
