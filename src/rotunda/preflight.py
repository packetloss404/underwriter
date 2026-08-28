"""Preflight: everything that must be true before the agent may trade.

Design rule: this module *reports*, it does not *decide quietly*. Every check
records what it looked at and what it found, so the dashboard and the audit log
can show a judge exactly why the agent did or did not trade. A check that
cannot reach its data returns FAIL, never PASS -- absence of evidence is not
evidence of safety.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from rotunda.config import PAPER_TRADING_HOST, Settings

# Spreads are a level 3 activity. Anything less cannot open a vertical.
REQUIRED_OPTIONS_LEVEL = 3


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Check:
    """One preflight result.

    `blocking` distinguishes "the agent must not trade" from "the operator
    should know". A WARN never blocks; a FAIL always does.
    """

    name: str
    status: Status
    detail: str
    observed: str | None = None

    @property
    def blocking(self) -> bool:
        return self.status is Status.FAIL


@dataclass(frozen=True, slots=True)
class PreflightReport:
    checks: Sequence[Check]
    run_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.WARN]

    @property
    def may_trade(self) -> bool:
        """The single question the agent asks. Fails closed on an empty report."""
        return bool(self.checks) and not self.failures


class AccountLike(Protocol):
    """The subset of the Alpaca account object preflight depends on.

    A Protocol rather than the SDK type so these checks are testable without
    credentials or a network. Members are declared read-only, because preflight
    observes account state and must never mutate it -- and because that lets
    frozen test doubles satisfy the contract.
    """

    @property
    def status(self) -> object: ...
    @property
    def trading_blocked(self) -> bool: ...
    @property
    def account_blocked(self) -> bool: ...
    @property
    def equity(self) -> object: ...
    @property
    def options_trading_level(self) -> object: ...
    @property
    def options_approved_level(self) -> object: ...
    @property
    def options_buying_power(self) -> object: ...


class ClockLike(Protocol):
    @property
    def is_open(self) -> bool: ...
    @property
    def next_open(self) -> datetime: ...
    @property
    def next_close(self) -> datetime: ...


def check_paper_only(settings: Settings) -> list[Check]:
    """Restate the paper-only guarantee as an explicit, displayable check.

    `Settings` already refuses to construct if live trading were selected, so
    this cannot fail in practice. It exists so the guarantee is *visible* in
    the report a judge reads, rather than being an invisible invariant.
    """
    return [
        Check(
            name="paper_only.host",
            status=Status.OK if settings.trading_host == PAPER_TRADING_HOST else Status.FAIL,
            detail="Trading host is the paper endpoint and has no branch that reaches live.",
            observed=settings.trading_host,
        ),
        Check(
            name="paper_only.live_trade_flag",
            status=Status.OK if not settings.alpaca_live_trade else Status.FAIL,
            detail="ALPACA_LIVE_TRADE must be unset or false.",
            observed=str(settings.alpaca_live_trade),
        ),
    ]


def _as_int(value: object) -> int | None:
    """Alpaca returns numeric fields as strings in places. Parse defensively."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def check_account(account: AccountLike) -> list[Check]:
    checks: list[Check] = []

    status_text = str(getattr(account.status, "value", account.status))
    checks.append(
        Check(
            name="account.status",
            status=Status.OK if status_text.upper() == "ACTIVE" else Status.FAIL,
            detail="Account must be ACTIVE.",
            observed=status_text,
        )
    )

    blocked = bool(account.trading_blocked) or bool(account.account_blocked)
    checks.append(
        Check(
            name="account.not_blocked",
            status=Status.FAIL if blocked else Status.OK,
            detail="Account and trading must both be unblocked.",
            observed=f"trading_blocked={account.trading_blocked} "
            f"account_blocked={account.account_blocked}",
        )
    )

    equity = _as_float(account.equity)
    checks.append(
        Check(
            name="account.equity",
            status=Status.OK if equity is not None and equity > 0 else Status.FAIL,
            detail="Equity must be readable and positive.",
            observed=str(account.equity),
        )
    )

    return checks


