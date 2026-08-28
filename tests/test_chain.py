"""Contract selection tests.

Weighted towards rejection paths and towards the two behaviours that are easy
to get silently wrong: the expiry-window truncation gotcha, and the selection
objective drifting to the cheapest, least probable spread.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from rotunda.chain import (
    Contract,
    ContractType,
    DeltaPolicy,
    ExpiryWindow,
    LiquidityPolicy,
    Quote,
    Rejected,
    Rejection,
    SpreadEconomics,
    VerticalSpread,
    screen_contract,
    select_vertical,
)

NOW = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
TODAY = date(2026, 8, 31)
EXPIRY = date(2026, 9, 11)
WINDOW = ExpiryWindow.from_dte(TODAY, 5, 14)


def contract(
    strike: float,
    *,
    bid: float = 1.00,
    ask: float = 1.10,
    delta: float | None = 0.30,
    oi: int | None = 5000,
    expiry: date = EXPIRY,
    kind: ContractType = ContractType.CALL,
    age_s: float = 2.0,
    quote: bool = True,
) -> Contract:
    right = "C" if kind is ContractType.CALL else "P"
    return Contract(
        symbol=f"SPY{expiry:%y%m%d}{right}{int(strike * 1000):08d}",
        underlying="SPY",
        expiry=expiry,
        strike=strike,
        contract_type=kind,
        quote=Quote(bid, ask, NOW - timedelta(seconds=age_s)) if quote else None,
        delta=delta,
        open_interest=oi,
    )


class TestExpiryWindow:
    def test_always_emits_both_bounds(self) -> None:
        # The whole point: omitting expiration_date_lte silently truncates the
        # chain to contracts expiring before the coming weekend.
        params = WINDOW.as_query_params()
        assert set(params) == {"expiration_date_gte", "expiration_date_lte"}
        assert all(params.values())

    def test_from_dte_computes_inclusive_bounds(self) -> None:
        w = ExpiryWindow.from_dte(date(2026, 8, 31), 5, 14)
        assert w.gte == date(2026, 9, 5)
        assert w.lte == date(2026, 9, 14)

    def test_rejects_inverted_window(self) -> None:
        with pytest.raises(ValueError, match="inverted"):
            ExpiryWindow(gte=date(2026, 9, 14), lte=date(2026, 9, 5))

    def test_rejects_inverted_dte(self) -> None:
        with pytest.raises(ValueError, match="exceeds"):
            ExpiryWindow.from_dte(TODAY, 14, 5)

    @pytest.mark.parametrize("min_days", [0, -1])
    def test_rejects_zero_dte(self, min_days: int) -> None:
        # 0DTE has no Greeks on the Basic plan and pins the same session.
        with pytest.raises(ValueError, match="0DTE"):
            ExpiryWindow.from_dte(TODAY, min_days, 14)

    def test_contains_is_inclusive_at_both_edges(self) -> None:
        assert WINDOW.contains(WINDOW.gte)
        assert WINDOW.contains(WINDOW.lte)
        assert not WINDOW.contains(WINDOW.gte - timedelta(days=1))
        assert not WINDOW.contains(WINDOW.lte + timedelta(days=1))


class TestScreening:
    def _screen(self, c: Contract, policy: LiquidityPolicy | None = None) -> Rejection | None:
        return screen_contract(
            c,
            now=NOW,
            window=WINDOW,
            wanted=ContractType.CALL,
            policy=policy or LiquidityPolicy(),
        )

    def test_healthy_contract_passes(self) -> None:
        assert self._screen(contract(650)) is None

    def test_wrong_type_rejected(self) -> None:
        assert self._screen(contract(650, kind=ContractType.PUT)) is Rejection.WRONG_TYPE

    def test_expiry_outside_window_rejected(self) -> None:
        far = contract(650, expiry=date(2026, 12, 18))
        assert self._screen(far) is Rejection.OUTSIDE_EXPIRY_WINDOW

    def test_missing_quote_rejected(self) -> None:
        assert self._screen(contract(650, quote=False)) is Rejection.NO_QUOTE

    def test_zero_bid_rejected(self) -> None:
        # Nothing is willing to buy it, so we could never exit.
        assert self._screen(contract(650, bid=0.0, ask=0.10)) is Rejection.ZERO_BID

    def test_crossed_quote_rejected(self) -> None:
        assert self._screen(contract(650, bid=1.20, ask=1.10)) is Rejection.CROSSED_QUOTE

    def test_stale_quote_rejected(self) -> None:
        assert self._screen(contract(650, age_s=120)) is Rejection.STALE_QUOTE

    def test_wide_spread_rejected(self) -> None:
        # bid 1.00 / ask 2.00 -> mid 1.50, width 1.00, 66% of mid.
        assert self._screen(contract(650, bid=1.00, ask=2.00)) is Rejection.SPREAD_TOO_WIDE

    def test_low_open_interest_rejected(self) -> None:
        assert self._screen(contract(650, oi=5)) is Rejection.OPEN_INTEREST_TOO_LOW

    def test_missing_open_interest_tolerated_by_default(self) -> None:
        # Basic-plan chains omit OI often enough that failing on absence would
        # empty the universe.
        assert self._screen(contract(650, oi=None)) is None

    def test_missing_open_interest_rejected_when_required(self) -> None:
        policy = LiquidityPolicy(require_open_interest=True)
        assert self._screen(contract(650, oi=None), policy) is Rejection.OPEN_INTEREST_TOO_LOW


def _chain() -> list[Contract]:
    return [
        contract(640, bid=12.00, ask=12.10, delta=0.72),
        contract(645, bid=8.40, ask=8.50, delta=0.62),
        contract(650, bid=5.30, ask=5.40, delta=0.48),
        contract(655, bid=3.00, ask=3.10, delta=0.32),
        contract(660, bid=1.55, ask=1.62, delta=0.20),
        contract(665, bid=0.70, ask=0.76, delta=0.11),
    ]


SelectResult = tuple[VerticalSpread | None, Rejection | None, list[Rejected]]


def select_calls(
    chain: list[Contract],
    *,
    underlying_price: float | None = 647.0,
    deltas: DeltaPolicy | None = None,
    economics: SpreadEconomics | None = None,
) -> SelectResult:
    return select_vertical(
        chain,
        now=NOW,
        window=WINDOW,
        contract_type=ContractType.CALL,
        underlying_price=underlying_price,
        deltas=deltas,
        economics=economics,
    )


class TestSelection:
    def test_selects_legs_inside_the_target_delta_bands(self) -> None:
        spread, rejection, _ = select_calls(_chain())
        assert rejection is None
        assert spread is not None
        assert 0.55 <= abs(spread.long_leg.delta or 0) <= 0.70
        assert 0.20 <= abs(spread.short_leg.delta or 0) <= 0.35

    def test_does_not_chase_the_widest_cheapest_spread(self) -> None:
        # Reward:risk improves monotonically as the short leg moves further
        # out, so a naive max-R:R objective picks the 0.20-delta band edge.
        # Selection should sit near the band centre instead.
        spread, _, _ = select_calls(_chain())
        assert spread is not None
        assert spread.short_leg.strike == 655
        assert spread.long_leg.strike == 645

    def test_short_leg_is_further_out_than_long_leg_for_calls(self) -> None:
        spread, _, _ = select_calls(_chain())
        assert spread is not None
        assert spread.short_leg.strike > spread.long_leg.strike

    def test_economics_are_arithmetically_consistent(self) -> None:
        spread, _, _ = select_calls(_chain())
        assert spread is not None
        assert spread.max_loss == pytest.approx(spread.debit * 100)
        assert spread.max_profit == pytest.approx((spread.width - spread.debit) * 100)
        assert spread.reward_risk == pytest.approx(spread.max_profit / spread.max_loss)

    def test_debit_is_conservative_relative_to_mid(self) -> None:
        # We assume we cross half of each leg's quoted spread, so the modelled
        # debit must exceed the naive mid-to-mid figure.
        spread, _, _ = select_calls(_chain())
        assert spread is not None
        long_q, short_q = spread.long_leg.quote, spread.short_leg.quote
        assert long_q is not None and short_q is not None
        assert spread.debit > long_q.mid - short_q.mid

    def test_reward_risk_floor_is_enforced(self) -> None:
        spread, rejection, _ = select_calls(
            _chain(), economics=SpreadEconomics(min_reward_risk=99.0)
        )
        assert spread is None
        assert rejection is Rejection.REWARD_RISK_TOO_LOW

    def test_no_candidate_in_long_band_is_reported(self) -> None:
        spread, rejection, _ = select_calls(
            _chain(), deltas=DeltaPolicy(long_leg_min=0.95, long_leg_max=0.99)
        )
        assert spread is None
        assert rejection is Rejection.NO_LONG_LEG_CANDIDATE

    def test_empty_chain_is_reported_not_crashed(self) -> None:
        spread, rejection, _ = select_calls([])
        assert spread is None
        assert rejection is Rejection.NO_LONG_LEG_CANDIDATE

    def test_rejections_are_returned_for_display(self) -> None:
        chain = [*_chain(), contract(670, bid=0.0, ask=0.05, delta=0.05)]
        _, _, screened_out = select_calls(chain)
        assert any(r.reason is Rejection.ZERO_BID for r in screened_out)


class TestGreeksFallback:
    def test_falls_back_to_moneyness_when_delta_absent(self) -> None:
        chain = [
            contract(640, bid=12.00, ask=12.10, delta=None),
            contract(645, bid=8.40, ask=8.50, delta=None),
            contract(655, bid=3.00, ask=3.10, delta=None),
            contract(665, bid=0.70, ask=0.76, delta=None),
        ]
        spread, rejection, _ = select_vertical(
            chain,
            now=NOW,
            window=WINDOW,
            contract_type=ContractType.CALL,
            underlying_price=647.0,
        )
        assert rejection is None
        assert spread is not None
        # It must never invent a delta to get there.
        assert spread.long_leg.delta is None
        assert spread.short_leg.delta is None

    def test_no_delta_and_no_spot_refuses_rather_than_guessing(self) -> None:
        chain = [contract(s, delta=None) for s in (640, 650, 660)]
        spread, rejection, _ = select_vertical(
            chain,
            now=NOW,
            window=WINDOW,
            contract_type=ContractType.CALL,
            underlying_price=None,
        )
        assert spread is None
        assert rejection is Rejection.NO_UNDERLYING_PRICE


class TestPutSpreads:
    def test_short_leg_is_below_long_leg_for_puts(self) -> None:
        chain = [
            contract(655, bid=12.00, ask=12.10, delta=-0.72, kind=ContractType.PUT),
            contract(650, bid=8.40, ask=8.50, delta=-0.62, kind=ContractType.PUT),
            contract(640, bid=3.00, ask=3.10, delta=-0.32, kind=ContractType.PUT),
            contract(635, bid=1.55, ask=1.62, delta=-0.20, kind=ContractType.PUT),
        ]
        spread, rejection, _ = select_vertical(
            chain,
            now=NOW,
            window=WINDOW,
            contract_type=ContractType.PUT,
            underlying_price=647.0,
        )
        assert rejection is None
        assert spread is not None
        assert spread.short_leg.strike < spread.long_leg.strike
        assert spread.max_loss > 0
