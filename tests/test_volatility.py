"""Volatility ranking tests.

The load-bearing behaviour is refusal: a missing or implausible implied vol
must skip the instrument with a reason, never be filled in with an estimate.
A fabricated IV would produce a confident ranking built on nothing.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import pytest

from underwriter.volatility import (
    Skip,
    Skipped,
    VolPolicy,
    VolRanking,
    log_returns,
    rank_instrument,
    rank_universe,
    realised_volatility,
)


def flat(n: int = 40, price: float = 100.0) -> list[float]:
    return [price] * n


def walk(daily_sigma: float, n: int = 40, start: float = 100.0) -> list[float]:
    """Deterministic alternating series with a known per-step magnitude."""
    out = [start]
    for i in range(n):
        out.append(out[-1] * math.exp(daily_sigma * (1 if i % 2 else -1)))
    return out


class TestLogReturns:
    def test_computes_successive_log_returns(self) -> None:
        assert log_returns([100.0, 110.0]) == pytest.approx([math.log(1.1)])

    def test_length_is_one_less_than_input(self) -> None:
        assert len(log_returns([1.0, 2.0, 3.0, 4.0])) == 3

    @pytest.mark.parametrize("bad", [[100.0, 0.0], [-1.0, 100.0], [100.0, -5.0]])
    def test_non_positive_price_raises(self, bad: list[float]) -> None:
        # Bad data must stop the calculation, not silently poison it.
        with pytest.raises(ValueError, match="positive"):
            log_returns(bad)


class TestRealisedVolatility:
    def test_returns_none_without_enough_history(self) -> None:
        assert realised_volatility([100.0] * 5, window=20) is None

    def test_returns_none_for_degenerate_window(self) -> None:
        assert realised_volatility(flat(), window=1) is None

    def test_flat_series_has_zero_volatility(self) -> None:
        assert realised_volatility(flat(), window=20) == pytest.approx(0.0)

    def test_annualises_by_root_252(self) -> None:
        # Constant-magnitude alternating moves: stdev of returns is the step
        # size, annualised by sqrt(252).
        vol = realised_volatility(walk(0.01), window=20)
        assert vol is not None
        assert vol == pytest.approx(0.01 * math.sqrt(252), rel=0.05)

    def test_larger_moves_produce_larger_volatility(self) -> None:
        low = realised_volatility(walk(0.005), window=20)
        high = realised_volatility(walk(0.02), window=20)
        assert low is not None and high is not None
        assert high > low

    def test_non_positive_close_returns_none_rather_than_raising(self) -> None:
        series = [*flat(30), 0.0, *flat(10)]
        assert realised_volatility(series, window=20) is None


class TestRankInstrument:
    def _rank(
        self,
        *,
        closes: Sequence[float] | None = None,
        implied_vol: float | None = 0.30,
        policy: VolPolicy | None = None,
    ) -> VolRanking | Skipped:
        return rank_instrument(
            "XLE",
            closes=walk(0.01) if closes is None else closes,
            implied_vol=implied_vol,
            policy=policy or VolPolicy(),
        )

    def test_missing_implied_vol_skips_and_never_estimates(self) -> None:
        result = self._rank(implied_vol=None)
        assert isinstance(result, Skipped)
        assert result.reason is Skip.IMPLIED_VOL_MISSING
        assert "not estimated" in result.detail

    @pytest.mark.parametrize("iv", [0.0, -0.5, 5.0, float("nan"), float("inf")])
    def test_implausible_implied_vol_skips(self, iv: float) -> None:
        result = self._rank(implied_vol=iv)
        assert isinstance(result, Skipped)
        assert result.reason is Skip.IMPLIED_VOL_INVALID

    def test_insufficient_history_skips(self) -> None:
        result = self._rank(closes=flat(5))
        assert isinstance(result, Skipped)
        assert result.reason is Skip.INSUFFICIENT_HISTORY

    def test_zero_realised_vol_skips_rather_than_dividing(self) -> None:
        # A perfectly flat series would otherwise report infinite premium.
        result = self._rank(closes=flat())
        assert isinstance(result, Skipped)
        assert result.reason is Skip.REALISED_VOL_ZERO

    def test_non_positive_close_skips(self) -> None:
        result = self._rank(closes=[*flat(30), 0.0, *flat(10)])
        assert isinstance(result, Skipped)
        assert result.reason is Skip.NON_POSITIVE_PRICE

    def test_healthy_inputs_produce_a_ranking(self) -> None:
        result = self._rank()
        assert isinstance(result, VolRanking)
        assert result.implied_vol == 0.30
        assert result.realised_vol > 0


class TestPremiumArithmetic:
    def _ranking(self, iv: float, rv_sigma: float) -> VolRanking:
        result = rank_instrument("XLE", closes=walk(rv_sigma), implied_vol=iv, policy=VolPolicy())
        assert isinstance(result, VolRanking)
        return result

    def test_ratio_is_implied_over_realised(self) -> None:
        r = self._ranking(0.30, 0.01)
        assert r.vrp_ratio == pytest.approx(r.implied_vol / r.realised_vol)

    def test_points_is_the_difference(self) -> None:
        r = self._ranking(0.30, 0.01)
        assert r.vrp_points == pytest.approx(r.implied_vol - r.realised_vol)

    def test_ratio_denominator_is_the_tenor_window_not_the_context_window(self) -> None:
        # The correction found at kickoff. In a calming market a longer window
        # still carries the memory of a rougher stretch, so implied vol
        # correctly pricing the calm ahead reads as "no premium" against RV20
        # while showing a real premium against RV10. Measured on SPY: 0.83
        # against the slow window, 1.21 against the tenor-matched one.
        calming = walk(0.02, n=40) + walk(0.004, n=14, start=100.0)[1:]
        result = rank_instrument("SPY", closes=calming, implied_vol=0.12, policy=VolPolicy())
        assert isinstance(result, VolRanking)
        assert result.realised_vol_context is not None
        # Recent realised vol is far below the longer-run figure.
        assert result.realised_vol < result.realised_vol_context
        # And the ratio is computed against the responsive one.
        assert result.vrp_ratio == pytest.approx(result.implied_vol / result.realised_vol)
        assert result.vrp_ratio > result.implied_vol / result.realised_vol_context

    def test_ratio_is_comparable_across_different_vol_levels(self) -> None:
        # The reason we rank on a ratio: a high-vol and a low-vol instrument
        # with the same proportional richness must score the same, even though
        # their premium in vol points differs by a lot.
        quiet = self._ranking(0.15, 0.005)
        loud = self._ranking(0.60, 0.02)
        assert quiet.vrp_ratio == pytest.approx(loud.vrp_ratio, rel=0.05)
        assert loud.vrp_points > quiet.vrp_points * 2


class TestRankUniverse:
    def _inputs(self) -> Mapping[str, tuple[Sequence[float], float | None]]:
        return {
            "RICH": (walk(0.005), 0.40),
            "FAIR": (walk(0.01), 0.16),
            "CHEAP": (walk(0.02), 0.10),
            "NOIV": (walk(0.01), None),
        }

    def test_only_instruments_above_the_floor_become_candidates(self) -> None:
        ranked, _ = rank_universe(self._inputs())
        assert [r.symbol for r in ranked] == ["RICH"]

    def test_candidates_are_ordered_richest_first(self) -> None:
        inputs: Mapping[str, tuple[Sequence[float], float | None]] = {
            "A": (walk(0.005), 0.40),
            "B": (walk(0.005), 0.25),
            "C": (walk(0.005), 0.60),
        }
        ranked, _ = rank_universe(inputs)
        ratios = [r.vrp_ratio for r in ranked]
        assert ratios == sorted(ratios, reverse=True)
        assert ranked[0].symbol == "C"

    def test_below_floor_is_recorded_not_silently_dropped(self) -> None:
        # "We looked and it was not rich enough" is a decision worth showing.
        _, skipped = rank_universe(self._inputs())
        reasons = {s.symbol: s.reason for s in skipped}
        assert reasons["CHEAP"] is Skip.PREMIUM_BELOW_FLOOR
        assert reasons["NOIV"] is Skip.IMPLIED_VOL_MISSING

    def test_every_instrument_is_accounted_for(self) -> None:
        inputs = self._inputs()
        ranked, skipped = rank_universe(inputs)
        assert len(ranked) + len(skipped) == len(inputs)

    def test_floor_is_configurable(self) -> None:
        strict, _ = rank_universe(self._inputs(), policy=VolPolicy(min_vrp_ratio=1.15))
        loose, _ = rank_universe(self._inputs(), policy=VolPolicy(min_vrp_ratio=0.5))
        assert len(loose) > len(strict)

    def test_empty_universe_returns_empty(self) -> None:
        ranked, skipped = rank_universe({})
        assert ranked == [] and skipped == []


class TestExpansionWarning:
    def test_expanding_short_window_is_flagged(self) -> None:
        # Quiet for 30 sessions then violent for 10: the short window should
        # exceed the long one, which is a warning against selling premium.
        closes = walk(0.002, n=30) + walk(0.03, n=10, start=100.0)[1:]
        result = rank_instrument("XLE", closes=closes, implied_vol=0.5, policy=VolPolicy())
        assert isinstance(result, VolRanking)
        assert result.realised_is_expanding is True

    def test_stable_series_is_not_flagged(self) -> None:
        # The two windows differ by a few percent from sampling alone. That
        # must not read as expansion.
        result = rank_instrument("XLE", closes=walk(0.01), implied_vol=0.3, policy=VolPolicy())
        assert isinstance(result, VolRanking)
        assert result.realised_vol_context is not None
        # The two windows differ by a few percent from sampling alone...
        assert result.realised_vol != result.realised_vol_context
        # ...but that must not read as expansion.
        assert result.realised_is_expanding is False

    def test_margin_is_configurable(self) -> None:
        closes = walk(0.002, n=30) + walk(0.03, n=10, start=100.0)[1:]
        strict = rank_instrument(
            "XLE", closes=closes, implied_vol=0.5, policy=VolPolicy(expansion_margin=10.0)
        )
        assert isinstance(strict, VolRanking)
        assert strict.realised_is_expanding is False
