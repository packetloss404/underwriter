"""Broker reader tests.

The load-bearing behaviour is the sign of `qty`. Alpaca reports the magnitude
in `qty` and the direction separately in `side`, so a short leg read as
positive pairs as a long one and inverts the whole spread -- a put credit
spread would reassemble as a debit spread with the risk on the wrong side.
"""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import pytest

from underwriter.account import AlpacaBroker, to_raw_option_position


def position(
    *,
    symbol: str = "SPY260911P00760000",
    qty: Any = "2",
    side: str = "short",
    asset_class: str = "us_option",
    avg_entry_price: Any = "0.42",
    current_price: Any = "0.20",
) -> Any:
    return NS(
        symbol=symbol,
        qty=qty,
        side=side,
        asset_class=asset_class,
        avg_entry_price=avg_entry_price,
        current_price=current_price,
    )


class TestQuantitySign:
    def test_a_short_position_is_negative(self) -> None:
        p = to_raw_option_position(position(side="short", qty="2"))
        assert p is not None
        assert p.qty == -2.0

    def test_a_long_position_is_positive(self) -> None:
        p = to_raw_option_position(position(side="long", qty="2"))
        assert p is not None
        assert p.qty == 2.0

    def test_an_already_negative_short_stays_negative(self) -> None:
        # Belt and braces: some clients report the sign in qty as well.
        p = to_raw_option_position(position(side="short", qty="-2"))
        assert p is not None
        assert p.qty == -2.0

    def test_side_wins_over_an_inconsistent_qty_sign(self) -> None:
        p = to_raw_option_position(position(side="long", qty="-2"))
        assert p is not None
        assert p.qty == 2.0

    def test_an_unknown_side_preserves_the_reported_sign(self) -> None:
        p = to_raw_option_position(position(side="", qty="-2"))
        assert p is not None
        assert p.qty == -2.0


class TestFiltering:
    def test_an_equity_position_is_ignored(self) -> None:
        # A stray share position must never be mistaken for a leg.
        assert to_raw_option_position(position(asset_class="us_equity")) is None

    def test_a_position_without_a_symbol_is_ignored(self) -> None:
        assert to_raw_option_position(position(symbol="")) is None

    @pytest.mark.parametrize("qty", ["", "n/a", None])
    def test_an_unreadable_quantity_is_ignored(self, qty: Any) -> None:
        assert to_raw_option_position(position(qty=qty)) is None

    def test_a_missing_asset_class_is_accepted(self) -> None:
        # Not every client populates it; the OCC symbol is the real filter and
        # reassembly rejects anything unparseable anyway.
        p = to_raw_option_position(position(asset_class=""))
        assert p is not None


class TestOptionalPrices:
    def test_prices_are_parsed_when_present(self) -> None:
        p = to_raw_option_position(position())
        assert p is not None
        assert p.avg_entry_price == pytest.approx(0.42)
        assert p.current_price == pytest.approx(0.20)

    @pytest.mark.parametrize("value", [None, "", "n/a"])
    def test_an_unreadable_price_is_none_not_zero(self, value: Any) -> None:
        # Zero would read as a free position; None reads as unknown.
        p = to_raw_option_position(position(current_price=value))
        assert p is not None
        assert p.current_price is None


class TestBroker:
    def _client(self, positions: list[Any], account: Any = None) -> Any:
        return NS(
            get_all_positions=lambda: positions,
            get_account=lambda: (
                account
                or NS(
                    status="ACTIVE",
                    trading_blocked=False,
                    account_blocked=False,
                    equity="100000",
                    options_trading_level="3",
                    options_approved_level="3",
                    options_buying_power="100000",
                    last_equity="100000",
                    cash="100000",
                    buying_power="400000",
                )
            ),
            get_clock=lambda: NS(is_open=False),
        )

    def test_reads_option_positions(self) -> None:
        broker = AlpacaBroker(self._client([position(), position(side="long")]))
        assert len(broker.positions()) == 2

    def test_skips_what_it_cannot_read(self) -> None:
        broker = AlpacaBroker(
            self._client([position(), position(asset_class="us_equity"), position(qty="x")])
        )
        assert len(broker.positions()) == 1

    def test_an_empty_book_is_not_an_error(self) -> None:
        assert AlpacaBroker(self._client([])).positions() == ()

    def test_an_unreadable_response_yields_no_positions_rather_than_raising(self) -> None:
        # The position read must not be the thing that takes the cycle down:
        # an empty book is visible to the snapshot diff, an exception is not.
        broker = AlpacaBroker(NS(get_all_positions=lambda: None, get_account=lambda: None))
        assert broker.positions() == ()

    def test_account_fields_map_across(self) -> None:
        account = AlpacaBroker(self._client([])).account()
        assert account.status == "ACTIVE"
        assert account.options_trading_level == "3"
        assert account.last_equity == "100000"

    def test_account_satisfies_what_preflight_reads(self) -> None:
        from underwriter.preflight import check_account, check_options_level

        account = AlpacaBroker(self._client([])).account()
        assert all(c.status.value == "ok" for c in check_account(account))
        assert all(c.status.value in {"ok", "warn"} for c in check_options_level(account))
