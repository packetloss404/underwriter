"""Wiring the agent to a live broker.

Every other module takes its dependencies by injection so it can be tested
without a network. This is the one place that constructs the real ones, which
makes it the only place a credential is read and the only place a mistake
reaches the market.

Two things it is responsible for beyond assembly:

**Refusing to start rather than starting wrong.** A missing credential, an
account that cannot trade options, a live-trading flag -- all of these stop
construction here, where the failure is loud and nothing has been placed. The
alternative is an agent that boots, appears healthy, and discovers at 09:31
that it cannot submit.

**Never claiming to trade when it is not.** The supervisor previously idled
with a lambda that did nothing, which is the failure this codebase exists to
avoid: a process that looks like it is working. If the agent cannot be built,
`build_agent` raises and the caller decides -- it does not hand back a
do-nothing stub.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from underwriter.account import AlpacaBroker, paper_broker
from underwriter.chain import ExpiryWindow
from underwriter.config import Settings
from underwriter.cycle import BrokerOrderView, Cycle, CycleReport
from underwriter.data import Bars, MarketData, SnapshotLike
from underwriter.execution import Backend, ExecutionAdapter, Kind, build_adapter
from underwriter.journal import Journal
from underwriter.preflight import REQUIRED_OPTIONS_LEVEL, run_preflight
from underwriter.veto import build_veto

log = logging.getLogger(__name__)


class NotReadyToTrade(RuntimeError):
    """The agent cannot be constructed safely. Raised before anything is placed."""


@dataclass(frozen=True, slots=True)
class LiveMarket:
    """Adapts `MarketData` to the cycle's `MarketSource` protocol.

    The cycle asks for closes without saying how far back; the lookback is a
    deployment concern rather than a strategy one, so it is bound here.
    """

    data: MarketData
    lookback_days: int = 120

    def daily_closes(self, symbols: Sequence[str]) -> Bars:
        return self.data.daily_closes(symbols, lookback_days=self.lookback_days)

    def chain(self, underlying: str, window: ExpiryWindow) -> Mapping[str, SnapshotLike]:
        return self.data.chain(underlying, window)

    def option_snapshots(self, symbols: Sequence[str]) -> Mapping[str, SnapshotLike]:
        return self.data.option_snapshots(symbols)


@dataclass(frozen=True, slots=True)
class LiveOrderReader:
    """Asks the broker the state of one order, by our own client order id.

    Reconciliation goes through the CLI where it is available, deliberately:
    the confirmation then arrives over a different transport than the one that
    submitted, so a transport-specific failure cannot both lose the order and
    lose the evidence of it.
    """

    adapter: ExecutionAdapter

    def order_status(self, client_order_id: str) -> BrokerOrderView | None:
        backend = self.adapter.reconciler or self.adapter.primary
        outcome = backend.lookup(client_order_id)
        if outcome.kind is not Kind.ACCEPTED or outcome.order is None:
            # Absent, or unreadable. Both mean "no confirmed state", and the
            # cycle must not treat the second as the first.
            return None
        found = outcome.order
        # Execution carries prices as Decimal, the cycle's view as float. The
        # conversion is explicit rather than implicit so the narrowing is
        # visible: it is lossless at cent precision, which is the only
        # precision the broker quotes, and None survives as None because a
        # zero here would read as a filled order at no price.
        return BrokerOrderView(
            status=str(found.status),
            order_id=found.id,
            filled_qty=None if found.filled_qty is None else float(found.filled_qty),
            filled_avg_price=(
                None if found.filled_avg_price is None else float(found.filled_avg_price)
            ),
            detail=f"via {backend.name.value}",
        )


@dataclass(frozen=True, slots=True)
class Agent:
    """A constructed, ready-to-run agent. Calling it runs one cycle."""

    cycle: Cycle
    journal: Journal
    settings: Settings
    broker: AlpacaBroker

    def __call__(self) -> CycleReport:
        """Run one cycle, preflighting first.

        Preflight runs EVERY cycle rather than once at boot. Account state is
        not static: options approval can be revoked, an account can be blocked
        mid-session, and buying power moves with every fill. Checking once at
        start-up would mean trading for hours on a permission we verified at
        09:10 and no longer hold.

        If the account cannot be read at all, no report is passed and the cycle
        halts on the missing preflight -- which is the correct outcome, because
        the alternative is opening positions against an account whose state is
        unknown.
        """
        report = None
        try:
            report = run_preflight(self.settings, self.broker.account(), self.broker.clock())
        except Exception:
            log.exception("preflight could not read the account; entries will be barred")
        return self.cycle.run(preflight=report)

    def close(self) -> None:
        self.journal.close()


def _assert_can_trade(broker: AlpacaBroker, settings: Settings) -> None:
    """Fail loudly now rather than quietly at the open."""
    account = broker.account()
    report = run_preflight(settings, account, _StubClock())
    blocking = [c for c in report.failures if not c.name.startswith("market.")]
    if blocking:
        lines = "; ".join(f"{c.name}: {c.detail}" for c in blocking)
        msg = f"preflight refuses to start the agent -- {lines}"
        raise NotReadyToTrade(msg)
    log.info(
        "preflight clear: equity=%s options_level=%s (need %s)",
        account.equity,
        account.options_trading_level,
        REQUIRED_OPTIONS_LEVEL,
    )


@dataclass(frozen=True, slots=True)
class _StubClock:
    """A stand-in market clock for the startup check.

    Preflight's clock check is informational and never blocking -- a closed
    market is the normal state at boot -- so construction does not need a real
    one, and asking for it would add a network call to a path that must fail
    fast.
    """

    is_open: bool = False
    next_open: datetime = datetime(1970, 1, 1, tzinfo=UTC)
    next_close: datetime = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _CallableClock:
    """Adapts a plain callable to the cycle's Clock protocol."""

    fn: Callable[[], datetime]

    def now(self) -> datetime:
        return self.fn()


