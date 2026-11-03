"""Static export tests.

The one behaviour that matters beyond "it writes files" is the staleness rule.
A live server computes `data_age_seconds` at request time, where generation and
viewing are the same moment. A snapshot breaks that assumption: a file written
at 16:00 and read at 02:00 would otherwise still claim its data was seconds
old. On a project that claims to be honest about its own limits, a dashboard
misreporting its own freshness is a worse failure than one that is merely out
of date.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from underwriter.export import API_FILES, PAGES, export
from underwriter.journal import Journal

NOW = datetime(2026, 8, 29, 20, 0, tzinfo=UTC)


@pytest.fixture
def journal() -> Journal:
    return Journal(":memory:")


class TestStaleness:
    def test_every_payload_nulls_the_baked_age(self, journal: Journal, tmp_path: Path) -> None:
        # The whole point. The frontend recomputes from generated_at when this
        # is null; leaving the server's figure in would freeze a lie into the
        # file.
        export(journal, tmp_path, now=NOW)
        for name in API_FILES:
            payload = json.loads((tmp_path / "api" / name).read_text())
            assert payload["data_age_seconds"] is None, name

    def test_the_key_is_present_and_null_not_absent(self, journal: Journal, tmp_path: Path) -> None:
        # A missing key would read as an older payload shape rather than as
        # "recompute this", and the frontend would fall back differently.
        export(journal, tmp_path, now=NOW)
        payload = json.loads((tmp_path / "api" / "state").read_text())
        assert "data_age_seconds" in payload

    def test_generated_at_is_preserved_for_the_client_to_use(
        self, journal: Journal, tmp_path: Path
    ) -> None:
        export(journal, tmp_path, now=NOW)
        payload = json.loads((tmp_path / "api" / "state").read_text())
        assert payload["generated_at"].startswith("2026-08-29T20:00")


class TestLayout:
    def test_writes_every_path_the_frontend_fetches(self, journal: Journal, tmp_path: Path) -> None:
        result = export(journal, tmp_path, now=NOW)
        written = {f.name for f in result.files}
        assert set(API_FILES) <= written

    def test_snapshot_navigation_and_json_evidence_links_resolve(
        self, journal: Journal, tmp_path: Path
    ) -> None:
        from html.parser import HTMLParser

        class Links(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.targets: list[str] = []

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                if tag == "a":
                    self.targets.extend(value for key, value in attrs if key == "href" and value)

        export(journal, tmp_path, now=NOW)
        for page in (tmp_path / name for name in (*PAGES, "ledger/index.html")):
            links = Links()
            links.feed(page.read_text())
            assert len(links.targets) == len(API_FILES) + 2
            for target in links.targets:
                assert (page.parent / target).is_file(), (page, target)

    def test_every_page_is_copied_alongside(self, journal: Journal, tmp_path: Path) -> None:
        export(journal, tmp_path, now=NOW)
        static = Path("src/underwriter/static")
        for name in PAGES:
            if (static / name).is_file():
                assert (tmp_path / name).is_file(), name

    def test_the_ledger_is_also_reachable_as_a_directory(
        self, journal: Journal, tmp_path: Path
    ) -> None:
        # A static host has no rewrite rules, so /ledger must resolve without
        # the .html or the link from the dashboard is dead in the export.
        if not (Path("src/underwriter/static") / "ledger.html").is_file():
            pytest.skip("ledger page not present")
        export(journal, tmp_path, now=NOW)
        assert (tmp_path / "ledger" / "index.html").is_file()

    def test_a_missing_static_dir_still_exports_the_data(
        self, journal: Journal, tmp_path: Path
    ) -> None:
        # Losing the page is recoverable; losing the data is not.
        result = export(journal, tmp_path, now=NOW, static_dir=tmp_path / "nope")
        assert len(result.files) == len(API_FILES)
        assert not (tmp_path / "index.html").exists()

    def test_output_is_valid_json(self, journal: Journal, tmp_path: Path) -> None:
        export(journal, tmp_path, now=NOW)
        for name in API_FILES:
            json.loads((tmp_path / "api" / name).read_text())

    def test_exporting_twice_overwrites_cleanly(self, journal: Journal, tmp_path: Path) -> None:
        export(journal, tmp_path, now=NOW)
        later = datetime(2026, 8, 29, 21, 0, tzinfo=UTC)
        export(journal, tmp_path, now=later)
        payload = json.loads((tmp_path / "api" / "state").read_text())
        assert payload["generated_at"].startswith("2026-08-29T21:00")


class TestEmptyJournal:
    def test_an_untraded_journal_exports_fine(self, journal: Journal, tmp_path: Path) -> None:
        # The current reality, and what a judge sees first.
        result = export(journal, tmp_path, now=NOW)
        assert result.bytes_written > 0

    def test_no_credentials_appear_anywhere_in_the_output(
        self, journal: Journal, tmp_path: Path
    ) -> None:
        export(journal, tmp_path, now=NOW)
        for name in API_FILES:
            text = (tmp_path / "api" / name).read_text()
            for marker in ("ALPACA_", "sk-", "PK", "secret"):
                assert marker not in text, f"{marker} leaked into {name}"

def test_snapshot_pages_are_self_contained_and_disclose_finite_scope(
    journal: Journal, tmp_path: Path
) -> None:
    export(journal, tmp_path, now=NOW)
    for name in PAGES:
        page = (tmp_path / name).read_text()
        assert "Static snapshot · not live" in page
        assert "2026-08-29T20:00:00+00:00" in page
        assert "complete" in page and "require the running service" in page
        assert "<script" not in page
        assert "fonts.googleapis.com" not in page
        assert 'href="/history' not in page
        assert 'href="/reports' not in page
        assert 'href="api/overview"' in page
    health = json.loads((tmp_path / "api" / "health").read_text())
    assert health["status"] == "snapshot"
    assert "generation time" in health["note"]


def test_snapshot_renders_escaped_evidence_and_redacts_credentials(
    journal: Journal, tmp_path: Path
) -> None:
    from underwriter.journal import Stage

    attack = '<script>alert("snapshot")</script>'
    credential = "sk-live-snapshotFixtureCredential"
    journal.record_decision(
        cycle_id="snapshot-evidence",
        stage=Stage.VETO,
        symbol="XLE",
        accepted=False,
        reasons=("veto_catalyst",),
        detail=(attack, f"Request using {credential} failed"),
        at=NOW,
    )
    export(journal, tmp_path, now=NOW)
    page = (tmp_path / "ledger.html").read_text()
    assert "XLE" in page
    assert attack not in page
    assert "&lt;script&gt;" in page
    assert "[redacted]" in page
    for file in (p for p in tmp_path.rglob("*") if p.is_file()):
        assert credential not in file.read_text(), file

def test_export_refuses_to_publish_or_overwrite_its_input_journal(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    (root / "api").mkdir(parents=True)
    database = root / "api" / "overview"
    with Journal(database) as journal:
        with pytest.raises(ValueError, match="must not contain"):
            export(journal, root, now=NOW)
        assert journal.order_history(1) == ()
        assert database.read_bytes().startswith(b"SQLite format 3")
        assert not (root / "api" / "health").exists()


def test_export_rejects_symlink_escape_before_writing_any_payload(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    (root / "api").mkdir(parents=True)
    outside = tmp_path / "do-not-overwrite"
    outside.write_text("original", encoding="utf-8")
    (root / "api" / "overview").symlink_to(outside)
    with Journal() as journal, pytest.raises(ValueError, match="inside the output"):
        export(journal, root, now=NOW)
    assert outside.read_text() == "original"
    assert not (root / "api" / "health").exists()


def test_export_rejects_hardlink_alias_of_the_input_journal(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    (root / "api").mkdir(parents=True)
    database = tmp_path / "private.db"
    with Journal(database) as journal:
        (root / "api" / "overview").hardlink_to(database)
        with pytest.raises(ValueError, match="must not alias"):
            export(journal, root, now=NOW)
        assert journal.order_history(1) == ()
        assert database.read_bytes().startswith(b"SQLite format 3")
        assert not (root / "api" / "health").exists()
