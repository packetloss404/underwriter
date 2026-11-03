"""Export a finite, self-contained dashboard snapshot.

The live dashboard needs dynamic date queries, archive pagination and polling.
Copying its HTML onto a static host cannot supply those contracts. This exporter
instead renders explicit snapshot pages with bounded recent evidence and the two
independent P&L series. They have no scripts, external assets or API requests.
The accompanying JSON files preserve their generation/observation timestamps;
they do not claim to be a live health check or a complete historical archive.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

from underwriter.config import RiskLimits
from underwriter.dashboard import (
    DEFAULT_DECISION_LIMIT,
    DEFAULT_ORDER_LIMIT,
    DEFAULT_PNL_DAYS,
    DEFAULT_REJECTION_LIMIT,
    decisions_payload,
    health_payload,
    orders_payload,
    overview_payload,
    pnl_payload,
    positions_payload,
    rejections_payload,
    state_payload,
)
from underwriter.journal import DEFAULT_MAX_VIEW_AGE, Journal
from underwriter.runtime import Schedule

# The finite snapshot has two pages. Live History and Reports require the server.
PAGES: tuple[str, ...] = ("index.html", "ledger.html")
API_FILES: tuple[str, ...] = (
    "health",
    "overview",
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
    """Do not freeze a live freshness claim into a JSON snapshot."""
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
        "health": lambda: {
            **health_payload(journal, now=now),
            "status": "snapshot",
            "note": "Journal readability was checked at generation time, not at viewing time.",
        },
        "overview": lambda: overview_payload(
            journal, now=now, max_view_age=max_view_age, limits=RiskLimits(), schedule=Schedule()
        ),
        "state": lambda: state_payload(journal, now=now, max_view_age=max_view_age),
        "positions": lambda: positions_payload(journal, now=now),
        "decisions": lambda: decisions_payload(journal, now=now, limit=DEFAULT_DECISION_LIMIT),
        "rejections": lambda: rejections_payload(journal, now=now, limit=DEFAULT_REJECTION_LIMIT),
        "pnl": lambda: pnl_payload(journal, now=now, days=pnl_days),
        "orders": lambda: orders_payload(journal, now=now, limit=DEFAULT_ORDER_LIMIT),
    }
    return {name: _strip_baked_age(build()) for name, build in builders.items()}


def _cell(value: object) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, (dict, list, tuple)):
        return "<pre>" + escape(json.dumps(value, indent=2, default=str)) + "</pre>"
    return escape(str(value))


def _table(rows: Sequence[Mapping[str, Any]], columns: tuple[tuple[str, str], ...]) -> str:
    if not rows:
        return "<p>No recorded observations in this snapshot.</p>"
    head = "".join("<th>" + escape(label) + "</th>" for _, label in columns)
    body = "".join(
        "<tr>" + "".join("<td>" + _cell(row.get(key)) + "</td>" for key, _ in columns) + "</tr>"
        for row in rows
    )
    return '<div class="scroll"><table><thead><tr>' + head + "</tr></thead><tbody>" + body + (
        "</tbody></table></div>"
    )


def _snapshot_page(
    payloads: Mapping[str, dict[str, Any]], *, now: datetime, ledger: bool, prefix: str = ""
) -> str:
    """Render escaped observations without borrowing the live application's UI."""
    decisions = payloads["decisions"]
    orders = payloads["orders"]
    positions = payloads["positions"]
    overview = payloads["overview"]
    content = ""
    if not ledger:
        content += "<h2>Official account observations</h2>"
        content += _table(
            [overview["money"]],
            (
                ("equity_usd", "Equity · USD"),
                ("equity_as_of", "Equity observed at · UTC"),
                ("realised_today_usd", "Session realised P&L · USD"),
                ("unrealised_usd", "Session unrealised P&L · USD"),
                ("open_risk_usd", "Open maximum loss · USD"),
            ),
        )
        content += "<p>Official paper figures are reported, not trusted. Unknown is not zero.</p>"
        content += "<h2>Last observed book</h2>"
        content += "<p>Observed at: " + _cell(positions["data_as_of"]) + "</p>"
        content += (
            _table(
                positions["positions"],
                (
                    ("underlying", "Underlying"),
                    ("spreads", "Spreads"),
                    ("max_loss_usd", "Maximum loss · USD"),
                    ("unrealised_pnl_usd", "Unrealised P&L · USD"),
                    ("detail", "Recorded detail"),
                ),
            )
            if positions["observed"]
            else "<p>The book has never been observed; its size is unknown.</p>"
        )
        content += "<h2>Independent P&L sources</h2><p>Never summed or blended.</p>"
        for name, series in payloads["pnl"]["series"].items():
            content += "<h3>" + escape(name.title()) + "</h3><p>" + escape(series["note"]) + "</p>"
            content += _table(
                series["points"],
                (
                    ("trading_day", "Session · Eastern"),
                    ("at", "Observed at · UTC"),
                    ("equity_usd", "Equity · USD"),
                    ("realised_pnl_usd", "Realised P&L · USD"),
                    ("unrealised_pnl_usd", "Unrealised P&L · USD"),
                ),
            )
    else:
        content += "<h2>Recent decision evidence</h2>"
        content += "<p>At most " + str(DEFAULT_DECISION_LIMIT) + (
            " recent stage events. Stage approvals are not orders or fills.</p>"
        )
        content += _table(
            decisions["decisions"],
            (
                ("at", "Recorded at · UTC"),
                ("symbol", "Symbol"),
                ("stage", "Stage"),
                ("accepted", "Stage accepted"),
                ("reasons", "Reasons"),
                ("detail", "Recorded detail"),
                ("context", "Recorded context"),
            ),
        )
        content += "<h2>Recent order evidence</h2><p>At most " + str(DEFAULT_ORDER_LIMIT) + (
            " parent orders. Quantities are spreads, not leg contracts.</p>"
        )
        content += _table(
            orders["orders"],
            (
                ("symbol", "Symbol"),
                ("status", "Status"),
                ("spreads_ordered", "Spreads ordered"),
                ("spreads_filled", "Spreads filled"),
                ("net_price_per_spread_signed", "Signed net price · USD per share"),
                ("detail", "Recorded detail"),
            ),
        )
    evidence = " · ".join(
        '<a href="' + escape(prefix) + 'api/' + name + '">' + escape(name) + " JSON</a>"
        for name in API_FILES
    )
    title = "Snapshot ledger" if ledger else "Snapshot overview"
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Underwriter · """ + title + """</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;background:#09131e;color:#dce5e9;font:16px/1.55 system-ui,sans-serif}
main{max-width:1200px;margin:auto;padding:24px}a{color:#71d5db}
nav{display:flex;gap:24px;flex-wrap:wrap}.notice{padding:20px;border:1px solid #c0a35a}
h1,h2,h3{font-weight:500}h2{margin-top:40px}
.scroll{max-width:100%;overflow-x:auto}table{width:100%;border-collapse:collapse}
th,td{padding:12px;text-align:left;border-bottom:1px solid #34424d;vertical-align:top}
th{white-space:nowrap}pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;max-width:360px}
footer{margin-top:40px;overflow-wrap:anywhere}
</style></head><body><main><nav aria-label="Snapshot navigation">
<a href='""" + escape(prefix) + """index.html'>Snapshot overview</a>
<a href='""" + escape(prefix) + """ledger.html'>Snapshot ledger</a>
</nav><h1>Underwriter · """ + title + """</h1>
<aside class="notice"><strong>Static snapshot · not live</strong><p>Generated at """ + escape(
        now.astimezone(UTC).isoformat()
    ) + """. Observations may be older than this export. This page never polls.
Recent evidence is bounded; dynamic History, Reports, date filtering and the complete
decision archive require the running service.</p></aside>
""" + content + "<footer><h2>Snapshot JSON evidence</h2><p>" + evidence + (
        "</p><p>Journal readability and operating posture describe generation time. "
        "They do not establish current broker, model or agent connectivity.</p></footer>"
        "</main></body></html>"
    )



def _validate_output(journal: Journal, root: Path) -> None:
    """Refuse a snapshot layout that exposes or overwrites its input database."""
    resolved_root = root.resolve()
    targets = [
        *(root / "api" / name for name in API_FILES),
        *(root / name for name in PAGES),
        root / "ledger" / "index.html",
    ]
    if any(not target.resolve().is_relative_to(resolved_root) for target in targets):
        raise ValueError("Snapshot output files must stay inside the output directory")
    if str(journal.path) == ":memory:":
        return
    database = Path(journal.path).resolve()
    if database.is_relative_to(resolved_root):
        raise ValueError("Snapshot output directory must not contain the input journal")
    protected = [database, Path(str(database) + "-wal"), Path(str(database) + "-shm")]
    if any(
        target.exists() and source.exists() and target.samefile(source)
        for target in targets
        for source in protected
    ):
        raise ValueError("Snapshot output files must not alias the input journal or its sidecars")

def export(
    journal: Journal,
    out_dir: Path | str,
    *,
    static_dir: Path | str | None = None,
    now: datetime | None = None,
    max_view_age: timedelta = DEFAULT_MAX_VIEW_AGE,
    pnl_days: int = DEFAULT_PNL_DAYS,
) -> ExportResult:
    """Write finite snapshot pages and JSON; never copy the live frontend."""
    moment = now or datetime.now(UTC)
    root = Path(out_dir)
    _validate_output(journal, root)
    api = root / "api"
    api.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    payloads = _payloads(journal, now=moment, max_view_age=max_view_age, pnl_days=pnl_days)
    for name in API_FILES:
        target = api / name
        target.write_text(json.dumps(payloads[name], indent=1, default=str), encoding="utf-8")
        written.append(target)

    # Preserve the optional data-only export when no static source is present.
    source = Path(static_dir) if static_dir else Path(__file__).parent / "static"
    for name in PAGES:
        if not (source / name).is_file():
            continue
        page = _snapshot_page(payloads, now=moment, ledger=name == "ledger.html")
        destination = root / name
        destination.write_text(page, encoding="utf-8")
        written.append(destination)

    if (source / "ledger.html").is_file():
        nested = root / "ledger"
        nested.mkdir(parents=True, exist_ok=True)
        target = nested / "index.html"
        target.write_text(
            _snapshot_page(payloads, now=moment, ledger=True, prefix="../"), encoding="utf-8"
        )
        written.append(target)

    return ExportResult(out_dir=root, files=tuple(written), generated_at=moment)


def main() -> int:
    """Export from the configured journal. Reads only; writes only to out_dir."""
    import argparse

    parser = argparse.ArgumentParser(description="Export a finite, labelled dashboard snapshot.")
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
    print("Static snapshot only; live History, Reports and archive queries require the server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
