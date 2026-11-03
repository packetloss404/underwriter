"""Isolated deterministic HTTP fixture; never starts the trading supervisor."""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from fastapi.staticfiles import StaticFiles
from tests.test_dashboard import LONG_LEG, NOW, SHORT_LEG, seed

from underwriter.dashboard import DashboardConfig, create_app
from underwriter.export import export
from underwriter.journal import IntentLeg, Journal, OrderStatus, PnlSource, Stage


def populate(path: Path) -> None:
    with Journal(path) as journal:
        seed(journal)
        journal.record_intent(
            client_order_id="browser-filled-without-event",
            cycle_id="browser-execution",
            symbol="XLE",
            spreads=3,
            payload={"limit_price": "-1.20", "qty": "3", "type": "limit"},
            legs=(
                IntentLeg(
                    occ_symbol=SHORT_LEG, side="sell", ratio_qty=1, position_intent="sell_to_open"
                ),
                IntentLeg(
                    occ_symbol=LONG_LEG, side="buy", ratio_qty=1, position_intent="buy_to_open"
                ),
            ),
            at=NOW - timedelta(minutes=25),
        )
        journal.mark_status(
            "browser-filled-without-event",
            OrderStatus.FILLED,
            spreads_filled=3,
            net_price_per_spread=-1.20,
            at=NOW - timedelta(minutes=24),
        )
        for i in range(123):
            journal.record_decision(
                cycle_id=f"browser-cycle-{i:03}",
                stage=Stage.RANK,
                accepted=False,
                symbol="SPY" if i % 2 == 0 else "QQQ",
                reasons=("premium_below_floor",),
                detail=(f"Browser audit observation {i:03}; premium below floor.",),
                context={"iv": 0.22, "rv": 0.20, "vrp_ratio": 1.10},
                at=NOW - timedelta(days=i % 3, seconds=i),
            )
        journal.record_decision(
            cycle_id="browser-model-failure",
            stage=Stage.VETO,
            accepted=False,
            symbol="SPY",
            reasons=("veto_catalyst",),
            detail=("Model unavailable (APIConnectionError)",),
            at=NOW - timedelta(minutes=10),
        )
        journal.record_decision(
            cycle_id="browser-exploration",
            stage=Stage.EXPLORE,
            accepted=False,
            symbol="GLD",
            reasons=("premium_below_floor",),
            at=NOW - timedelta(minutes=9),
        )
        for offset in (1, 2):
            journal.record_pnl(
                source=PnlSource.OFFICIAL,
                equity=100_000.0 - offset * 100,
                realised_pnl=0,
                unrealised_pnl=-offset * 100,
                at=NOW - timedelta(days=offset),
            )


if __name__ == "__main__":
    with TemporaryDirectory(prefix="underwriter-browser-") as directory:
        root = Path(directory)
        populated = root / "populated.db"
        populate(populated)
        with Journal(populated) as journal:
            export(journal, root / "snapshot", now=NOW)
        with Journal(root / "empty.db"):
            pass
        app = create_app(DashboardConfig(journal_path=populated, clock=lambda: NOW))
        app.mount("/snapshot", StaticFiles(directory=root / "snapshot", html=True))
        app.mount(
            "/empty",
            create_app(DashboardConfig(journal_path=root / "empty.db", clock=lambda: NOW)),
        )
        uvicorn.run(app, host="0.0.0.0", port=18084, log_level="warning")
