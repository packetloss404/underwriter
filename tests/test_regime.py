"""Regime filter tests.

The filter is a global no-entry gate, so it has two ways to be badly wrong and
both are tested: failing open (permitting entries in a hostile tape) and
failing shut (blocking every entry for the whole window, which looks identical
to working correctly while the agent never trades).
"""

from __future__ import annotations

from datetime import date

import pytest

from underwriter.regime import (
    KNOWN_CATALYST_EVENTS,
    RegimeBlock,
    RegimePolicy,
    TermStructure,
    check_drawdown,
    check_term_structure,
    check_trend,
    check_volatility_expansion,
    evaluate_regime,
    scheduled_advisories,
)

# A quiet Monday in the judged window.
CALM_DAY = date(2026, 8, 31)


def rising(n: int = 30, start: float = 600.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(n)]


def falling(n: int = 30, start: float = 650.0, step: float = -1.5) -> list[float]:
    return [start + i * step for i in range(n)]


class TestTrend:
    def test_above_average_permits(self) -> None:
        assert check_trend(rising(), RegimePolicy()) is None

    def test_below_average_blocks(self) -> None:
        block = check_trend(falling(), RegimePolicy())
        assert block is not None
        assert block.reason is RegimeBlock.BENCHMARK_BELOW_TREND

    def test_insufficient_history_blocks_rather_than_permits(self) -> None:
        # If we cannot see whether the regime is safe, we assume it is not.
        block = check_trend([600.0] * 5, RegimePolicy())
        assert block is not None
        assert block.reason is RegimeBlock.BENCHMARK_HISTORY_MISSING
        assert "blocked" in block.detail


class TestDrawdown:
    def test_stable_tape_permits(self) -> None:
        assert check_drawdown(rising(), RegimePolicy()) is None

    def test_sharp_recent_decline_blocks(self) -> None:
        closes = [*rising(27), 627.0, 618.0, 610.0]
        block = check_drawdown(closes, RegimePolicy())
        assert block is not None
        assert block.reason is RegimeBlock.BENCHMARK_DRAWDOWN

    def test_catches_a_fast_drop_still_above_the_slow_average(self) -> None:
        # The case the trend filter misses, and the reason both checks exist:
        # a steep uptrend leaves the 20-session mean far below spot, so a sharp
        # 2.5% three-day drop is still above trend while the tape is already
        # disorderly.
        closes = [*rising(27, start=600.0, step=6.0), 748.0, 740.0, 737.0]
        policy = RegimePolicy()
        assert check_trend(closes, policy) is None, "should still be above trend"
        block = check_drawdown(closes, policy)
        assert block is not None
        assert block.reason is RegimeBlock.BENCHMARK_DRAWDOWN

    def test_threshold_is_inclusive(self) -> None:
        closes = [*rising(27), 100.0, 100.0, 98.0]
        block = check_drawdown(closes, RegimePolicy(drawdown_lookback=2, max_drawdown_pct=2.0))
        assert block is not None

    def test_insufficient_history_blocks(self) -> None:
        block = check_drawdown([600.0, 601.0], RegimePolicy())
        assert block is not None
        assert block.reason is RegimeBlock.BENCHMARK_HISTORY_MISSING


class TestVolatilityExpansion:
    def test_quiet_universe_permits(self) -> None:
        assert check_volatility_expansion([False] * 16, RegimePolicy()) is None

    def test_widespread_expansion_blocks(self) -> None:
        block = check_volatility_expansion([True] * 10 + [False] * 6, RegimePolicy())
        assert block is not None
        assert block.reason is RegimeBlock.VOLATILITY_EXPANDING

    def test_isolated_expansion_is_tolerated(self) -> None:
        # One instrument moving is idiosyncratic, not a market event.
        assert check_volatility_expansion([True] + [False] * 15, RegimePolicy()) is None

    def test_empty_input_does_not_block(self) -> None:
        # No ranked instruments is a separate problem, not a regime signal.
        assert check_volatility_expansion([], RegimePolicy()) is None


