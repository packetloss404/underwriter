"""Preflight decides whether the agent may risk money, so the tests here are
mostly about the *unhappy* paths: every way a check can fail to see its data
must produce FAIL, never a silent pass."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest

from rotunda.config import Settings
from rotunda.preflight import (
    Check,
    PreflightReport,
    Status,
    check_account,
    check_cli,
    check_kill_switch,
    check_options_level,
    run_preflight,
)


@dataclass(frozen=True)
class FakeAccount:
    status: str = "ACTIVE"
    trading_blocked: bool = False
    account_blocked: bool = False
    equity: str = "100000"
    options_trading_level: str = "3"
    options_approved_level: str = "3"
    options_buying_power: str = "100000"


@dataclass(frozen=True)
class FakeClock:
    is_open: bool = False
    next_open: datetime = datetime(2026, 8, 31, 13, 30, tzinfo=UTC)
    next_close: datetime = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for var in ("ALPACA_LIVE_TRADE", "ROTUNDA_KILL_SWITCH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    monkeypatch.chdir("/")
    return Settings()


def _named(checks: list[Check], name: str) -> Check:
    return next(c for c in checks if c.name == name)


class TestReportGating:
    def test_healthy_account_may_trade(self, settings: Settings) -> None:
        report = run_preflight(settings, FakeAccount(), FakeClock())
        assert report.may_trade
        assert report.failures == []

    def test_empty_report_fails_closed(self) -> None:
        # A report with no checks means nothing was verified. It must not read
        # as permission to trade.
        assert PreflightReport(checks=[]).may_trade is False

    def test_a_single_failure_blocks_trading(self, settings: Settings) -> None:
        report = run_preflight(settings, FakeAccount(status="ONBOARDING"), FakeClock())
        assert report.may_trade is False
        assert _named(list(report.checks), "account.status").status is Status.FAIL

    def test_warnings_alone_do_not_block(self, settings: Settings) -> None:
        # Approved 3 but effective 3 with a cap warning present elsewhere:
        # construct a warn-only condition and confirm it still trades.
        report = PreflightReport(
            checks=[Check(name="x", status=Status.WARN, detail="", observed=None)]
        )
        assert report.may_trade is True
        assert len(report.warnings) == 1


class TestKillSwitch:
    def test_engaged_kill_switch_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALPACA_API_KEY", "k")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
        monkeypatch.setenv("ROTUNDA_KILL_SWITCH", "true")
        monkeypatch.chdir("/")
        assert check_kill_switch(Settings())[0].status is Status.FAIL

    def test_disengaged_kill_switch_passes(self, settings: Settings) -> None:
        assert check_kill_switch(settings)[0].status is Status.OK


class TestAccount:
    @pytest.mark.parametrize("status", ["ONBOARDING", "SUBMITTED", "REJECTED", "INACTIVE", ""])
    def test_non_active_status_fails(self, status: str) -> None:
        checks = check_account(FakeAccount(status=status))
        assert _named(checks, "account.status").status is Status.FAIL

    def test_status_check_is_case_insensitive(self) -> None:
        checks = check_account(FakeAccount(status="active"))
        assert _named(checks, "account.status").status is Status.OK

    @pytest.mark.parametrize(
        ("trading_blocked", "account_blocked"),
        [(True, False), (False, True), (True, True)],
    )
    def test_any_block_fails(self, trading_blocked: bool, account_blocked: bool) -> None:
        account = FakeAccount(trading_blocked=trading_blocked, account_blocked=account_blocked)
        assert _named(check_account(account), "account.not_blocked").status is Status.FAIL

    @pytest.mark.parametrize("equity", ["0", "-1", "", "not-a-number", "None"])
    def test_unreadable_or_nonpositive_equity_fails(self, equity: str) -> None:
        assert _named(check_account(FakeAccount(equity=equity)), "account.equity").status is (
            Status.FAIL
        )


class TestOptionsLevel:
    @pytest.mark.parametrize("level", ["0", "1", "2"])
    def test_level_below_three_fails(self, level: str) -> None:
        account = FakeAccount(options_trading_level=level, options_approved_level=level)
        assert _named(check_options_level(account), "options.effective_level").status is (
            Status.FAIL
        )

    def test_level_three_passes(self) -> None:
        checks = check_options_level(FakeAccount())
        assert _named(checks, "options.effective_level").status is Status.OK

    def test_level_above_three_passes(self) -> None:
        account = FakeAccount(options_trading_level="4", options_approved_level="4")
        assert _named(check_options_level(account), "options.effective_level").status is Status.OK

    @pytest.mark.parametrize("level", ["", "unknown", "None", "three"])
    def test_unreadable_level_fails_closed(self, level: str) -> None:
        # The dangerous case: we cannot see the permission, so we must not
        # assume we have it.
        checks = check_options_level(FakeAccount(options_trading_level=level))
        assert _named(checks, "options.effective_level").status is Status.FAIL
        assert "Refusing to trade" in _named(checks, "options.effective_level").detail

    def test_gates_on_effective_not_approved_level(self) -> None:
        # Approved 3 but capped to 2 by account configuration. Gating on the
        # approved level here would build a spread the API then rejects.
        account = FakeAccount(options_approved_level="3", options_trading_level="2")
        checks = check_options_level(account)
        assert _named(checks, "options.effective_level").status is Status.FAIL

    def test_cap_below_approved_raises_an_explanatory_warning(self) -> None:
        account = FakeAccount(options_approved_level="3", options_trading_level="2")
        warning = _named(check_options_level(account), "options.level_capped")
        assert warning.status is Status.WARN
        assert "max_options_trading_level" in warning.detail

    def test_no_cap_warning_when_levels_agree(self) -> None:
        names = [c.name for c in check_options_level(FakeAccount())]
        assert "options.level_capped" not in names

    @pytest.mark.parametrize("obp", ["0", "-5", "", "n/a"])
    def test_unreadable_or_zero_options_buying_power_fails(self, obp: str) -> None:
        account = FakeAccount(options_buying_power=obp)
        assert _named(check_options_level(account), "options.buying_power").status is Status.FAIL


class TestCli:
    def test_missing_binary_fails(self) -> None:
        check = check_cli("definitely-not-a-real-binary-name-xyz")[0]
        assert check.status is Status.FAIL
        assert "not found on PATH" in check.detail

    def test_present_binary_passes(self) -> None:
        # `true` exits 0 and is present on any POSIX system.
        assert check_cli("true")[0].status is Status.OK

    def test_nonzero_exit_fails(self) -> None:
        assert check_cli("false")[0].status is Status.FAIL


class TestClockIsNonBlocking:
    def test_closed_market_does_not_block_trading(self, settings: Settings) -> None:
        # Preflight is meant to run before the open. A closed market is normal.
        report = run_preflight(settings, FakeAccount(), replace(FakeClock(), is_open=False))
        assert report.may_trade is True
