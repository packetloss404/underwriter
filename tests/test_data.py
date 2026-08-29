"""Market data conversion tests.

The conversion functions are pure and take already-fetched responses, so the
mapping from Alpaca's shapes into domain objects is testable without
credentials. That matters: this is the layer where a wrong assumption about a
response silently becomes a thin chain or a missing gate input.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace as NS
from typing import Any

import pytest

from underwriter.chain import ContractType
from underwriter.data import (
    contracts_from_chain,
    delta_from,
    deltas_from,
    mids_from,
    quote_from,
    term_structure_from,
)

NOW = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)


def snap(
    bid: float | None = 1.00,
    ask: float | None = 1.10,
    *,
    iv: float | None = None,
    delta: float | None = None,
    quote: bool = True,
    ts: datetime = NOW,
) -> Any:
    return NS(
        latest_quote=NS(bid_price=bid, ask_price=ask, timestamp=ts) if quote else None,
        implied_volatility=iv,
        greeks=NS(delta=delta) if delta is not None else None,
    )


class TestQuoteConversion:
    def test_builds_a_quote(self) -> None:
        q = quote_from(snap(2.35, 2.45))
        assert q is not None
        assert q.bid == pytest.approx(2.35)
        assert q.ask == pytest.approx(2.45)
        assert q.mid == pytest.approx(2.40)

    def test_absent_quote_yields_none(self) -> None:
        # None and a zeroed quote mean different things downstream: screening
        # distinguishes "no quote" from "zero bid".
        assert quote_from(snap(quote=False)) is None

    @pytest.mark.parametrize(("bid", "ask"), [(None, 1.1), (1.0, None), (None, None)])
    def test_partial_quote_yields_none(self, bid: float | None, ask: float | None) -> None:
        assert quote_from(snap(bid, ask)) is None

    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        # A naive timestamp compared against an aware "now" raises, which would
        # surface as a crash in the staleness check rather than a data problem.
        naive = datetime(2026, 8, 31, 14, 0)  # noqa: DTZ001 - naive on purpose
        q = quote_from(snap(ts=naive))
        assert q is not None
        assert q.as_of.tzinfo is not None

    def test_zero_bid_is_preserved_not_dropped(self) -> None:
        q = quote_from(snap(0.0, 0.05))
        assert q is not None
        assert q.bid == 0.0


class TestDeltaConversion:
    def test_extracts_delta_when_present(self) -> None:
        assert delta_from(snap(delta=-0.26)) == pytest.approx(-0.26)

    def test_absent_greeks_yield_none_not_zero(self) -> None:
        # Zero delta is a real value. Conflating it with "missing" would make
        # a contract look at-the-money when we simply cannot see it.
        assert delta_from(snap()) is None

    def test_greeks_without_delta_yield_none(self) -> None:
        assert delta_from(NS(latest_quote=None, implied_volatility=None, greeks=NS())) is None


class TestChainConversion:
    def _chain(self) -> dict[str, Any]:
        return {
            "SPY260911P00637000": snap(2.35, 2.45, iv=0.131, delta=-0.26),
            "SPY260911C00650000": snap(5.30, 5.40, iv=0.128, delta=0.48),
        }

    def test_maps_symbols_strikes_and_types(self) -> None:
        by_symbol = {c.symbol: c for c in contracts_from_chain(self._chain(), underlying="SPY")}
        put = by_symbol["SPY260911P00637000"]
        assert put.contract_type is ContractType.PUT
        assert put.strike == pytest.approx(637.0)
        assert put.underlying == "SPY"
        assert by_symbol["SPY260911C00650000"].contract_type is ContractType.CALL

    def test_unparseable_key_is_skipped_not_fatal(self) -> None:
        chain = {**self._chain(), "NOT-AN-OCC-SYMBOL": snap()}
        assert len(contracts_from_chain(chain, underlying="SPY")) == 2

    def test_open_interest_is_none_when_not_supplied(self) -> None:
        # The chain snapshot does not carry it; it lives on the contracts
        # endpoint. See docs/GOTCHAS.md #4.
        assert all(
            c.open_interest is None for c in contracts_from_chain(self._chain(), underlying="SPY")
        )

    def test_open_interest_is_joined_when_supplied(self) -> None:
        contracts = contracts_from_chain(
            self._chain(), underlying="SPY", open_interest={"SPY260911P00637000": 8123}
        )
        by_symbol = {c.symbol: c for c in contracts}
        assert by_symbol["SPY260911P00637000"].open_interest == 8123
        assert by_symbol["SPY260911C00650000"].open_interest is None

    def test_empty_chain_yields_no_contracts(self) -> None:
        assert contracts_from_chain({}, underlying="SPY") == []


def near_slice() -> dict[str, Any]:
    return {
        "SPY260911P00637000": snap(2.35, 2.45, iv=0.131),
        "SPY260911C00650000": snap(5.30, 5.40, iv=0.128),
    }


def far_slice() -> dict[str, Any]:
    return {"SPY261016P00630000": snap(9.0, 9.2, iv=0.158)}


class TestTermStructure:
    def test_builds_a_curve_from_two_slices(self) -> None:
        ts = term_structure_from(near_slice(), far_slice(), underlying_price=647.0)
        assert ts is not None
        assert ts.far_iv == pytest.approx(0.158)
        assert ts.is_contango

    def test_picks_the_strike_nearest_the_money(self) -> None:
        # 650 is 3 away from spot; 637 is 10 away.
        ts = term_structure_from(near_slice(), far_slice(), underlying_price=647.0)
        assert ts is not None
        assert ts.near_iv == pytest.approx(0.128)

    def test_missing_near_implied_vol_yields_no_curve(self) -> None:
        # The regime filter blocks on a missing curve, so refusing to build one
        # is the safe direction.
        blank = {"SPY260911P00637000": snap(2.35, 2.45)}
        assert term_structure_from(blank, far_slice(), underlying_price=647.0) is None

    def test_missing_far_implied_vol_yields_no_curve(self) -> None:
        blank = {"SPY261016P00630000": snap(9.0, 9.2)}
        assert term_structure_from(near_slice(), blank, underlying_price=647.0) is None

    def test_non_positive_implied_vol_is_ignored(self) -> None:
        near = {"SPY260911C00650000": snap(5.3, 5.4, iv=0.0), **near_slice()}
        ts = term_structure_from(near, far_slice(), underlying_price=647.0)
        assert ts is not None
        assert ts.near_iv > 0

    def test_inverted_curve_is_detected(self) -> None:
        stressed = {"SPY260911C00650000": snap(5.3, 5.4, iv=0.30)}
        ts = term_structure_from(stressed, far_slice(), underlying_price=647.0)
        assert ts is not None
        assert ts.ratio > 1.0
        assert not ts.is_contango

    def test_empty_slice_yields_no_curve(self) -> None:
        assert term_structure_from({}, far_slice(), underlying_price=647.0) is None


class TestOptionPricingForHeldPositions:
    """The exit path cannot reuse the scan's chains.

    A held position routinely sits outside the entry window -- opened days ago
    and decaying toward an expiry the current 5-14 day slice no longer covers --
    so pricing it from those chains would silently return nothing and every
    exit trigger would read "cannot price" forever.
    """

    def test_mids_are_computed_per_contract(self) -> None:
        mids = mids_from({"A": snap(0.18, 0.22), "B": snap(0.04, 0.06)})
        assert mids == {"A": pytest.approx(0.20), "B": pytest.approx(0.05)}

    def test_an_unquotable_contract_is_none_not_zero(self) -> None:
        # exits.closing_debit treats None as "cannot evaluate"; a zero would
        # read as costless to close.
        assert mids_from({"A": snap(quote=False)}) == {"A": None}

    def test_a_zero_mid_is_none(self) -> None:
        assert mids_from({"A": snap(0.0, 0.0)}) == {"A": None}

    def test_deltas_are_never_estimated(self) -> None:
        deltas = deltas_from({"A": snap(delta=-0.25), "B": snap()})
        assert deltas == {"A": pytest.approx(-0.25), "B": None}

    def test_an_empty_request_is_an_empty_answer(self) -> None:
        assert mids_from({}) == {}
        assert deltas_from({}) == {}


class TestMarketSourceContract:
    """MarketData must satisfy the cycle's MarketSource protocol.

    Regression. `option_snapshots` was deleted during a refactor that
    simultaneously rewrote its tests to exercise the pure helpers instead, so
    the suite stayed green while the method the live wiring depends on had
    silently gone. Testing the extracted logic is not the same as testing that
    the caller can still reach it.
    """

    def test_market_data_exposes_every_member_the_cycle_asks_for(self) -> None:
        from underwriter.data import MarketData

        for member in ("daily_closes", "chain", "option_snapshots"):
            assert callable(getattr(MarketData, member, None)), member

    def test_an_empty_symbol_list_does_not_call_the_api(self) -> None:
        # A snapshot request naming no symbols is an error, not an empty
        # answer, so the short-circuit must survive too.
        from underwriter.data import MarketData

        market = MarketData.__new__(MarketData)
        assert market.option_snapshots([]) == {}

    def test_live_market_adapter_satisfies_the_protocol(self) -> None:
        from underwriter.live import LiveMarket

        for member in ("daily_closes", "chain", "option_snapshots"):
            assert callable(getattr(LiveMarket, member, None)), member
