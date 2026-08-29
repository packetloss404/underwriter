"""Universe tests.

The load-bearing behaviour is that a provisional instrument cannot reach the
live path. The strategy trades ETFs specifically because penny-wide contracts
are where an indicative quote is closest to true, so admitting an unmeasured
name on the assumption that it is liquid would quietly undo that rationale.
"""

from __future__ import annotations

import pytest

from underwriter import universe as u


class TestMembership:
    def test_live_universe_excludes_provisional_by_default(self) -> None:
        live = set(u.symbols())
        assert live.isdisjoint(u.provisional_symbols())

    def test_provisional_are_included_when_asked(self) -> None:
        full = set(u.symbols(include_provisional=True))
        assert full == set(u.symbols()) | set(u.provisional_symbols())

    def test_provisional_symbol_is_not_tradeable_by_default(self) -> None:
        for symbol in u.provisional_symbols():
            assert not u.is_tradeable(symbol)
            assert u.is_tradeable(symbol, include_provisional=True)

    def test_unknown_symbol_is_never_tradeable(self) -> None:
        assert not u.is_tradeable("GME")
        assert not u.is_tradeable("GME", include_provisional=True)

    def test_every_live_symbol_is_tradeable(self) -> None:
        assert all(u.is_tradeable(s) for s in u.symbols())

    def test_symbols_are_unique(self) -> None:
        full = u.symbols(include_provisional=True)
        assert len(full) == len(set(full))


class TestSectorMapping:
    def test_known_sector_resolves_to_an_instrument(self) -> None:
        inst = u.instrument_for_sector(u.SECTOR_ENERGY)
        assert inst is not None
        assert inst.symbol == "XLE"

    def test_untradeable_sector_returns_none_rather_than_raising(self) -> None:
        # A disclosure or catalyst can name a sector we have no clean
        # instrument for. That is a normal skip, not an error.
        assert u.instrument_for_sector("real_estate") is None

    def test_every_mapped_sector_resolves(self) -> None:
        for inst in (u.BY_SYMBOL[s] for s in u.symbols(include_provisional=True)):
            mapped = u.instrument_for_sector(inst.sector)
            assert mapped is not None, f"{inst.sector} maps to nothing"


class TestCorrelation:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("SPY", "QQQ"),
            ("QQQ", "XLK"),
            ("XLK", "SMH"),
            ("TLT", "GLD"),
            ("GLD", "SLV"),
            ("XLE", "USO"),
            ("FXI", "EWZ"),
        ],
    )
    def test_overlapping_exposures_are_correlated(self, a: str, b: str) -> None:
        assert u.are_correlated(a, b)
        assert u.are_correlated(b, a), "correlation must be symmetric"

    @pytest.mark.parametrize(("a", "b"), [("XLE", "XLF"), ("USO", "SLV"), ("TLT", "XLV")])
    def test_unrelated_exposures_are_not_correlated(self, a: str, b: str) -> None:
        assert not u.are_correlated(a, b)

    def test_an_instrument_is_not_correlated_with_itself(self) -> None:
        # Self-overlap is the duplicate-symbol gate's job, not this one.
        assert not u.are_correlated("SPY", "SPY")

    def test_correlated_with_returns_every_partner(self) -> None:
        assert u.correlated_with("SPY") == frozenset({"QQQ", "IWM", "XLK"})
        assert u.correlated_with("SLV") == frozenset({"GLD"})

    def test_instrument_with_no_partners_returns_empty(self) -> None:
        assert u.correlated_with("XLV") == frozenset()


class TestDiversification:
    def test_the_diversifiers_are_not_equity_beta(self) -> None:
        # The point of adding them: twelve of the original sixteen fall
        # together, and gates can only contain that rather than fix it.
        equity_beta = {"SPY", "QQQ", "IWM", "XLK", "SMH"}
        for symbol in ("USO", "SLV", "FXI", "EWZ", "TLT", "GLD"):
            assert not any(u.are_correlated(symbol, e) for e in equity_beta), (
                f"{symbol} should diversify away from equity beta"
            )

    def test_each_diversifier_has_a_distinct_sector(self) -> None:
        sectors = [u.BY_SYMBOL[s].sector for s in ("USO", "SLV", "FXI", "EWZ")]
        assert len(set(sectors)) == len(sectors)
