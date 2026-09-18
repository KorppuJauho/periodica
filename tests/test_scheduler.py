from __future__ import annotations

import asyncio
from types import SimpleNamespace

from periodica import app as app_module
from periodica.settings import Settings


class CountingScanner:
    def __init__(self):
        self.runs: list = []

    def run(self, dry_run=False, settings=None, trigger=None):
        self.runs.append(trigger)


def _fake_app(interval: int) -> SimpleNamespace:
    store = SimpleNamespace(load=lambda: Settings(scan_interval_minutes=interval))
    state = SimpleNamespace(scanner=CountingScanner(), scan_event=asyncio.Event(), store=store,
                            scan_trigger=None, scan_request_id=0, scan_requested_at=0.0)
    return SimpleNamespace(state=state)


def test_interval_zero_only_scans_at_startup_and_on_trigger(monkeypatch):
    monkeypatch.setattr(app_module, "STARTUP_SCAN_DELAY", 0.01)

    async def scenario():
        app = _fake_app(0)
        task = asyncio.create_task(app_module._scheduler(app))
        await asyncio.sleep(0.3)
        assert app.state.scanner.runs == [None]  # startup scan only, nothing scheduled afterwards
        app_module.request_scan(app, "API scan")
        await asyncio.sleep(0.2)
        assert app.state.scanner.runs == [None, "API scan"]
        task.cancel()

    asyncio.run(scenario())


def test_positive_interval_keeps_scheduling(monkeypatch):
    monkeypatch.setattr(app_module, "STARTUP_SCAN_DELAY", 0.01)

    async def scenario():
        app = _fake_app(1)
        app.state.store = SimpleNamespace(load=lambda: SimpleNamespace(scan_interval_minutes=0.002))  # ~0.12 s
        task = asyncio.create_task(app_module._scheduler(app))
        await asyncio.sleep(0.5)
        assert len(app.state.scanner.runs) >= 3
        task.cancel()

    asyncio.run(scenario())
