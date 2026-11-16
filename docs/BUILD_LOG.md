# Build log

Running record of what has been built against [PLAN.md](PLAN.md), how it was checked, and what is still open. Newest entry first. Each entry states plainly what was **not** tested.

---

## 16 Nov 2026 — Session 1: authentication, lock-downs, production entry point

### Starting point

- Branch `azon-secure`, created from upstream `terryvel/essential-open-api` commit `8512d0a`, plus the local repository-switch feature (endpoints `/api/repository`, `/api/repositories`) that previously existed only as uncommitted changes.
- Remotes: `origin` = `LotisOfficial/AzonAPI`, `upstream` = `terryvel/essential-open-api` (fetch only; never push there).

### Built

| Plan phase | Item | Where |
|---|---|---|
| 0 | Jar checksums recorded | `jars/SHA256SUMS` |
| 0 | Ignore rules for `config/`, `repositories/`, `secrets/`, keys | `.gitignore` |
| 2 | Bearer-token authentication on every `/api/*` route; roles `read` / `write` / `admin`; constant-time comparison; only SHA-256 digests stored | `essential_open_api/auth.py` |
| 2 | Start-up fails when the token file is unset, missing, empty or malformed | `auth.load_tokens`, `create_app` |
| 2 | CORS removed (`Flask-Cors` dropped) | `essential_open_api/__init__.py` |
| 2 | Bodies must be `application/json` (415 otherwise); lenient body parser removed | `auth.authenticate`, `routes.load_json_body` |
| 2 | Swagger UI off unless `AZON_API_DOCS=1` | `create_app` |
| 2 | Token generator | `tools/make_token.py` |
| 3 (part) | 1 MB request body limit (413) | `create_app`, `auth.authenticate` |
| 3 (part) | Audit log lines for publish and repository switch; failed and denied token use logged | `routes.py`, `auth.py` |
| 3 (part) | Loader and save errors logged server-side instead of returned; Protégé load messages now captured | `essential_open_api/jvm.py` |
| 4 | Publish is `POST` only, to named server-side targets; no caller-supplied URL; default credentials removed; stored password read from a secret file | `essential_open_api/targets.py`, `routes.publish` |
| 4 | `GET /api/publish-targets` | `routes.publish_targets` |
| 4 | Repository switch by listed `id` only; absolute paths no longer accepted or returned; saved config stores the id | `jvm.py`, `routes.switch_repository` |
| 5 | `waitress` entry point; `startup.sh`, `sshd_config`, Flask debug settings and the upstream `demo/` page removed | `wsgi.py` |
| 6 (draft) | Non-root, read-only, loopback-bound container definition on Java 8, with file-based secrets | `Dockerfile`, `docker-compose.yml`, `.dockerignore` |

### Bugs found and fixed while testing

1. **Process crash after a failed JVM start.** When the JVM failed to start at boot, the next repository switch tried to use it and the process died with a segmentation fault. JPype still reports the JVM as started after a failed start. `start_jvm()` now remembers the failure, never retries, and callers return a clean error.
2. **Oversized bodies returned 500** instead of 413 when the handler answered before reading the body. The size check now runs in the authentication hook.

### Tested

**Unit tests** — `python -m pytest tests -q`: **59 passed**. They run the real Flask app with the JVM stubbed out and cover: fail-closed start-up (6 cases), 401 without or with bad credentials on every route family, token in query string ignored, role enforcement for read / write / admin, 415 and 413, no CORS headers, Swagger off, publish `GET` → 405, publish targets hide URLs and secrets, publish ignores caller-supplied URL and credentials, repository listing has ids and no paths, repository switch rejects traversal, backslash paths, absolute paths, unknown ids and the legacy `path` field.

**Live smoke test** — `tools/live_smoke_test.sh`, real `waitress` server on `127.0.0.1`, real HTTP, against a scratch copy of a production repository:

| Check | Result |
|---|---|
| `/health` without token | 200 |
| `/api/classes/` without token / with bad token | 401 / 401 |
| Create with read token | 403 |
| Create with `text/plain` body | 415 |
| `/api/repositories` with read token | 200, ids only, no paths |
| Switch repository with write token | 403 |
| Switch with `../` id / legacy `path` field | 400 / 400 |
| `GET /api/publish?...` | 405 |
| `POST /api/publish` with no targets configured | 503 |
| `/api/` (Swagger) | 404 |
| CORS preflight | 401, no `Access-Control-*` headers |
| Listening address | `127.0.0.1` only |
| Start with no token file | process exits with code 1 |
| Switch repository when the JVM is unavailable | 500 with a generic message, process stays up |

### NOT tested — read this before using the build

- **Nothing that needs a loaded knowledge base has been run on this branch.** The build machine had Java 8 and Python 3.14 only; JPype 1.7.1 (the only JPype with a Python 3.14 build) refuses Java 8. So class browsing, instance read / create / update / delete, a successful repository switch, and publish were exercised only as far as authentication and validation. In the live test those calls returned `500 Knowledge Base not loaded`.
- **The changes to `jvm.py` loading and saving are unverified against a real JVM** (`ArrayList` error capture, `JClass` instead of `JPackage`, disposing the previous project on switch).
- **The pins `jpype1==1.5.2` + Python 3.12 + Java 8 are unverified.** See [JAVA_OPTIONS.md](JAVA_OPTIONS.md).
- **The Docker image has not been built.** Docker was not running on the build machine.
- Azon Admin and the MCP server have not been changed and **will not work against this build** until plan phases 8 and 9 are done (they send no token, publish by `GET`, and post `path` to switch repository).

### Next steps, in order

1. Get a Python 3.12 environment with Java 8 (natively or by building the Docker image) and run `tools/live_smoke_test.sh` to completion. Fix whatever the knowledge-base paths show.
2. Plan phase 3: single lock around the knowledge base, write pause during publish, slot rules (`:`-prefixed and non-template slots), batch / depth / page limits, generic error bodies for the remaining handlers that still return exception text, backups before first save.
3. Plan phase 1: hash-locked requirements; jar checksum check at start-up.
4. Plan phases 8 and 9: Azon Admin and MCP server changes.
5. Plan phase 6 and 7 verification in both modes.

### Known gaps in what is built

- Handlers in `routes.py` outside publish and repository switching still return some exception text to authenticated callers.
- Publish job logs are returned as produced by `PublishService`; they have not been checked for credentials.
- `requirements.txt` is version-pinned but not hash-locked.
- There is no rate limiting.