def build_agent(
    settings: Settings,
    journal_path: str,
    *,
    dry_run: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> Agent:
    """Construct the live agent, or raise.

    Raises `NotReadyToTrade` rather than returning something that cannot trade.
    A caller holding an Agent may assume the account is reachable, active, and
    approved for spreads.
    """
    key = settings.alpaca_api_key.get_secret_value()
    secret = settings.alpaca_secret_key.get_secret_value()

    broker = paper_broker(key, secret)
    _assert_can_trade(broker, settings)

    from alpaca.trading.client import TradingClient

    sdk = TradingClient(api_key=key, secret_key=secret, paper=True)
    adapter = build_adapter(sdk_client=sdk)
    if adapter.primary.name is not Backend.SDK:
        # The SDK carries the POST because its retry loop is ours and is
        # verified off. Anything else here is a wiring mistake.
        msg = f"expected the SDK on the order path, got {adapter.primary.name}"
        raise NotReadyToTrade(msg)

    # The veto is optional by configuration but never optional in effect: if a
    # key is present it screens every candidate, and if one is absent the agent
    # runs without it rather than pretending to screen. Wiring a veto that
    # cannot reach a model would be worse than none, because the cycle treats a
    # raised exception as a veto and the agent would silently stop trading.
    veto = None
    anthropic_key = settings.anthropic_api_key
    if anthropic_key is not None and anthropic_key.get_secret_value().strip():
        veto = build_veto(anthropic_key.get_secret_value(), key, secret)
        log.info("catalyst veto wired")
    else:
        log.warning(
            "no ANTHROPIC_API_KEY: running without the catalyst veto. Candidates "
            "will not be screened for scheduled events."
        )

    journal = Journal(journal_path)
    cycle = Cycle(
        journal=journal,
        market=LiveMarket(MarketData(key, secret)),
        broker=broker,
        execution=adapter,
        orders=LiveOrderReader(adapter),
        veto=veto,
        limits=settings.risk,
        kill_switch=settings.kill_switch,
        dry_run=dry_run,
        clock=_CallableClock(clock or (lambda: datetime.now(UTC))),
    )
    log.info(
        "agent built: journal=%s dry_run=%s kill_switch=%s",
        journal_path,
        dry_run,
        settings.kill_switch,
    )
    return Agent(cycle=cycle, journal=journal, settings=settings, broker=broker)
