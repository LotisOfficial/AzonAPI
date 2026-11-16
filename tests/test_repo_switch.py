"""Offline tests for essential_open_api.repo_switch (fake loader, no JVM).

Run from the repository root:  python -m pytest tests/test_repo_switch.py -q
"""

import importlib.util
import sys
import threading
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "essential_open_api" / "repo_switch.py"
_spec = importlib.util.spec_from_file_location("azon_repo_switch_under_test", _PATH)
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class World:
    """A fake loader that takes a set number of (fake) seconds per repository."""

    def __init__(self, clock):
        self.clock = clock
        self.loads = []
        self.active = None
        self.evicted = []
        self.signatures = {}
        self.sizes = {}
        self.cost = {}
        self.fail = set()

    def loader(self, path):
        self.loads.append(path)
        self.clock.now += self.cost.get(path, 10)
        if path in self.fail:
            raise RuntimeError("broken project")
        return f"project:{path}:{len(self.loads)}", f"kb:{path}:{len(self.loads)}"

    def on_loaded(self, path, project, kb):
        self.active = (path, project, kb)


def make(cache_size=2, run_async=False):
    clock = Clock()
    world = World(clock)
    switcher = mod.RepositorySwitcher(
        loader=world.loader, on_loaded=world.on_loaded, cache_size=cache_size,
        signature=lambda p: world.signatures.get(p, ("v1",)), size=lambda p: world.sizes.get(p, 1000),
        clock=clock, run_async=run_async, on_evict=world.evicted.append)
    return switcher, world, clock


def test_idle_then_first_switch_loads_and_reports_ready():
    switcher, world, _ = make()
    assert switcher.status()["state"] == "idle"
    status = switcher.start("/r/a/a.pprj")
    assert status["state"] == "ready" and status["progress"] == 1.0 and status["cached"] is False
    assert status["file"] == "a.pprj" and status["name"] == "a" and status["elapsedSeconds"] == 10.0
    assert world.active[0] == "/r/a/a.pprj" and world.loads == ["/r/a/a.pprj"]


def test_switching_back_uses_the_cache_and_does_not_reload():
    switcher, world, _ = make()
    switcher.start("/r/a/a.pprj")
    switcher.start("/r/b/b.pprj")
    status = switcher.start("/r/a/a.pprj")
    assert status["state"] == "ready" and status["cached"] is True and status["elapsedSeconds"] == 0.0
    assert world.loads == ["/r/a/a.pprj", "/r/b/b.pprj"]
    assert world.active == ("/r/a/a.pprj", "project:/r/a/a.pprj:1", "kb:/r/a/a.pprj:1")


def test_changed_files_force_a_reload():
    switcher, world, _ = make()
    switcher.start("/r/a/a.pprj")
    switcher.start("/r/b/b.pprj")
    world.signatures["/r/a/a.pprj"] = ("v2",)
    status = switcher.start("/r/a/a.pprj")
    assert status["cached"] is False and world.loads.count("/r/a/a.pprj") == 2


def test_refresh_after_our_own_save_keeps_the_cache_valid():
    switcher, world, _ = make()
    switcher.start("/r/a/a.pprj")
    world.signatures["/r/a/a.pprj"] = ("after-save",)
    switcher.refresh("/r/a/a.pprj")
    switcher.start("/r/b/b.pprj")
    assert switcher.start("/r/a/a.pprj")["cached"] is True


def test_cache_is_bounded_and_never_evicts_the_active_repository():
    switcher, world, _ = make(cache_size=2)
    for name in "abc":
        switcher.start(f"/r/{name}/{name}.pprj")
    assert world.evicted == ["project:/r/a/a.pprj:1"]
    assert switcher.start("/r/b/b.pprj")["cached"] is True
    assert switcher.start("/r/a/a.pprj")["cached"] is False  # was evicted, so loaded again


def test_failed_load_keeps_the_previous_repository_active():
    switcher, world, _ = make()
    switcher.start("/r/a/a.pprj")
    world.fail.add("/r/bad/bad.pprj")
    status = switcher.start("/r/bad/bad.pprj")
    assert status["state"] == "error" and "broken project" in status["message"] and status["progress"] is None
    assert world.active[0] == "/r/a/a.pprj"
    assert switcher.start("/r/a/a.pprj")["cached"] is True  # can go straight back


def test_progress_estimate_from_previous_load_and_from_size():
    switcher, world, clock = make(run_async=True)
    release = threading.Event()
    started = threading.Event()

    def slow_loader(path):
        started.set()
        release.wait(5)
        return "p", "k"

    switcher._loader = slow_loader
    assert switcher.start("/r/a/a.pprj")["progress"] is None  # nothing to estimate from yet
    started.wait(5)
    clock.now += 20
    release.set()
    for thread in threading.enumerate():
        if thread.name == "repo-switch":
            thread.join(5)
    assert switcher.status()["state"] == "ready"

    # Same repository, files changed: estimate is the 20 s it took last time.
    world.signatures["/r/a/a.pprj"] = ("v2",)
    release.clear(), started.clear()
    status = switcher.start("/r/a/a.pprj")
    assert status["state"] == "loading" and status["estimatedSeconds"] == 20.0 and status["progress"] == 0.0
    started.wait(5)
    clock.now += 5
    assert switcher.status()["progress"] == 0.25
    clock.now += 100
    assert switcher.status()["progress"] == 0.95  # capped until it really finishes

    # While loading: same path returns the status, another path is refused.
    assert switcher.start("/r/a/a.pprj")["state"] == "loading"
    with pytest.raises(mod.SwitchInProgress):
        switcher.start("/r/b/b.pprj")
    release.set()
    for thread in threading.enumerate():
        if thread.name == "repo-switch":
            thread.join(5)
    assert switcher.status()["progress"] == 1.0

    # A different repository twice the size: estimated from the measured bytes-per-second rate.
    world.sizes["/r/big/big.pprj"] = 2000
    release.clear()
    status = switcher.start("/r/big/big.pprj")
    assert status["estimatedSeconds"] == pytest.approx(2 * switcher._seconds["/r/a/a.pprj"], rel=0.01)
    release.set()


def test_prime_registers_the_startup_repository():
    switcher, world, _ = make()
    switcher.prime("/r/a/a.pprj", "p0", "k0", seconds=30)
    assert switcher.status()["state"] == "ready" and switcher.status()["path"] == "/r/a/a.pprj"
    switcher.start("/r/b/b.pprj")
    assert switcher.start("/r/a/a.pprj")["cached"] is True
    assert world.active == ("/r/a/a.pprj", "p0", "k0")


def test_file_signature_tracks_pont_and_pins(tmp_path):
    for name in ("x.pprj", "x.pont", "x.pins", "notes.txt"):
        (tmp_path / name).write_text("1")
    pprj = str(tmp_path / "x.pprj")
    first = mod.file_signature(pprj)
    assert [entry[0] for entry in first] == ["x.pins", "x.pont", "x.pprj"]
    assert mod.total_size(pprj) == 3
    (tmp_path / "x.pins").write_text("changed")
    assert mod.file_signature(pprj) != first
    (tmp_path / "notes.txt").write_text("irrelevant change")
    assert mod.file_signature(pprj) == mod.file_signature(pprj)
