"""Typed configuration.

Two jobs, in priority order:

1. Make it structurally impossible for this process to reach the live trading
   API. Paper-only is a hackathon rule and a safety property, so it is asserted
   here rather than assumed at the call sites.
2. Fail closed. Missing credentials, an unreadable risk limit, or an
   unrecognised environment must stop the agent, never default it to something
   permissive.
"""

from __future__ import annotations

import os
from typing import Final, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The only trading host this project is ever allowed to talk to.
PAPER_TRADING_HOST: Final = "https://paper-api.alpaca.markets"
LIVE_TRADING_HOST: Final = "https://api.alpaca.markets"


class LiveTradingBlocked(RuntimeError):
    """Raised when configuration would allow a live-trading code path."""


class RiskLimits(BaseSettings):
    """Risk gates. Defaults are the research hypotheses from the strategy spec.

    These are versioned configuration on purpose: the one-page submission
    write-up has to quote them, and they must be frozen before the judged run
    is used as feedback.
    """

    model_config = SettingsConfigDict(env_prefix="UNDERWRITER_RISK_", extra="forbid")

    # Calibrated for short-premium verticals. A credit spread wins often and
    # small, so the concurrency cap rather than the per-trade cap is what sets
    # expected return. Per-trade risk stays at 0.5%: the safest lever is more
    # positions and wider spreads, not a bigger single bet.
    max_risk_per_trade_pct: float = Field(default=0.5, gt=0, le=2.0)
    max_concurrent_positions: int = Field(default=6, ge=1, le=10)
    max_total_open_risk_pct: float = Field(default=3.0, gt=0, le=10.0)
    daily_loss_stop_pct: float = Field(default=1.5, gt=0, le=10.0)

    # Every short put loses together in a selloff, so a book of individually
    # compliant positions can still be one large directional bet.
    #
    # A put credit spread is net LONG delta: being short the put contributes
    # positive exposure and the protective long put offsets part of it. So what
    # accumulates across the book, and what hurts in a selloff, is net long
    # delta. Expressed in equivalent shares of the underlying per $100k of
    # equity, so the cap scales with account size.
    max_aggregate_net_delta_per_100k: float = Field(default=150.0, gt=0)

    # Contract selection guardrails.
    min_days_to_expiry: int = Field(default=5, ge=2)
    max_days_to_expiry: int = Field(default=14, ge=3)
    max_spread_pct_of_mid: float = Field(default=10.0, gt=0)
    min_open_interest: int = Field(default=100, ge=0)

    # Session timing. No new entries late in the day; everything flat before expiry.
    no_new_entries_after_et: str = Field(default="15:00")
    force_flat_days_before_expiry: int = Field(default=2, ge=1)

    # Data freshness. A stale quote must reject the trade, not price it.
    max_quote_age_seconds: float = Field(default=30.0, gt=0)
    max_news_age_hours: float = Field(default=24.0, gt=0)

    @model_validator(mode="after")
    def _no_unrecognised_risk_variables(self) -> RiskLimits:
        """Reject a `UNDERWRITER_RISK_*` variable that matches no field.

        `extra="forbid"` does not cover environment variables: pydantic-settings
        simply ignores a prefixed variable with no matching field. For risk
        limits that failure mode is backwards -- someone setting
        `UNDERWRITER_RISK_MAX_RISK_PER_TRADE_PCNT=0.1` to *tighten* risk would
        silently keep the more permissive default. Typos here must stop the
        agent, not quietly widen its limits.
        """
        prefix = "UNDERWRITER_RISK_"
        known = {f"{prefix}{name}".upper() for name in type(self).model_fields}
        unknown = sorted(
            name
            for name in os.environ
            if name.upper().startswith(prefix) and name.upper() not in known
        )
        if unknown:
            msg = (
                f"unrecognised risk configuration: {', '.join(unknown)}. "
                "A misspelled risk limit would silently fall back to its default, "
                "so this fails closed. Known limits: "
                f"{', '.join(sorted(known))}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _expiry_window_is_ordered(self) -> RiskLimits:
        if self.min_days_to_expiry > self.max_days_to_expiry:
            msg = (
                f"min_days_to_expiry ({self.min_days_to_expiry}) exceeds "
                f"max_days_to_expiry ({self.max_days_to_expiry})"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _per_trade_risk_fits_total(self) -> RiskLimits:
        implied = self.max_risk_per_trade_pct * self.max_concurrent_positions
        if implied > self.max_total_open_risk_pct:
            msg = (
                f"max_risk_per_trade_pct * max_concurrent_positions = {implied}% "
                f"exceeds max_total_open_risk_pct ({self.max_total_open_risk_pct}%); "
                "the position cap and the aggregate cap contradict each other"
            )
            raise ValueError(msg)
        return self


class Settings(BaseSettings):
    """Process configuration, loaded from the environment and `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Secrets must never be echoed into a log line or the dashboard.
        hide_input_in_errors=True,
    )

    alpaca_api_key: SecretStr = Field(..., alias="ALPACA_API_KEY")
    alpaca_secret_key: SecretStr = Field(..., alias="ALPACA_SECRET_KEY")

    # Present only so it can be asserted false. This project never sets it true.
    alpaca_live_trade: bool = Field(default=False, alias="ALPACA_LIVE_TRADE")

    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    # Which provider screens candidates. "auto" picks whichever key is present,
    # preferring Anthropic when both are, so a stray second key cannot silently
    # change which model is making the call.
    model_provider: Literal["auto", "anthropic", "openai"] = Field(
        default="auto", alias="UNDERWRITER_MODEL_PROVIDER"
    )
    # Overridable because model names move faster than this repo will.
    model_name: str | None = Field(default=None, alias="UNDERWRITER_MODEL")

    env: Literal["dev", "competition"] = Field(default="dev", alias="UNDERWRITER_ENV")
    kill_switch: bool = Field(default=False, alias="UNDERWRITER_KILL_SWITCH")

    risk: RiskLimits = Field(default_factory=RiskLimits)

    @field_validator("alpaca_api_key", "alpaca_secret_key")
    @classmethod
    def _credential_is_non_empty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            msg = "Alpaca credential is present but empty; refusing to start"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _refuse_live_trading(self) -> Settings:
        if self.alpaca_live_trade:
            msg = (
                "ALPACA_LIVE_TRADE is true. This project is paper-only by "
                "hackathon rule and by design. Unset it before starting."
            )
            raise LiveTradingBlocked(msg)
        return self

    @property
    def trading_host(self) -> str:
        """The trading host. There is deliberately no branch here."""
        return PAPER_TRADING_HOST

    @property
    def is_paper(self) -> bool:
        """Always true. Exposed so the dashboard can display the asserted state."""
        return True
