"""Process entry point: the dashboard and the agent in one container.

A hosting volume binds to exactly one service, so the two cannot be split
across services and still share a journal file. They run here as two threads
over one SQLite database.

If either stops, the process exits non-zero and the host restarts the whole
thing. A container serving a dashboard for an agent that died hours ago is
worse than one that is plainly down: the dashboard would keep rendering the
last known book as though it were current, which is precisely the kind of
confident-but-wrong state this project spends its effort avoiding.
"""

from __future__ import annotations

import logging
import os
import threading

import uvicorn

from underwriter.config import Settings
from underwriter.dashboard import DashboardConfig, create_app
from underwriter.live import NotReadyToTrade, build_agent
from underwriter.runtime import Supervisor, install_signal_handlers

log = logging.getLogger(__name__)

DEFAULT_JOURNAL = "/data/underwriter.db"
DEFAULT_PORT = 8080


def _journal_path() -> str:
    return os.environ.get("UNDERWRITER_JOURNAL", DEFAULT_JOURNAL)


def _port() -> int:
    # Managed hosts inject the port. Falling back to a fixed one keeps local
    # runs working without configuration.
    raw = os.environ.get("PORT", str(DEFAULT_PORT))
    try:
        return int(raw)
    except ValueError:
        log.warning("PORT=%r is not an integer; using %d", raw, DEFAULT_PORT)
        return DEFAULT_PORT


def _serve_dashboard(stop: threading.Event) -> None:
    app = create_app(DashboardConfig(journal_path=_journal_path()))
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=_port(), log_level="info")  # noqa: S104
    )

    def watch() -> None:
        stop.wait()
        server.should_exit = True

    threading.Thread(target=watch, daemon=True).start()
    server.run()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("UNDERWRITER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    stop = threading.Event()
    install_signal_handlers(stop)

    web = threading.Thread(target=_serve_dashboard, args=(stop,), name="dashboard")
    web.start()

    dry_run = os.environ.get("UNDERWRITER_DRY_RUN", "").lower() == "true"
    try:
        agent = build_agent(Settings(), _journal_path(), dry_run=dry_run)
    except (NotReadyToTrade, ValueError) as exc:
        # Serving a dashboard for an agent that cannot trade is worse than
        # failing: the page would render an empty book indistinguishable from a
        # quiet one. Exit and let the host surface it.
        log.error("cannot start the agent: %s", exc)
        stop.set()
        web.join(timeout=10)
        return 2

    supervisor = Supervisor(run_cycle=agent, stop=stop)
    try:
        code = supervisor.run_forever()
    finally:
        stop.set()
        web.join(timeout=10)
        agent.close()
    log.info("stopped after %d cycles, %d failures", supervisor.cycles, supervisor.failures)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
