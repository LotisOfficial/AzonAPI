"""Background repository switching with progress reporting and a small in-memory cache.

Loading a Protégé project takes a while, so a switch must not block the request that asked for it.
`RepositorySwitcher.start()` returns at once; the load runs on a worker thread; `status()` says how
far along it is. Recently loaded repositories stay in memory, so switching back to one is immediate
unless its files changed on disk.

Protégé gives no progress callbacks. The progress figure is an estimate: elapsed time against how long
this repository took last time, or against a bytes-per-second rate measured on earlier loads. When
there is nothing to estimate from, `progress` is None and the page shows an indeterminate bar.

This module does not import the JVM, so it can be tested with a fake loader.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

DATA_SUFFIXES = {".pprj", ".pont", ".pins"}


class SwitchInProgress(RuntimeError):
    """Raised when a switch to a different repository is requested while one is still loading."""


def repository_files(path: str):
    """The project file and the class/instance files beside it."""
    pprj = Path(path)
    try:
        return sorted(p for p in pprj.parent.iterdir() if p.is_file() and p.suffix.lower() in DATA_SUFFIXES)
    except OSError:
        return []


def file_signature(path: str) -> Tuple:
    """Changes whenever any of the repository's files is modified."""
    signature = []
    for item in repository_files(path):
        try:
            stat = item.stat()
        except OSError:
            continue
        signature.append((item.name, stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def total_size(path: str) -> int:
    return sum(entry[2] for entry in file_signature(path))


class RepositorySwitcher:
    def __init__(
        self,
        loader: Callable[[str], Tuple[object, object]],
        on_loaded: Callable[[str, object, object], None],
        cache_size: int = 2,
        signature: Callable[[str], Tuple] = file_signature,
        size: Callable[[str], int] = total_size,
        clock: Callable[[], float] = time.monotonic,
        run_async: bool = True,
        on_evict: Optional[Callable[[object], None]] = None,
    ):
        self._loader, self._on_loaded, self._on_evict = loader, on_loaded, on_evict
        self._cache_size = max(1, int(cache_size))
        self._signature, self._size, self._clock, self._run_async = signature, size, clock, run_async
        self._lock = threading.RLock()
        self._cache: "OrderedDict[str, Tuple[object, object, Tuple]]" = OrderedDict()
        self._seconds: Dict[str, float] = {}  # how long each repository took to load
        self._bytes: Dict[str, int] = {}
        self._active: Optional[str] = None
        self._state = {"state": "idle", "path": None, "cached": False, "message": "", "started": None,
                       "finished": None, "estimate": None}

    # ---- cache

    def _remember(self, path: str, project, kb, signature, seconds: Optional[float]) -> None:
        self._cache[path] = (project, kb, signature)
        self._cache.move_to_end(path)
        if seconds is not None and seconds > 0:
            self._seconds[path] = seconds
            self._bytes[path] = self._size(path)
        while len(self._cache) > self._cache_size:
            oldest = next(iter(self._cache))
            if oldest == self._active and len(self._cache) > 1:
                self._cache.move_to_end(oldest)
                oldest = next(iter(self._cache))
            evicted_project, _, _ = self._cache.pop(oldest)
            if self._on_evict is not None:
                try:
                    self._on_evict(evicted_project)
                except Exception as exc:  # pylint: disable=broad-except
                    print(f"[repo] could not release {oldest}: {exc}")

    def prime(self, path: str, project, kb, seconds: Optional[float] = None) -> None:
        """Record a repository that was loaded outside start(), such as the one loaded at start-up."""
        with self._lock:
            self._active = path
            self._remember(path, project, kb, self._signature(path), seconds)
            if self._state["state"] == "idle":
                self._state.update(state="ready", path=path, cached=False, message="")

    def refresh(self, path: Optional[str]) -> None:
        """Call after saving: the files changed because we wrote them, so the cached copy is still current."""
        if not path:
            return
        with self._lock:
            entry = self._cache.get(path)
            if entry is not None:
                self._cache[path] = (entry[0], entry[1], self._signature(path))

    # ---- estimate

    def _estimate(self, path: str) -> Optional[float]:
        if path in self._seconds:
            return self._seconds[path]
        known_bytes = sum(self._bytes.values())
        known_seconds = sum(self._seconds.values())
        size = self._size(path)
        if known_bytes > 0 and known_seconds > 0 and size > 0:
            return size * known_seconds / known_bytes
        return None

    # ---- switching

    def start(self, path: str) -> dict:
        """Begin switching to `path`. Returns the status straight away."""
        with self._lock:
            if self._state["state"] == "loading":
                if self._state["path"] == path:
                    return self.status()
                raise SwitchInProgress(f"Already loading {self._state['path']}")

            signature = self._signature(path)
            entry = self._cache.get(path)
            now = self._clock()
            if entry is not None and entry[2] == signature:
                self._cache.move_to_end(path)
                self._active = path
                self._on_loaded(path, entry[0], entry[1])
                self._state.update(state="ready", path=path, cached=True, message="", started=now, finished=now, estimate=0.0)
                return self.status()

            self._state.update(state="loading", path=path, cached=False, message="", started=now, finished=None,
                               estimate=self._estimate(path))

        if self._run_async:
            threading.Thread(target=self._run, args=(path, signature), name="repo-switch", daemon=True).start()
        else:
            self._run(path, signature)
        return self.status()

    def _run(self, path: str, signature) -> None:
        began = self._clock()
        try:
            project, kb = self._loader(path)
        except Exception as exc:  # pylint: disable=broad-except
            with self._lock:
                # The previous repository is still active and still being served.
                self._state.update(state="error", message=f"Could not load the repository: {exc}", finished=self._clock())
            return
        with self._lock:
            self._active = path
            self._remember(path, project, kb, signature, self._clock() - began)
            self._on_loaded(path, project, kb)
            self._state.update(state="ready", message="", finished=self._clock())

    def status(self) -> dict:
        with self._lock:
            state = dict(self._state)
        path = state["path"]
        started, finished, estimate = state["started"], state["finished"], state["estimate"]
        elapsed = None
        if started is not None:
            elapsed = (finished if finished is not None else self._clock()) - started
        if state["state"] == "ready":
            progress = 1.0
        elif state["state"] == "loading" and estimate and elapsed is not None:
            progress = min(0.95, elapsed / estimate)  # never claims to be finished before it is
        else:
            progress = None
        return {
            "state": state["state"],
            "path": path,
            "file": Path(path).name if path else None,
            "name": Path(path).parent.name if path else None,
            "cached": state["cached"],
            "message": state["message"],
            "elapsedSeconds": round(elapsed, 1) if elapsed is not None else None,
            "estimatedSeconds": round(estimate, 1) if estimate else None,
            "progress": round(progress, 3) if progress is not None else None,
        }