class TestEvaluateRegime:
    def test_calm_market_permits_entry(self) -> None:
        verdict = evaluate_regime(
            benchmark_closes=rising(),
            expanding_flags=[False] * 16,
            term_structure=CONTANGO,
            today=CALM_DAY,
        )
        assert verdict.may_open
        assert verdict.blocks == ()

    def test_calendar_never_blocks_the_judged_window(self) -> None:
        for day in (
            date(2026, 8, 31),
            date(2026, 9, 1),
            date(2026, 9, 2),
            date(2026, 9, 3),
            date(2026, 9, 4),
        ):
            verdict = evaluate_regime(
                benchmark_closes=rising(),
                expanding_flags=[False] * 16,
                term_structure=CONTANGO,
                today=day,
            )
            assert verdict.may_open, f"{day} should remain eligible in a calm tape"

    def test_blocks_accumulate_rather_than_short_circuit(self) -> None:
        closes = [*falling(27), 560.0, 550.0, 540.0]
        verdict = evaluate_regime(
            benchmark_closes=closes, expanding_flags=[True] * 12 + [False] * 4, today=CALM_DAY
        )
        assert not verdict.may_open
        assert RegimeBlock.BENCHMARK_BELOW_TREND in verdict.reasons
        assert RegimeBlock.BENCHMARK_DRAWDOWN in verdict.reasons
        assert RegimeBlock.VOLATILITY_EXPANDING in verdict.reasons

    def test_missing_history_is_reported_once_not_twice(self) -> None:
        # Both price checks emit it; the dashboard should show one line.
        verdict = evaluate_regime(benchmark_closes=[600.0] * 3, today=CALM_DAY)
        assert verdict.reasons.count(RegimeBlock.BENCHMARK_HISTORY_MISSING) == 1

    def test_every_block_carries_readable_detail(self) -> None:
        verdict = evaluate_regime(
            benchmark_closes=falling(), expanding_flags=[True] * 16, today=date(2026, 9, 3)
        )
        assert not verdict.may_open
        assert all(b.detail for b in verdict.blocks)

    def test_calendar_keeps_nfp_as_advisory_context(self) -> None:
        assert [(e.on, e.name) for e in KNOWN_CATALYST_EVENTS] == [
            (date(2026, 9, 1), "JOLTS, 10:00 ET"),
            (date(2026, 9, 3), "Productivity and Costs (revised), 08:30 ET"),
            (
                date(2026, 9, 4),
                "Employment Situation (non-farm payrolls), 08:30 ET",
            ),
        ]
        assert scheduled_advisories(date(2026, 9, 4)) == (
            "2026-09-04: Employment Situation (non-farm payrolls), 08:30 ET",
        )

    @pytest.mark.parametrize("day", [date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2)])
    def test_hostile_tape_blocks_even_on_permitted_days(self, day: date) -> None:
        verdict = evaluate_regime(
            benchmark_closes=falling(), expanding_flags=[False] * 16, today=day
        )
        assert not verdict.may_open


def curve(near: float, far: float, near_dte: int = 7, far_dte: int = 45) -> TermStructure:
    return TermStructure(near_iv=near, far_iv=far, near_dte=near_dte, far_dte=far_dte)


CONTANGO = curve(0.11, 0.135)


