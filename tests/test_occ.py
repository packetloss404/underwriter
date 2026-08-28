"""OCC symbol parsing tests.

A chain key we mis-parse becomes a contract silently dropped, which presents as
a thin chain -- indistinguishable from a liquidity problem. So the parser
raises rather than skipping, and these tests cover the shapes that would
otherwise slip through.
"""

from __future__ import annotations

from datetime import date

import pytest

from underwriter.occ import OccParseError, parse


class TestWellFormed:
    def test_parses_a_put(self) -> None:
        occ = parse("SPY260911P00637000")
        assert occ.root == "SPY"
        assert occ.expiry == date(2026, 9, 11)
        assert occ.is_put
        assert not occ.is_call
        assert occ.strike == pytest.approx(637.0)

    def test_parses_a_call(self) -> None:
        occ = parse("QQQ260904C00580000")
        assert occ.root == "QQQ"
        assert occ.is_call
        assert occ.strike == pytest.approx(580.0)

    def test_parses_a_fractional_strike(self) -> None:
        assert parse("IWM260911P00237500").strike == pytest.approx(237.5)

    def test_parses_a_sub_dollar_strike(self) -> None:
        assert parse("UNG260911C00000500").strike == pytest.approx(0.5)

    def test_parses_a_four_figure_strike(self) -> None:
        assert parse("SPX261218C05000000").strike == pytest.approx(5000.0)

    @pytest.mark.parametrize("root", ["A", "GLD", "EWZ", "SOXL"])
    def test_handles_varying_root_lengths(self, root: str) -> None:
        # Anchoring to the right is what makes this work: roots vary, every
        # field after them is fixed width.
        assert parse(f"{root}260911P00100000").root == root

    def test_handles_a_dotted_root(self) -> None:
        assert parse("BRK.B260911P00400000").root == "BRK.B"

    def test_lowercase_input_is_normalised(self) -> None:
        assert parse("spy260911p00637000").root == "SPY"

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        assert parse("  SPY260911P00637000  ").strike == pytest.approx(637.0)

    def test_two_digit_year_resolves_into_this_century(self) -> None:
        assert parse("SPY990911P00100000").expiry.year == 2099


class TestMalformed:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "SPY",
            "SPY260911",
            "SPY260911X00637000",  # right is neither C nor P
            "SPY26091P00637000",  # five-digit date
            "SPY260911P0063700",  # seven-digit strike
            "SPY260911P006370000",  # nine-digit strike
            "260911P00637000",  # no root
            "SPY260911P0063700A",  # non-numeric strike
            "SPY 260911P00637000",  # internal space
        ],
    )
    def test_malformed_symbols_raise(self, bad: str) -> None:
        with pytest.raises(OccParseError):
            parse(bad)

    @pytest.mark.parametrize("bad", ["SPY261301P00637000", "SPY260931P00637000"])
    def test_impossible_dates_raise_with_context(self, bad: str) -> None:
        # Month 13 and 31 September are structurally valid but not real dates.
        with pytest.raises(OccParseError, match="impossible expiry"):
            parse(bad)

    def test_error_names_the_offending_symbol(self) -> None:
        with pytest.raises(OccParseError, match="NONSENSE"):
            parse("NONSENSE")


class TestRoundTrip:
    @pytest.mark.parametrize(
        ("symbol", "strike", "expiry"),
        [
            ("SPY260911P00637000", 637.0, date(2026, 9, 11)),
            ("XLE260918C00095500", 95.5, date(2026, 9, 18)),
            ("TLT261016P00088000", 88.0, date(2026, 10, 16)),
        ],
    )
    def test_known_symbols(self, symbol: str, strike: float, expiry: date) -> None:
        occ = parse(symbol)
        assert occ.strike == pytest.approx(strike)
        assert occ.expiry == expiry
        assert occ.symbol == symbol
