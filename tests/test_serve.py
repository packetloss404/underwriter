"""The service must not leave an agent running after its dashboard dies."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from underwriter import serve
from underwriter.config import Settings


class FakeAgent:
    closed = False

    def close(self) -> None:
        self.closed = True

    def end_of_day(self, *_args: object) -> dict[str, object]:
        pytest.fail("service startup tests must not observe a real broker")


def prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeAgent:
    monkeypatch.chdir(tmp_path)
    settings = Settings(ALPACA_API_KEY="fixture-key", ALPACA_SECRET_KEY="fixture-secret")
    agent = FakeAgent()
    monkeypatch.setattr(serve, "Settings", lambda: settings)
    monkeypatch.setattr(serve, "install_signal_handlers", lambda _stop: None)
    monkeypatch.setattr(serve, "build_agent", lambda *_args, **_kwargs: agent)
    return agent


def supervisor(monkeypatch: pytest.MonkeyPatch, run: Callable[[threading.Event], int]) -> None:
    class FakeSupervisor:
        cycles = 0
        failures = 0

        def __init__(self, *, stop: threading.Event, **_kwargs: Any) -> None:
            self.stop = stop

        def run_forever(self) -> int:
            return run(self.stop)

    monkeypatch.setattr(serve, "Supervisor", FakeSupervisor)


def test_dashboard_bind_failure_prevents_starting_the_supervisor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    agent = prepare(monkeypatch, tmp_path)
    failed = threading.Event()
    stops: list[threading.Event] = []

    def fail(stop: threading.Event, _settings: Settings) -> None:
        stops.append(stop)
        failed.set()
        raise SystemExit(1)

    def build(*_args: object, **_kwargs: object) -> FakeAgent:
        assert failed.wait(2)
        assert stops[0].wait(2)
        return agent

    monkeypatch.setattr(serve, "_serve_dashboard", fail)
    monkeypatch.setattr(serve, "build_agent", build)
    supervisor(monkeypatch, lambda _stop: pytest.fail("dashboard unavailable"))
    assert serve.main() == 1
    assert agent.closed


def test_dashboard_failure_stops_an_existing_supervisor_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    agent = prepare(monkeypatch, tmp_path)
    running = threading.Event()

    def fail(_stop: threading.Event, _settings: Settings) -> None:
        assert running.wait(2)
        raise OSError("web listener failed")

    def run(stop: threading.Event) -> int:
        running.set()
        assert stop.wait(2)
        return 0

    monkeypatch.setattr(serve, "_serve_dashboard", fail)
    supervisor(monkeypatch, run)
    assert serve.main() == 1
    assert agent.closed


def test_normal_shutdown_does_not_count_as_a_dashboard_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    agent = prepare(monkeypatch, tmp_path)

    def web(stop: threading.Event, _settings: Settings) -> None:
        assert stop.wait(2)

    def run(stop: threading.Event) -> int:
        stop.set()
        return 0

    monkeypatch.setattr(serve, "_serve_dashboard", web)
    supervisor(monkeypatch, run)
    assert serve.main() == 0
    assert agent.closed


def test_unexpected_agent_construction_failure_stops_the_web_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    prepare(monkeypatch, tmp_path)
    finished = threading.Event()

    def web(stop: threading.Event, _settings: Settings) -> None:
        assert stop.wait(2)
        finished.set()

    def build(*_args: object, **_kwargs: object) -> FakeAgent:
        raise RuntimeError("startup failed")

    monkeypatch.setattr(serve, "_serve_dashboard", web)
    monkeypatch.setattr(serve, "build_agent", build)
    with pytest.raises(RuntimeError, match="startup failed"):
        serve.main()
    assert finished.is_set()
