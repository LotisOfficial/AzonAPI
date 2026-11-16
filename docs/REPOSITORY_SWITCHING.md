# Repository switching

Added 16 November 2026. Code: `essential_open_api/repo_switch.py`, wired in `jvm.py` and `routes.py`. Tests: `tests/test_repo_switch.py`.

## What changed

| Before | Now |
|---|---|
| `POST /api/repository` blocked until Protégé had loaded the project, with nothing to show meanwhile | Returns at once; the load runs on a background thread |
| No way to see how far a switch had got | `GET /api/repository/status` reports state, elapsed time and estimated progress |
| Every switch reloaded from disk | The last `REPO_CACHE_SIZE` repositories (default 2) stay in memory. Switching back to one is immediate unless its `.pprj`, `.pont` or `.pins` file changed on disk |
| Any path ending in `.pprj` was accepted | Only a repository listed by `GET /api/repositories` |
| A failed load left the outcome unclear | The previous repository stays active and keeps being served; the status carries the error message |

## Endpoints

`POST /api/repository` with `{"path": "..."}`:

- `200` and `"state": "ready"`: the repository was already in memory and is now active.
- `202` and `"state": "loading"`: loading has started. Poll the status.
- `409`: another repository is already loading. The body is that load's status.
- `400`: the path is not a listed repository.

`GET /api/repository/status`:

```json
{"state": "loading", "path": "/app/repositories/a230_clean/A230Clean.pprj", "name": "a230_clean",
 "file": "A230Clean.pprj", "cached": false, "elapsedSeconds": 12.4, "estimatedSeconds": 31.0,
 "progress": 0.4, "message": ""}
```

`state` is `idle`, `loading`, `ready` or `error`.

**Callers that relied on `POST /api/repository` finishing the switch before it answered must now wait for `ready`.**

## The progress figure is an estimate

Protégé reports nothing while it loads. `progress` is elapsed time divided by the expected time, where the expected time is how long this repository took last time, or else its size on disk times the seconds-per-byte measured on earlier loads (including the one at start-up). It is capped at 95% until the load really finishes. When there is nothing to estimate from, `progress` is `null` and the page shows a moving bar with elapsed seconds only.

## In the page

- A gold bar along the bottom of the header, and "Loading a230_clean… 12 s, about 19 s left" beside the repository selector.
- The three columns are dimmed and locked while loading, because their contents belong to the repository being replaced.
- When the switch completes, the selected class, instance list, editor, filters and expanded nodes are all cleared and the class tree reloads. No page refresh.
- If the load fails, the selector goes back to the previous repository and the reason is shown in red.
- Opening the page while a switch is running picks it up and shows its progress.

## Memory

Each repository kept in memory costs heap in the JVM. `REPO_CACHE_SIZE=1` keeps only the active one (no instant switch back). A repository dropped from the cache is released with `Project.dispose()`.

## What was verified, and what was not

**Ran** (`python -m pytest tests/test_repo_switch.py`, 9 passed): cache hit and miss, reload when files change, cache kept valid after the API's own saves, cache bound and eviction, failed load keeping the previous repository, progress estimates, refusal of a second concurrent switch, file signature.

**Ran in a browser** with this module and a fake six-second loader: progress text and bar, dimming, state reset, instant switch back, failed load.

**Ran against the complete API in Docker** (Protégé engine, copies of the `a230_clean` and `Arthur` repositories, 16 November 2026):

| Check | Result |
|---|---|
| First switch to a repository not in memory | `202`, loaded on the background thread in 49.9 s (estimate 37.5 s); a second run took 32.9 s (estimate 31.4 s) |
| Requests during the load | Served from the previous repository |
| A different switch requested during the load | `409` with the running load's status |
| Switch back to a repository in memory | `200` in about 0.3 s |
| Switch after one of its files changed on disk | Reloaded |
| Switch back after the API saved an instance | Still served from memory |
| A path outside the listed repositories | `400` |
| In the page | Progress text and bar, columns locked, tree reloaded for the new repository, no refresh |

**Memory:** with two repositories of this size in memory the container used about 2.7 GiB. Set `REPO_CACHE_SIZE=1` on a machine where that is too much.

**Not verified:** eviction with the real engine (`Project.dispose()` was never reached because only two repositories were loaded), and a load that fails inside Protégé.

**Possible improvement:** Protégé prints "Loaded N instances" lines to the container log while loading. Capturing those would give real progress instead of a time estimate.