class TestTermStructure:
    """The volatility curve is the only forward-looking input in the filter.
    Trend and drawdown describe what already happened; an inverted curve says
    the market expects the near term to be worse than the far term, which is
    the condition under which short-volatility books take their worst losses."""

    def test_healthy_contango_permits(self) -> None:
        assert check_term_structure(CONTANGO, RegimePolicy()) is None

    def test_contango_ratio_is_below_one(self) -> None:
        assert CONTANGO.ratio < 1.0
        assert CONTANGO.is_contango

    def test_inverted_curve_blocks(self) -> None:
        block = check_term_structure(curve(0.28, 0.205), RegimePolicy())
        assert block is not None
        assert block.reason is RegimeBlock.TERM_STRUCTURE_INVERTED

    def test_backwardation_is_not_contango(self) -> None:
        inverted = curve(0.28, 0.205)
        assert inverted.ratio > 1.0
        assert not inverted.is_contango

    def test_flat_curve_just_under_the_threshold_permits(self) -> None:
        assert check_term_structure(curve(0.140, 0.142), RegimePolicy()) is None

    def test_missing_curve_blocks_rather_than_permits(self) -> None:
        block = check_term_structure(None, RegimePolicy())
        assert block is not None
        assert block.reason is RegimeBlock.TERM_STRUCTURE_MISSING

    def test_expiries_too_close_together_block(self) -> None:
        # Both readings sit at the same point on the curve, so the ratio
        # measures noise rather than term structure.
        block = check_term_structure(curve(0.11, 0.12, 7, 14), RegimePolicy())
        assert block is not None
        assert block.reason is RegimeBlock.TERM_STRUCTURE_MISSING
        assert "noise" in block.detail

    @pytest.mark.parametrize(("near", "far"), [(0.0, 0.13), (0.13, 0.0), (-0.1, 0.13)])
    def test_non_positive_implied_vol_blocks(self, near: float, far: float) -> None:
        block = check_term_structure(curve(near, far), RegimePolicy())
        assert block is not None
        assert block.reason is RegimeBlock.TERM_STRUCTURE_MISSING

    def test_threshold_is_configurable(self) -> None:
        slightly_inverted = curve(0.145, 0.140)
        assert check_term_structure(slightly_inverted, RegimePolicy()) is not None
        tolerant = RegimePolicy(max_term_structure_ratio=1.10)
        assert check_term_structure(slightly_inverted, tolerant) is None


class TestTermStructureInRegime:
    def test_inverted_curve_blocks_an_otherwise_calm_market(self) -> None:
        # Trend and drawdown are both fine. Only the forward-looking input
        # sees the problem, which is the whole reason it was added.
        verdict = evaluate_regime(
            benchmark_closes=rising(),
            expanding_flags=[False] * 16,
            term_structure=curve(0.28, 0.205),
            today=CALM_DAY,
        )
        assert not verdict.may_open
        assert RegimeBlock.TERM_STRUCTURE_INVERTED in verdict.reasons

    def test_contango_permits_a_calm_market(self) -> None:
        verdict = evaluate_regime(
            benchmark_closes=rising(),
            expanding_flags=[False] * 16,
            term_structure=CONTANGO,
            today=CALM_DAY,
        )
        assert verdict.may_open

    def test_absent_curve_blocks_by_default(self) -> None:
        verdict = evaluate_regime(
            benchmark_closes=rising(), expanding_flags=[False] * 16, today=CALM_DAY
        )
        assert not verdict.may_open
        assert RegimeBlock.TERM_STRUCTURE_MISSING in verdict.reasons

    def test_requirement_can_be_waived_for_backtests(self) -> None:
        # Historical runs may lack a second expiry. Waiving is explicit and
        # never the default, so live trading cannot lose the gate by accident.
        verdict = evaluate_regime(
            benchmark_closes=rising(),
            expanding_flags=[False] * 16,
            today=CALM_DAY,
            require_term_structure=False,
        )
        assert verdict.may_open

    def test_curve_block_accumulates_with_the_others(self) -> None:
        verdict = evaluate_regime(
            benchmark_closes=falling(),
            expanding_flags=[True] * 16,
            term_structure=curve(0.30, 0.20),
            today=date(2026, 9, 3),
        )
        assert not verdict.may_open
        for expected in (
            RegimeBlock.BENCHMARK_BELOW_TREND,
            RegimeBlock.VOLATILITY_EXPANDING,
            RegimeBlock.TERM_STRUCTURE_INVERTED,
        ):
            assert expected in verdict.reasons
