"""The paper-only guarantee and the fail-closed defaults are safety properties,
so they get tests before anything that could place an order exists.

Configuration is driven through real environment variables rather than
constructor kwargs, so these exercise the same load path the agent uses.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rotunda.config import (
    LIVE_TRADING_HOST,
    PAPER_TRADING_HOST,
    LiveTradingBlocked,
    RiskLimits,
    Settings,
)

CREDS = {"ALPACA_API_KEY": "test-key", "ALPACA_SECRET_KEY": "test-secret"}

# Anything that could bleed in from the developer's real shell.
LEAKY_VARS = [
    *CREDS,
    "ALPACA_LIVE_TRADE",
    "ANTHROPIC_API_KEY",
    "ROTUNDA_ENV",
    "ROTUNDA_KILL_SWITCH",
    "ROTUNDA_RISK_MAX_RISK_PER_TRADE_PCT",
    "ROTUNDA_RISK_MAX_CONCURRENT_POSITIONS",
    "ROTUNDA_RISK_MAX_TOTAL_OPEN_RISK_PCT",
    "ROTUNDA_RISK_DAILY_LOSS_STOP_PCT",
    "ROTUNDA_RISK_MAX_QUOTE_AGE_SECONDS",
    "ROTUNDA_RISK_MIN_DAYS_TO_EXPIRY",
    "ROTUNDA_RISK_MAX_DAYS_TO_EXPIRY",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from the ambient environment and from `.env`."""
    for var in LEAKY_VARS:
        monkeypatch.delenv(var, raising=False)
    # Never let a developer's real .env influence a safety test.
    monkeypatch.chdir("/")


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for key, value in {**CREDS, **overrides}.items():
        monkeypatch.setenv(key, value)


class TestPaperOnly:
    def test_trading_host_is_the_paper_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _env(monkeypatch)
        assert Settings().trading_host == PAPER_TRADING_HOST

    def test_stray_env_var_cannot_inject_the_live_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # extra="ignore" drops unknown keys, and `trading_host` is a property
        # with no branch to flip.
        _env(monkeypatch, ALPACA_BASE_URL=LIVE_TRADING_HOST, TRADING_HOST=LIVE_TRADING_HOST)
        settings = Settings()
        assert settings.trading_host == PAPER_TRADING_HOST
        assert settings.is_paper is True

    @pytest.mark.parametrize("truthy", ["true", "True", "1", "yes", "on"])
    def test_live_trade_opt_in_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch, truthy: str
    ) -> None:
        _env(monkeypatch, ALPACA_LIVE_TRADE=truthy)
        with pytest.raises(LiveTradingBlocked, match="paper-only"):
            Settings()

    def test_paper_is_the_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _env(monkeypatch)
        assert Settings().alpaca_live_trade is False

    @pytest.mark.parametrize("falsy", ["false", "False", "0", "no"])
    def test_explicit_false_starts_fine(self, monkeypatch: pytest.MonkeyPatch, falsy: str) -> None:
        _env(monkeypatch, ALPACA_LIVE_TRADE=falsy)
        assert Settings().is_paper is True


class TestFailClosed:
    def test_missing_credentials_refuse_to_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValidationError):
            Settings()

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_credential_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch, blank: str
    ) -> None:
        _env(monkeypatch, ALPACA_SECRET_KEY=blank)
        with pytest.raises(ValidationError):
            Settings()

    def test_secrets_do_not_leak_into_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _env(monkeypatch, ALPACA_SECRET_KEY="hunter2-do-not-print")
        assert "hunter2-do-not-print" not in repr(Settings())

    def test_kill_switch_defaults_off_but_is_settable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(monkeypatch)
        assert Settings().kill_switch is False
        _env(monkeypatch, ROTUNDA_KILL_SWITCH="true")
        assert Settings().kill_switch is True

    def test_unknown_environment_name_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _env(monkeypatch, ROTUNDA_ENV="production")
        with pytest.raises(ValidationError):
            Settings()


class TestRiskLimits:
    def test_research_defaults_are_internally_consistent(self) -> None:
        # Asserted as a relationship rather than as literals, so recalibrating
        # the strategy does not require editing the invariant.
        limits = RiskLimits()
        assert limits.max_risk_per_trade_pct > 0
        assert limits.max_concurrent_positions >= 1
        assert (
            limits.max_risk_per_trade_pct * limits.max_concurrent_positions
            <= limits.max_total_open_risk_pct
        )
        # The daily stop must be reachable before the aggregate cap is, or it
        # can never fire.
        assert limits.daily_loss_stop_pct < limits.max_total_open_risk_pct

    def test_position_cap_contradicting_aggregate_cap_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ROTUNDA_RISK_MAX_RISK_PER_TRADE_PCT", "1.0")
        monkeypatch.setenv("ROTUNDA_RISK_MAX_CONCURRENT_POSITIONS", "5")
        with pytest.raises(ValidationError, match="contradict"):
            RiskLimits()

    def test_inverted_expiry_window_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROTUNDA_RISK_MIN_DAYS_TO_EXPIRY", "14")
        monkeypatch.setenv("ROTUNDA_RISK_MAX_DAYS_TO_EXPIRY", "5")
        with pytest.raises(ValidationError, match="exceeds"):
            RiskLimits()

    @pytest.mark.parametrize(
        ("var", "value"),
        [
            ("ROTUNDA_RISK_MAX_RISK_PER_TRADE_PCT", "0"),
            ("ROTUNDA_RISK_MAX_RISK_PER_TRADE_PCT", "-1"),
            ("ROTUNDA_RISK_MAX_CONCURRENT_POSITIONS", "0"),
            ("ROTUNDA_RISK_DAILY_LOSS_STOP_PCT", "0"),
            ("ROTUNDA_RISK_MAX_QUOTE_AGE_SECONDS", "0"),
        ],
    )
    def test_nonsensical_limits_are_rejected(
        self, monkeypatch: pytest.MonkeyPatch, var: str, value: str
    ) -> None:
        monkeypatch.setenv(var, value)
        with pytest.raises(ValidationError):
            RiskLimits()

    def test_typo_in_a_risk_limit_is_rejected_rather_than_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A misspelled limit must not silently fall back to the permissive default.
        monkeypatch.setenv("ROTUNDA_RISK_MAX_RISK_PER_TRADE_PCNT", "0.1")
        with pytest.raises(ValidationError, match="unrecognised risk configuration"):
            RiskLimits()
