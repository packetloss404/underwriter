"""Static export of the dashboard.

The judged URL must be reachable when a judge looks at it, which will not
always be when the trading host is awake. Serving the dashboard from the
machine that runs the agent couples the two: the agent going quiet overnight
would take the submission's hosted application down with it.

So the agent writes a static snapshot and something always-on serves it. Every
dashboard route is already a thin wrapper over a pure payload function, and the
frontend fetches seven fixed paths with no query strings, so the export is a
loop over those functions and the page needs no changes at all.

**The staleness rule is the important part.** `_envelope` computes
`data_age_seconds` at generation time. In a live server that is correct, because
generation and viewing are the same moment. In a snapshot they are not: a file
written at 16:00 and read at 02:00 would still claim its data was seconds old.
The exporter therefore sets that field to None, which the frontend treats as a
signal to recompute the age client-side from `generated_at`. A dashboard that
misreports its own freshness is a worse failure here than one that is simply
out of date, because the whole project claims to be honest about its limits.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from underwriter.dashboard import (
    DEFAULT_DECISION_LIMIT,
    DEFAULT_ORDER_LIMIT,
    DEFAULT_PNL_DAYS,
    DEFAULT_REJECTION_LIMIT,
    decisions_payload,
    health_payload,
    orders_payload,
    pnl_payload,
    positions_payload,
    rejections_payload,
    state_payload,
)
from underwriter.journal import DEFAULT_MAX_VIEW_AGE, Journal

# The exact paths the frontend fetches. Kept as a literal rather than derived
# from the FastAPI app, so that adding a route without exporting it is a
# visible omission here rather than a silently missing file in production.
# The HTML pages, kept as a literal for the same reason as the API list: a
# page added without being exported is a broken link in production rather than
# a visible omission here.
PAGES: tuple[str, ...] = ("index.html", "ledger.html")

API_FILES: tuple[str, ...] = (
    "health",
    "state",
    "positions",
    "decisions",
    "rejections",
    "pnl",
    "orders",
)


@dataclass(frozen=True, slots=True)
class ExportResult:
    out_dir: Path
    files: tuple[Path, ...]
    generated_at: datetime

    @property
    def bytes_written(self) -> int:
        return sum(f.stat().st_size for f in self.files if f.exists())


def _strip_baked_age(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Blank the server-computed age so the page recomputes it on view.

    Left as-is, a snapshot claims the freshness it had when it was written. The
    frontend already recomputes from `generated_at` when this is null, so
    nulling it is the whole fix -- and it is a deliberate null rather than an
    omission, because a missing key would read as an older payload shape.
    """
    out = dict(payload)
    if "data_age_seconds" in out:
        out["data_age_seconds"] = None
    return out


def _payloads(
    journal: Journal,
    *,
    now: datetime,
    max_view_age: timedelta,
    pnl_days: int,
) -> dict[str, dict[str, Any]]:
    builders: dict[str, Callable[[], Mapping[str, Any]]] = {
        "health": lambda: health_payload(journal, now=now),
        "state": lambda: state_payload(journal, now=now, max_view_age=max_view_age),
        "positions": lambda: positions_payload(journal, now=now),
        "decisions": lambda: decisions_payload(journal, now=now, limit=DEFAULT_DECISION_LIMIT),
        "rejections": lambda: rejections_payload(journal, now=now, limit=DEFAULT_REJECTION_LIMIT),
        "pnl": lambda: pnl_payload(journal, now=now, days=pnl_days),
        "orders": lambda: orders_payload(journal, now=now, limit=DEFAULT_ORDER_LIMIT),
    }
    return {name: _strip_baked_age(build()) for name, build in builders.items()}


def export(
    journal: Journal,
    out_dir: Path | str,
    *,
    static_dir: Path | str | None = None,
    now: datetime | None = None,
    max_view_age: timedelta = DEFAULT_MAX_VIEW_AGE,
    pnl_days: int = DEFAULT_PNL_DAYS,
) -> ExportResult:
    """Write the whole dashboard as static files.

    Produces `index.html` plus one JSON file per API path, laid out so the
    frontend's existing fetches resolve unchanged.
    """
    moment = now or datetime.now(UTC)
    root = Path(out_dir)
    api = root / "api"
    api.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    payloads = _payloads(journal, now=moment, max_view_age=max_view_age, pnl_days=pnl_days)
    for name in API_FILES:
        target = api / name
        target.write_text(json.dumps(payloads[name], indent=1, default=str), encoding="utf-8")
        written.append(target)

    # Both pages, plus /ledger as a directory so the link works on a static
    # host that has no rewrite rules.
    source = Path(static_dir) if static_dir else Path(__file__).parent / "static"
    for name in PAGES:
        page = source / name
        if not page.is_file():
            continue
        destination = root / name
        destination.write_text(page.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(destination)

    ledger = source / "ledger.html"
    if ledger.is_file():
        nested = root / "ledger"
        nested.mkdir(parents=True, exist_ok=True)
        target = nested / "index.html"
        target.write_text(ledger.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(target)

    return ExportResult(out_dir=root, files=tuple(written), generated_at=moment)


def main() -> int:
    """Export from the configured journal. Reads only; writes only to out_dir."""
    import argparse

    parser = argparse.ArgumentParser(description="Export the dashboard as static files.")
    parser.add_argument("--journal", default="underwriter.db", help="path to the journal database")
    parser.add_argument("--out", default="dist", help="directory to write into")
    args = parser.parse_args()

    journal = Journal(args.journal)
    try:
        result = export(journal, args.out)
    finally:
        journal.close()

    print(
        f"exported {len(result.files)} files ({result.bytes_written:,} bytes) to {result.out_dir}"
    )
    print(f"generated_at {result.generated_at.isoformat()}")
    print("data_age_seconds is null in every payload; the page recomputes it on view")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
