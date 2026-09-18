from __future__ import annotations

import threading
import time

from periodica.jellyfin import JellyfinError, RefreshCoordinator
from periodica.settings import Settings

CONFIGURED = Settings(jellyfin_url="http://192.168.1.10:8096", jellyfin_api_key="a" * 32)


class RecordingJellyfin:
    def __init__(self):
        self.folders = []
        self.full = 0

    def __call__(self, settings):
        return self

    def refresh_library_folder(self, library_id):
        self.folders.append(library_id)

    def refresh_library(self):
        self.full += 1


def test_selected_library_is_scanned_instead_of_all(world):
    jf = RecordingJellyfin()
    coord = RefreshCoordinator(world.repo, jf)
    settings = CONFIGURED.model_copy(update={"jellyfin_library_id": "0c0e1e7b5a6b4b0a9f0e3a1d2c3b4a59",
                                             "jellyfin_library_name": "News"})
    coord.request(settings, "manual delete by admin", wait=True)
    _wait_idle(coord)
    assert jf.folders == ["0c0e1e7b5a6b4b0a9f0e3a1d2c3b4a59"] and jf.full == 0
    assert world.repo.activity(1)[0]["message"] == "Jellyfin: scan of library 'News' requested (manual delete by admin)"


class SlowJellyfin:
    def __init__(self, delay=0.2, fail=False):
        self.delay = delay
        self.fail = fail
        self.calls = 0
        self.started = threading.Event()

    def __call__(self, settings):
        return self

    def refresh_library(self):
        self.calls += 1
        self.started.set()
        time.sleep(self.delay)
        if self.fail:
            raise JellyfinError("timed out")


def _wait_idle(coord, timeout=5):
    end = time.time() + timeout
    while coord.busy and time.time() < end:
        time.sleep(0.02)
    assert not coord.busy


def test_requests_during_a_scan_are_merged_into_one_follow_up(world):
    jf = SlowJellyfin()
    coord = RefreshCoordinator(world.repo, jf)
    coord.request(CONFIGURED, "scan")
    assert jf.started.wait(2)
    coord.request(CONFIGURED, "manual delete by admin")  # arrives while Jellyfin is scanning
    coord.request(CONFIGURED, "manual delete by admin")
    _wait_idle(coord)
    assert jf.calls == 2
    messages = [a["message"] for a in world.repo.activity(10)]
    assert any("finished" in m and "manual delete by admin" in m for m in messages)


def test_failure_is_logged_and_not_configured_is_noop(world):
    jf = SlowJellyfin(delay=0, fail=True)
    coord = RefreshCoordinator(world.repo, jf)
    coord.request(Settings(), "scan")
    assert jf.calls == 0
    coord.request(CONFIGURED, "scan", wait=True)
    _wait_idle(coord)
    assert world.repo.activity(1)[0]["message"] == "Jellyfin scan of all libraries failed (scan): timed out"