def check_options_level(account: AccountLike) -> list[Check]:
    """Gate on the *effective* level.

    `options_trading_level` is the minimum of the approved level and the
    account configuration's `max_options_trading_level`. An account can show
    `options_approved_level: 3` while the effective level is lower because the
    configuration caps it, so gating on the approved level would let the agent
    build a spread the API will then reject.
    """
    effective = _as_int(account.options_trading_level)
    approved = _as_int(account.options_approved_level)

    if effective is None:
        return [
            Check(
                name="options.effective_level",
                status=Status.FAIL,
                detail=(
                    "Could not read options_trading_level. Refusing to trade rather "
                    "than assuming a permission we cannot see."
                ),
                observed=str(account.options_trading_level),
            )
        ]

    checks = [
        Check(
            name="options.effective_level",
            status=Status.OK if effective >= REQUIRED_OPTIONS_LEVEL else Status.FAIL,
            detail=f"Effective options level must be >= {REQUIRED_OPTIONS_LEVEL} for spreads.",
            observed=f"options_trading_level={effective}",
        )
    ]

    # Surface the approved/effective gap, which is otherwise a confusing
    # failure: the operator sees "approved 3" and cannot explain the rejection.
    if approved is not None and approved > effective:
        checks.append(
            Check(
                name="options.level_capped",
                status=Status.WARN,
                detail=(
                    "Effective level is below the approved level, so account "
                    "configuration max_options_trading_level is capping it. "
                    "Raise the cap via PATCH /v2/account/configurations."
                ),
                observed=f"approved={approved} effective={effective}",
            )
        )

    obp = _as_float(account.options_buying_power)
    checks.append(
        Check(
            name="options.buying_power",
            status=Status.OK if obp is not None and obp > 0 else Status.FAIL,
            detail="Options buying power must be readable and positive.",
            observed=str(account.options_buying_power),
        )
    )

    return checks


def check_clock(clock: ClockLike) -> list[Check]:
    """The clock is informational, not blocking.

    A closed market is a normal state -- the agent waits rather than fails.
    Preflight runs before the open on purpose.
    """
    return [
        Check(
            name="market.clock",
            status=Status.OK,
            detail="Market clock is reachable.",
            observed=(
                f"is_open={clock.is_open} next_open={clock.next_open.isoformat()} "
                f"next_close={clock.next_close.isoformat()}"
            ),
        )
    ]


def check_cli(binary: str = "alpaca") -> list[Check]:
    """The Alpaca CLI sits on the order path, so its absence is blocking.

    The hackathon also requires that the MCP server or CLI be used, so this
    doubles as evidence for the submission.
    """
    path = shutil.which(binary)
    if path is None:
        return [
            Check(
                name="cli.available",
                status=Status.FAIL,
                detail=f"Alpaca CLI ({binary}) not found on PATH; it is on the order path.",
                observed=None,
            )
        ]

    try:
        result = subprocess.run(  # noqa: S603 - fixed binary resolved via which
            [path, "version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [
            Check(
                name="cli.available",
                status=Status.FAIL,
                detail=f"Alpaca CLI found at {path} but could not be executed: {exc}",
                observed=path,
            )
        ]

    if result.returncode != 0:
        return [
            Check(
                name="cli.available",
                status=Status.FAIL,
                detail=f"`{binary} version` exited {result.returncode}.",
                observed=result.stderr.strip()[:200] or None,
            )
        ]

    return [
        Check(
            name="cli.available",
            status=Status.OK,
            detail="Alpaca CLI is present and executable.",
            observed=f"{path} -> {result.stdout.strip()}",
        )
    ]


def check_kill_switch(settings: Settings) -> list[Check]:
    """An engaged kill switch blocks trading. That is the point of it."""
    return [
        Check(
            name="risk.kill_switch",
            status=Status.FAIL if settings.kill_switch else Status.OK,
            detail="Kill switch must be disengaged for the agent to open positions.",
            observed=f"engaged={settings.kill_switch}",
        )
    ]


def run_preflight(
    settings: Settings,
    account: AccountLike,
    clock: ClockLike,
    *,
    cli_binary: str = "alpaca",
) -> PreflightReport:
    """Assemble the full report. Callers gate on `report.may_trade`."""
    checks: list[Check] = [
        *check_paper_only(settings),
        *check_kill_switch(settings),
        *check_account(account),
        *check_options_level(account),
        *check_clock(clock),
        *check_cli(cli_binary),
    ]
    return PreflightReport(checks=checks)
