# Lotis AZON API

A REST API over AZON (Essential) architecture repositories stored as Protégé 3.5 project files. It serves the [AZON Admin](https://github.com/LotisOfficial/AzonAdmin) editor and anything else that needs to read or change a repository over HTTP.

Python (Flask) with the Protégé engine running in-process through JPype. Derived from [terryvel/essential-open-api](https://github.com/terryvel/essential-open-api).

## Documentation

| Document | What it covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the `.pprj`, `.pont` and `.pins` files are read and written |
| [docs/AUTH.md](docs/AUTH.md) | Sign-in: the on/off switch, settings, managing users, file formats, what was tested |
| [docs/REPOSITORY_SWITCHING.md](docs/REPOSITORY_SWITCHING.md) | Background switching, the progress estimate, the in-memory cache |
| [Outstanding steps](https://github.com/LotisOfficial/AzonAdmin/blob/main/docs/PLAN.md) | The single plan for both repositories |
| [Review and roadmap](https://github.com/LotisOfficial/AzonAdmin/blob/main/docs/AZON_ADMIN_REVIEW_AND_ROADMAP.md) | Security review and the route to replacing Protégé |

## Running it

Needs Docker. The compose file is set up for one development machine: check the volume paths at the top of `docker-compose.yml` (repositories folder, Admin `public/` folder) and adjust them, or override the two `AZON_..._DIR` variables in a `.env` file.

```bash
docker compose up -d --build
```

The first start loads the repository, which takes around half a minute. Then:

- `http://localhost:5200/` is the Admin page (when the Admin `public/` folder is mounted).
- `http://localhost:5200/health` reports whether a repository is loaded.
- `http://localhost:5200/api/` is the Swagger UI.

The port is bound to `127.0.0.1` and debug mode is off. Set `FLASK_DEBUG=1` in `.env` for auto-reload while developing, and never on a machine others can reach.

### Sign-in on or off

One variable. The compose file defaults to **off**, for development and demonstrations.

```
# .env, next to docker-compose.yml
AZON_AUTH_MODE=auth
AZON_TOTP_KEY=<from: users_cli genkey>
```

Remove the first line or set it to `off` to go back. If the variable is not passed to the API at all, the API itself defaults to `auth`. See [docs/AUTH.md](docs/AUTH.md) for creating users.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `PPRJ_FILE` | `resources/essential_baseline_6_20.pprj` | Repository loaded at start-up, unless `config/active.json` remembers another |
| `REPO_ROOT` | `PPRJ_DIR` | Folder scanned (one level deep) for repositories that can be switched to |
| `REPO_CACHE_SIZE` | `2` | How many repositories stay in memory. Each costs over 1 GiB at typical size |
| `CONFIG_FILE` | `config/active.json` | Where the active repository is remembered |
| `JARS_DIR` | `jars` | Protégé and Essential JARs |
| `AZON_AUTH_MODE` and the other `AZON_*` variables | see `docs/AUTH.md` | Sign-in |

## Endpoints

All under `/api` unless shown otherwise. With sign-in on, everything except `/health`, the page and the sign-in endpoints needs a session; writes also need the `X-CSRF-Token` header and an editor account.

| Method and path | Purpose |
|---|---|
| `GET /health` | Liveness and whether a repository is loaded |
| `GET /classes/` | The whole class tree with instance counts |
| `GET /classes/{class}/` | One class and its direct subclasses |
| `GET /classes/{class}/slots` | Slot definitions: type, cardinality, allowed classes |
| `GET /classes/{class}/form` | Stored form layout for the editor |
| `GET /classes/{class}/instances` | Instances; `slots`, `maxdepth`, `directinstances`, `start`, `count` |
| `GET /list_items?class=` | Display names of a class's instances |
| `GET /instances/{id}` | One instance; `slots`, `maxdepth` |
| `POST /instances` | Create. Body: `className`, `name`, then slot values |
| `POST /instances/batch` | Create several |
| `PATCH /instances/{id}` (or `POST`) | Update |
| `DELETE /instances/{id}` | Delete |
| `GET /repository` | The active repository |
| `GET /repositories` | Repositories that can be switched to |
| `POST /repository` | Start a switch. `200` if already in memory, `202` if loading |
| `GET /repository/status` | Progress of the switch |
| `POST /publish` | Publish to a Viewer. Body: `url`, `user`, `pwd`, all required |
| `GET /publish-status?id=` | Status and log of a publish job |
| `POST /auth/login`, `POST /auth/totp`, `GET /auth/me`, `POST /auth/logout` | Sign-in (not under `/api`) |

Every create, update and delete saves the whole project to disk before answering.

## Tests

The sign-in and repository-switching modules are tested without the JVM:

```bash
pip install "Flask>=3.0,<4.0" argon2-cffi==25.1.0 cryptography==50.0.2 defusedxml==0.7.1 pytest
```

```bash
python -m pytest tests -q
```

65 tests. There are no automated tests of the routes that use Protégé; those were checked by hand in Docker against copies of two repositories (results in the two docs above).

**Always test against a copy of a repository.** The API writes to whatever it loads.

## Layout

```
essential_open_api/
  __init__.py        application factory (lazy, so tools can import the package without the JVM)
  auth.py            sign-in, sessions, CSRF, roles, audit log, security headers
  users_cli.py       manage users.xml
  repo_switch.py     background repository switching and cache
  jvm.py             the JVM, loading, saving, publishing
  routes.py          the REST endpoints
  standardFunctions.py   helper functions inherited from upstream
resources/forms/     form layout per class, generated by util/create_form_specs_for_api.py
jars/                protege.jar, Essential widgets, publish service
tests/               offline tests
docs/                documentation
demo/                upstream demo page (not updated for these changes)
```

## Known limits

- One process only. The loaded repositories, sessions and lockout counters are in memory.
- No write lock, no backup before save, no history.
- Scripts and the AZON MCP server cannot sign in; they work only with sign-in off.
- The container image still starts an SSH server with a fixed root password, inherited from upstream. Do not publish port 2222.
- It runs under Flask's development server.

Each of these has a step in the [plan](https://github.com/LotisOfficial/AzonAdmin/blob/main/docs/PLAN.md).
