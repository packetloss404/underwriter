"""Regime filter tests.

The filter is a global no-entry gate, so it has two ways to be badly wrong and
both are tested: failing open (permitting entries in a hostile tape) and
failing shut (blocking every entry for the whole window, which looks identical
to working correctly while the agent never trades).
"""

from __future__ import annotations

from datetime import date

import pytest

from rotunda.regime import (
    KNOWN_EVENTS,
    RegimeBlock,
    RegimePolicy,
    ScheduledEvent,
    check_drawdown,
    check_scheduled_events,
    check_trend,
    check_volatility_expansion,
    evaluate_regime,
)

# A quiet Monday with no event inside the holding horizon.
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


class TestScheduledEvents:
    EVENT = ScheduledEvent(date(2026, 9, 4), "Non-farm payrolls, 08:30 ET")

    def test_event_within_holding_horizon_blocks(self) -> None:
        block = check_scheduled_events(date(2026, 9, 3), RegimePolicy(), [self.EVENT])
        assert block is not None
        assert block.reason is RegimeBlock.SCHEDULED_EVENT

    def test_event_beyond_holding_horizon_permits(self) -> None:
        assert check_scheduled_events(date(2026, 8, 31), RegimePolicy(), [self.EVENT]) is None

    def test_event_on_the_day_blocks(self) -> None:
        assert check_scheduled_events(date(2026, 9, 4), RegimePolicy(), [self.EVENT]) is not None

    def test_past_event_does_not_block(self) -> None:
        assert check_scheduled_events(date(2026, 9, 8), RegimePolicy(), [self.EVENT]) is None

    def test_horizon_is_a_holding_period_not_time_to_expiry(self) -> None:
        # Regression. The horizon was originally set to the 5-day minimum DTE,
        # which blocked every entry for the whole judged window over a single
        # event -- the agent stood down all week while logging a plausible
        # reason. The horizon is how long we HOLD, not how long the contract
        # lives.
        assert RegimePolicy().event_lookahead_days <= 2

    def test_no_events_permits(self) -> None:
        assert check_scheduled_events(CALM_DAY, RegimePolicy(), []) is None


class TestEvaluateRegime:
    def test_calm_market_permits_entry(self) -> None:
        verdict = evaluate_regime(
            benchmark_closes=rising(), expanding_flags=[False] * 16, today=CALM_DAY
        )
        assert verdict.may_open
        assert verdict.blocks == ()

    def test_the_judged_window_has_tradeable_sessions(self) -> None:
        # The bug this guards against blocked all five sessions. In a calm
        # tape the agent must be able to open on most of the week, with
        # Thursday and Friday reserved by the payrolls rule.
        sessions = [date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2)]
        for day in sessions:
            verdict = evaluate_regime(
                benchmark_closes=rising(), expanding_flags=[False] * 16, today=day
            )
            assert verdict.may_open, f"{day} should permit entry in a calm tape"

    def test_payrolls_blocks_the_final_two_sessions(self) -> None:
        for day in (date(2026, 9, 3), date(2026, 9, 4)):
            verdict = evaluate_regime(
                benchmark_closes=rising(), expanding_flags=[False] * 16, today=day
            )
            assert not verdict.may_open
            assert RegimeBlock.SCHEDULED_EVENT in verdict.reasons

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

    def test_known_events_includes_payrolls(self) -> None:
        assert any(e.on == date(2026, 9, 4) for e in KNOWN_EVENTS)

    @pytest.mark.parametrize("day", [date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2)])
    def test_hostile_tape_blocks_even_on_permitted_days(self, day: date) -> None:
        verdict = evaluate_regime(
            benchmark_closes=falling(), expanding_flags=[False] * 16, today=day
        )
        assert not verdict.may_open
