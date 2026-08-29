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

from underwriter.export import API_FILES, export
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

    def test_paths_match_the_frontend_fetches_exactly(self) -> None:
        # Guards the literal list against drift: a route added to the dashboard
        # without being exported is a missing file in production, and the page
        # would show an endpoint-unreachable panel forever.
        page = Path("src/underwriter/static/index.html").read_text()
        for name in API_FILES:
            assert f"/api/{name}" in page, name

    def test_index_is_copied_alongside(self, journal: Journal, tmp_path: Path) -> None:
        export(journal, tmp_path, now=NOW)
        assert (tmp_path / "index.html").is_file()

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
