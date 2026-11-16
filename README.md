# AZON API

Authenticated HTTP API over an Essential / Protégé 3.5 repository (`.pprj`, `.pont`, `.pins`), used by Azon Admin and the AZON MCP server.

This is a hardened fork of [terryvel/essential-open-api](https://github.com/terryvel/essential-open-api). It is **work in progress**: authentication and the main lock-downs are built and tested; several plan phases are still open. Read [docs/BUILD_LOG.md](docs/BUILD_LOG.md) before relying on it.

| Document | What it covers |
|---|---|
| [docs/PLAN.md](docs/PLAN.md) | The full secure rebuild and install plan (10 phases) |
| [docs/BUILD_LOG.md](docs/BUILD_LOG.md) | What has been built, how it was tested, what is still open |
| [docs/PONT_PINS_LOADING.md](docs/PONT_PINS_LOADING.md) | How the API reads and writes Protégé PPRJ, PONT and PINS files |
| [docs/BULK_IMPORT.md](docs/BULK_IMPORT.md) | How the `.dup` bulk import works and a design for rebuilding it without Protégé 3.5 |
| [docs/JAVA_OPTIONS.md](docs/JAVA_OPTIONS.md) | Java 8 pinning, migration path to a newer Java, and options for removing Java |

## Security model

- Every `/api/*` request needs `Authorization: Bearer <token>`. Only `/health` is open.
- Three roles: `read` (all `GET`), `write` (create, update, delete instances), `admin` (publish, repository switch).
- The API will not start without a token file. There are no default credentials.
- No CORS. Serve Azon Admin from the same origin through a reverse proxy.
- Publish goes only to server-configured Viewer targets, by `POST`. Callers cannot supply a URL.
- Repository switching accepts only ids listed by `GET /api/repositories`; filesystem paths are never accepted or returned.

## Requirements

- Java 8 (64-bit). `JAVA_HOME` must point at it.
- Python 3.12 or earlier. JPype 1.5.x is the last line that starts a Java 8 JVM and it has no build for Python 3.13+.

## Run natively

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -r requirements.txt
```

Create tokens (one per client). The token is shown once; add the printed entry to a JSON list in your token file:

```bash
.venv/Scripts/python tools/make_token.py admin-ui admin
```

Set the environment variables listed in [.env.example](.env.example), then start one process bound to loopback:

```bash
.venv/Scripts/waitress-serve --listen=127.0.0.1:5200 --threads=8 wsgi:app
```

## Run in Docker

See the comments at the top of [docker-compose.yml](docker-compose.yml). The image has not been built or tested yet.

## API changes from upstream

| Endpoint | Change |
|---|---|
| all `/api/*` | bearer token required; bodies must be `application/json` |
| `GET /api/publish` | removed; use `POST /api/publish` with `{"target": "<name>"}` |
| `GET /api/publish-targets` | new; lists target names (admin) |
| `POST /api/repository` | takes `{"id": "<id>"}` instead of `{"path": ...}` |
| `GET /api/repository`, `/api/repositories` | return `id`, never filesystem paths |
| `/api/` Swagger UI | off unless `AZON_API_DOCS=1` |

Azon Admin and the MCP server must be updated to match (plan phases 8 and 9).

## Tests

```bash
.venv/Scripts/python -m pip install -r requirements-dev.txt
```

```bash
.venv/Scripts/python -m pytest tests -q
```

`tools/live_smoke_test.sh` runs the same checks over HTTP against a scratch copy of a repository.

## Operating rules

- Do not open a repository in Protégé desktop while the API has it loaded. Both save whole files; the last writer wins.
- Point the API at a copy until locking and backups (plan phase 3) are done.
- Never commit tokens, passwords or repository files.
