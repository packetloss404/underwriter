"""Risk engine tests.

Almost entirely denial paths. The engine's job is to say no correctly, and a
risk system that fails open is worse than none because it looks like one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import time

import pytest

from rotunda.config import RiskLimits
from rotunda.risk import (
    AccountState,
    Decision,
    Denial,
    OpenPosition,
    evaluate,
    max_risk_dollars,
    size_position,
)

MIDDAY = time(11, 0)
EQUITY = 100_000.0


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits()


def account(
    *,
    equity: float = EQUITY,
    options_buying_power: float = EQUITY,
    starting_equity: float = EQUITY,
    realised_pnl_today: float = 0.0,
    open_positions: Sequence[OpenPosition] = (),
) -> AccountState:
    return AccountState(
        equity=equity,
        options_buying_power=options_buying_power,
        starting_equity=starting_equity,
        realised_pnl_today=realised_pnl_today,
        open_positions=open_positions,
    )


def decide(
    limits: RiskLimits,
    *,
    symbol: str = "XLE",
    max_loss_per_contract: float = 250.0,
    account_state: AccountState | None = None,
    now_et: time = MIDDAY,
    kill_switch: bool = False,
) -> Decision:
    return evaluate(
        symbol=symbol,
        max_loss_per_contract=max_loss_per_contract,
        account=account_state if account_state is not None else account(),
        limits=limits,
        now_et=now_et,
        kill_switch=kill_switch,
    )


class TestSizing:
    def test_budget_is_the_configured_percentage_of_equity(self, limits: RiskLimits) -> None:
        # 0.5% of 100k = 500.
        assert max_risk_dollars(EQUITY, limits) == pytest.approx(500.0)

    def test_sizing_floors_rather_than_rounds(self, limits: RiskLimits) -> None:
        # 500 budget / 300 per contract = 1.67 -> 1, never 2.
        assert size_position(equity=EQUITY, max_loss_per_contract=300.0, limits=limits) == 1

    def test_exact_division_is_allowed(self, limits: RiskLimits) -> None:
        assert size_position(equity=EQUITY, max_loss_per_contract=250.0, limits=limits) == 2

    def test_contract_larger_than_budget_sizes_to_zero(self, limits: RiskLimits) -> None:
        assert size_position(equity=EQUITY, max_loss_per_contract=501.0, limits=limits) == 0

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_nonpositive_inputs_size_to_zero(self, limits: RiskLimits, bad: float) -> None:
        assert size_position(equity=bad, max_loss_per_contract=250.0, limits=limits) == 0
        assert size_position(equity=EQUITY, max_loss_per_contract=bad, limits=limits) == 0


class TestHappyPath:
    def test_clean_trade_is_allowed_and_sized(self, limits: RiskLimits) -> None:
        d = decide(limits)
        assert d.allowed
        assert d.contracts == 2
        assert d.denials == ()


class TestFailClosed:
    @pytest.mark.parametrize("equity", [0.0, -100.0, float("nan"), float("inf")])
    def test_unusable_equity_denies(self, limits: RiskLimits, equity: float) -> None:
        d = decide(limits, account_state=account(equity=equity))
        assert not d.allowed
        assert Denial.UNREADABLE_EQUITY in d.denials

    @pytest.mark.parametrize("risk", [0.0, -50.0, float("nan"), float("inf")])
    def test_nonpositive_or_nonfinite_risk_denies(self, limits: RiskLimits, risk: float) -> None:
        d = decide(limits, max_loss_per_contract=risk)
        assert not d.allowed
        assert Denial.NONPOSITIVE_RISK in d.denials

    def test_symbol_outside_universe_denies(self, limits: RiskLimits) -> None:
        d = decide(limits, symbol="GME")
        assert not d.allowed
        assert Denial.UNKNOWN_SYMBOL in d.denials

    def test_contract_too_large_for_budget_denies(self, limits: RiskLimits) -> None:
        d = decide(limits, max_loss_per_contract=750.0)
        assert not d.allowed
        assert Denial.SIZE_ROUNDS_TO_ZERO in d.denials
        # It must not silently submit a zero-quantity order.
        assert d.contracts == 0


class TestKillSwitch:
    def test_engaged_kill_switch_denies_an_otherwise_clean_trade(self, limits: RiskLimits) -> None:
        d = decide(limits, kill_switch=True)
        assert not d.allowed
        assert Denial.KILL_SWITCH in d.denials


class TestConcurrencyAndConcentration:
    def test_position_cap_denies(self, limits: RiskLimits) -> None:
        # Fill exactly to the configured cap with mutually uncorrelated names,
        # so the only gate that can fire is the position cap itself.
        uncorrelated = ("XLF", "XLV", "XLY", "XLP", "XLU", "XLB", "GLD", "ITA")
        held = [OpenPosition(s, 50.0) for s in uncorrelated[: limits.max_concurrent_positions]]
        assert len(held) == limits.max_concurrent_positions
        d = decide(limits, symbol="XLE", account_state=account(open_positions=held))
        assert not d.allowed
        assert Denial.POSITION_CAP in d.denials

    def test_one_below_the_cap_is_allowed(self, limits: RiskLimits) -> None:
        uncorrelated = ("XLF", "XLV", "XLY", "XLP", "XLU", "XLB", "GLD", "ITA")
        held = [OpenPosition(s, 50.0) for s in uncorrelated[: limits.max_concurrent_positions - 1]]
        assert decide(limits, symbol="XLE", account_state=account(open_positions=held)).allowed

    def test_duplicate_symbol_denies(self, limits: RiskLimits) -> None:
        d = decide(
            limits, symbol="XLE", account_state=account(open_positions=[OpenPosition("XLE", 200.0)])
        )
        assert not d.allowed
        assert Denial.DUPLICATE_SYMBOL in d.denials

    def test_correlated_exposure_denies(self, limits: RiskLimits) -> None:
        # QQQ and XLK overlap heavily; holding both is one bet.
        d = decide(
            limits, symbol="QQQ", account_state=account(open_positions=[OpenPosition("XLK", 200.0)])
        )
        assert not d.allowed
        assert Denial.CORRELATED_EXPOSURE in d.denials

    def test_uncorrelated_exposure_is_allowed(self, limits: RiskLimits) -> None:
        d = decide(
            limits, symbol="XLE", account_state=account(open_positions=[OpenPosition("XLV", 200.0)])
        )
        assert d.allowed

    def test_aggregate_risk_cap_denies(self, limits: RiskLimits) -> None:
        # Fill the aggregate cap to just under the per-trade budget, using
        # fewer names than the position cap so that gate cannot fire instead.
        cap = EQUITY * (limits.max_total_open_risk_pct / 100)
        per_trade = EQUITY * (limits.max_risk_per_trade_pct / 100)
        headroom = per_trade / 2
        held = [
            OpenPosition("XLF", (cap - headroom) / 2),
            OpenPosition("XLV", (cap - headroom) / 2),
        ]
        d = decide(limits, symbol="XLE", account_state=account(open_positions=held))
        assert not d.allowed
        assert Denial.AGGREGATE_RISK_CAP in d.denials

    def test_insufficient_options_buying_power_denies(self, limits: RiskLimits) -> None:
        d = decide(limits, account_state=account(options_buying_power=100.0))
        assert not d.allowed
        assert Denial.INSUFFICIENT_BUYING_POWER in d.denials


class TestDailyLossStop:
    def test_realised_loss_beyond_the_stop_denies(self, limits: RiskLimits) -> None:
        # 1.5% of 100k = 1500.
        d = decide(limits, account_state=account(realised_pnl_today=-1600.0))
        assert not d.allowed
        assert Denial.DAILY_LOSS_STOP in d.denials

    def test_stop_triggers_exactly_at_the_threshold(self, limits: RiskLimits) -> None:
        d = decide(limits, account_state=account(realised_pnl_today=-1500.0))
        assert not d.allowed
        assert Denial.DAILY_LOSS_STOP in d.denials

    def test_loss_inside_the_stop_is_allowed(self, limits: RiskLimits) -> None:
        assert decide(limits, account_state=account(realised_pnl_today=-1400.0)).allowed

    def test_unrealised_losses_count_against_the_stop(self, limits: RiskLimits) -> None:
        held = [OpenPosition("XLV", 200.0, unrealised_pnl=-1600.0)]
        d = decide(limits, account_state=account(open_positions=held))
        assert not d.allowed
        assert Denial.DAILY_LOSS_STOP in d.denials

    def test_unrealised_gains_do_not_unlock_a_breached_stop(self, limits: RiskLimits) -> None:
        # A stop that a paper profit can cancel is not a stop.
        held = [OpenPosition("XLV", 200.0, unrealised_pnl=+5000.0)]
        d = decide(limits, account_state=account(realised_pnl_today=-1600.0, open_positions=held))
        assert not d.allowed
        assert Denial.DAILY_LOSS_STOP in d.denials

    def test_stop_measures_against_session_open_not_current_equity(
        self, limits: RiskLimits
    ) -> None:
        # Equity has already fallen; the stop must still reference the open,
        # otherwise it drifts down with losses and never triggers.
        d = decide(
            limits,
            account_state=account(
                equity=98_000.0, starting_equity=100_000.0, realised_pnl_today=-1550.0
            ),
        )
        assert not d.allowed
        assert Denial.DAILY_LOSS_STOP in d.denials


class TestSessionTiming:
    def test_after_cutoff_denies(self, limits: RiskLimits) -> None:
        d = decide(limits, now_et=time(15, 30))
        assert not d.allowed
        assert Denial.TOO_LATE_IN_SESSION in d.denials

    def test_exactly_at_cutoff_denies(self, limits: RiskLimits) -> None:
        assert not decide(limits, now_et=time(15, 0)).allowed

    def test_before_cutoff_allows(self, limits: RiskLimits) -> None:
        assert decide(limits, now_et=time(14, 59)).allowed


class TestExplainability:
    def test_all_applicable_denials_are_reported_not_just_the_first(
        self, limits: RiskLimits
    ) -> None:
        # Late in the session, past the loss stop, and already holding a
        # correlated position. The log should show all three.
        held = [OpenPosition("XLK", 200.0)]
        d = decide(
            limits,
            symbol="QQQ",
            now_et=time(15, 30),
            account_state=account(realised_pnl_today=-2000.0, open_positions=held),
        )
        assert not d.allowed
        assert Denial.TOO_LATE_IN_SESSION in d.denials
        assert Denial.DAILY_LOSS_STOP in d.denials
        assert Denial.CORRELATED_EXPOSURE in d.denials

    def test_every_denial_carries_human_readable_detail(self, limits: RiskLimits) -> None:
        d = decide(limits, symbol="GME", now_et=time(16, 0))
        assert not d.allowed
        assert len(d.detail) >= 2
        assert all(isinstance(x, str) and x for x in d.detail)
