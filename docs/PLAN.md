# AZON API + Azon Admin: secure rebuild and install guide

**Audience:** an implementing model (Claude Opus 5.5) working in Claude Code on this Windows 11 machine, and the human who reviews its work.
**Goal:** rebuild the missing pieces of the AZON API (a fork of `terryvel/essential-open-api`) and wire it to Azon Admin so the stack is secure whether it runs in Docker or natively.
**Basis:** security review of upstream commit `8512d0a` plus inspection of the local copies on 16 Nov 2026. Nothing in this guide has been built or run yet; every phase ends with tests that must pass before moving on.


> **Status (16 Nov 2026).** This is the original plan. Work has started in this repository: see
> [BUILD_LOG.md](BUILD_LOG.md) for what is built and tested so far.
> **Java decision:** the stack stays on **Java 8** for now. Wherever this plan says Java 17, follow
> [JAVA_OPTIONS.md](JAVA_OPTIONS.md) instead (Java 8 pinning, the migration path, and options for removing Java).
> The working copy for this plan is now this repository (`LotisOfficial/AzonAPI`), not the `essential-open-api` folder.

---

## 0. Rules for the implementing model

1. **Never push to `origin`.** In both local API folders `origin` is the upstream author's repository (`github.com/terryvel/essential-open-api`). Work on a local branch. Ask the human for a private remote before pushing anywhere.
2. **Never develop against a live repository.** Copy `C:\AzonDev1\repositories\Arthur` to a scratch folder and point the API at the copy. Every write endpoint saves to disk immediately.
3. **Never commit secrets or repository data.** Tokens, passwords, `.pprj`, `.pont`, `.pins`, certificates and `secrets/` folders stay out of git. Add ignore rules first.
4. **Do not delete or overwrite existing files outside the branch** (start scripts, Nginx config, MCP server) without showing the human the diff first.
5. **Fail closed.** If a token file, allowlist or repository root is missing or empty, the API must refuse to start. No built-in default credentials anywhere.
6. **Verify, do not assume.** Items marked **VERIFY** are facts this guide could not confirm. Check them and record the result in the PR description.
7. Finish each phase with its acceptance tests. Report failures with output; do not paper over them.

---

## 1. What exists today

| Piece | Location | State |
|---|---|---|
| AZON API (live copy) | `%USERPROFILE%\essential-open-api` | Upstream `8512d0a` plus **uncommitted** changes to `docker-compose.yml`, `essential_open_api/jvm.py`, `essential_open_api/routes.py`, `startup.sh`, and untracked `config/`. Contains the repository-switch feature. This is what `start-azon-api.bat` runs. |
| AZON API (older copy) | `%USERPROFILE%\azon-api` | Upstream plus `new/` drafts (`jvm.py`, `routes_addition.py`, `docker-compose.yml`) of the same repository-switch feature. Reference only. |
| Azon Admin | `%USERPROFILE%\azon-open-admin\public\index.html` | Single 3,778-line HTML file with inline CSS and JS. No external scripts. Served by Scoop Nginx on port 8082 from `%USERPROFILE%\scoop\apps\nginx\current\conf\nginx.conf`. |
| AZON MCP server | `%USERPROFILE%\azon-mcp\server.py` | Calls the API with `httpx`; base URL from `AZON_API_BASE` (default `http://localhost:5200/api`). Sends no credentials. |
| Viewer | Tomcat, `http://localhost:8080/A230Viewer` | Publish target. |
| Repositories | `C:\AzonDev1\repositories\<name>\<name>.pprj` (+ `.pont`, `.pins`) | Protégé projects. |
| Start/stop scripts | `C:\AzonDev1\start-azon*.bat`, `stop-azon*.bat`, `check-azon-mcp.bat` | Health-check with `GET /api/classes` on port 5200. |
| Tooling on this machine | Docker 29.9.2, Python 3.14.3, no `java` on PATH; a bundled JRE sits at `C:\AzonDev1\jre` | **VERIFY** the bundled JRE version with `C:\AzonDev1\jre\bin\java -version`. |

### Calls Azon Admin makes (the contract to preserve)

- `GET /api/classes/` and `GET /api/classes/<class>/`
- `GET /api/classes/<class>/form` and `/slots`
- `GET /api/classes/<class>/instances?slots=…&directinstances=true` (it sends a misspelt `maxdept=1`; harmless, the default is 1)
- `GET /api/instances/<id>`
- `POST /api/instances`, `POST|PATCH /api/instances/<id>`, `DELETE /api/instances/<id>`
- `GET /api/publish?url=…&user=…&pwd=…` and `GET /api/publish-status?id=…`
- `GET /api/repository`, `GET /api/repositories`, `POST /api/repository`

Admin hard-codes `const apiOrigin = "http://localhost:5200"` (line ~913) and calls the API cross-origin with no credentials.

### Problems to fix (from the security review)

| # | Problem | Fixed in |
|---|---|---|
| 1 | No authentication on any endpoint | Phase 2 |
| 2 | `/api/publish` sends the whole repository to a caller-chosen URL, via `GET`, password in query string, default `alice`/`s3cr3t` | Phase 4 |
| 3 | `POST /api/repository` loads any `.pprj` path the caller supplies, and responses expose absolute host paths | Phase 4 |
| 4 | Flask debug server, bound to `0.0.0.0`, source tree mounted into the container | Phases 5–7 |
| 5 | Root SSH with password `Docker!` in both the API and Admin images | Phase 6 |
| 6 | CORS `*` on `/api/*` | Phase 2 |
| 7 | No locking on the shared in-memory knowledge base | Phase 3 |
| 8 | Writes can set any slot, including system (`:`-prefixed) slots | Phase 3 |
| 9 | No size, batch, depth or page limits | Phase 3 |
| 10 | Exception text returned to clients; public Swagger UI | Phase 3 |
| 11 | Unpinned Python dependencies; unverified jars; `publish-service-1.0.0.jar` has no source | Phase 1 |
| 12 | Admin Docker entrypoint writes the Viewer password into `index.html` | Phase 8 |

---

## 2. Target design

```
Browser ──► Nginx (127.0.0.1:8082)
              ├─ /        static Azon Admin files
              └─ /api/    reverse proxy ──► AZON API (waitress, 127.0.0.1:5200 or Docker-internal)
                                              ├─ bearer-token auth, three roles
                                              ├─ one lock around the knowledge base
                                              └─ publish only to allowlisted Viewer URLs
MCP server ──► AZON API directly on 127.0.0.1:5200, with its own token
```

Decisions already made, with the reason for each:

- **Same origin.** Admin calls `/api/...` relative to its own origin. This removes CORS completely, so no other website can call the API from a browser.
- **Bearer tokens enforced inside the API**, not only at Nginx. The API is then safe even when reached directly (MCP, a mis-bound port).
- **No cookies or Basic auth.** The browser never sends credentials automatically, so cross-site request forgery does not apply.
- **Three roles:** `read` (all `GET`), `write` (instance create/update/delete), `admin` (publish, publish status, repository switch). Higher roles include lower ones.
- **`waitress` as the server in both modes.** It runs on Windows and Linux, is single-process and multi-threaded. The knowledge base lives in one JVM in one process, so there must be exactly one process. Do not use multi-worker gunicorn.
- **Two network profiles.** *Local* (default): everything binds to `127.0.0.1`, plain HTTP is acceptable. *Network*: any non-loopback access requires TLS at Nginx; the API port is still never exposed.
- **Ports stay 5200 (API) and 8082 (Admin)** so existing scripts and the MCP default keep working.

---

## 3. Phases

### Phase 0 — Baseline and safety net

1. In `%USERPROFILE%\essential-open-api`: run `git status` and `git diff` and read them. Create branch `azon-secure` and commit the existing uncommitted repository-switch work as the baseline commit, **excluding** `config/`, `repositories/`, `logs/`.
2. Add to `.gitignore`: `config/`, `repositories/`, `secrets/`, `*.pem`, `*.key`, `.env`.
3. Copy `C:\AzonDev1\repositories\Arthur` to a scratch folder (for example `%USERPROFILE%\azon-scratch\repositories\Arthur`). All development and tests use `REPO_ROOT` = that scratch `repositories` folder.
4. Record the SHA-256 of every file in `jars/` into `jars/SHA256SUMS` and commit it.

**Accept when:** branch exists, working tree clean, scratch repository in place, live repository untouched.

### Phase 1 — Dependencies and missing build inputs

1. **Python.** Replace `requirements.txt` with a fully pinned, hashed lock file (`pip-compile --generate-hashes` or `uv pip compile --generate-hashes`). Runtime packages: `Flask`, `jpype1`, `waitress`. Drop `Flask-Cors`. Make `flasgger` a dev-only extra.
2. **Python version.** Target 3.11 or 3.12 in Docker and in the native venv. **VERIFY** whether the pinned `jpype1` publishes a wheel for the machine's Python 3.14 before using it natively; if not, install 3.12 side by side.
3. **Java.** Use a headless Java 17 runtime. The upstream Dockerfile installs `default-jre` (Java 17 on Debian bookworm) but sets `JAVA_HOME` to a Java 11 path that does not exist; remove that line and let JPype locate the JVM.
4. **Jars.** Add a startup check that compares `jars/*.jar` against `jars/SHA256SUMS` and refuses to start on mismatch. **VERIFY** whether `httpclient`, `httpcore`, `httpmime` and `commons-codec` can move to the latest 4.5.x / 4.4.x / 1.x patch releases without breaking publish; upgrade if the publish test passes.
5. **`publish-service-1.0.0.jar` (no source in the repo).** It holds one class, `com.enterprise_architecture.essential_os.publish_service.PublishService`, and its `pom.xml` is inside the jar under `META-INF/maven/`. Observed interface:
   - `static String startPublishAsync(Project project, String url, String user, String pwd)` returns a job id
   - `static String getPublishStatus(String jobId)`
   - `static String getPublishLogs(String jobId)`
   - It renders the repository with `XMLRenderer` (from `essential-xml`) and sends it with `ReportServiceClient` (from `essential-widgets`) to `<url>/reportService`.

   Default: keep the jar, pinned by hash. Optional, only if the human asks: recreate the source from that interface and the embedded `pom.xml`, build with Maven, and compare behaviour against the original.

**Accept when:** `pip install --require-hashes -r requirements.txt` succeeds in a clean venv; the jar hash check passes and fails correctly when a jar is altered.

### Phase 2 — Authentication and CORS removal

1. Delete `CORS(...)` and its import from `essential_open_api/__init__.py`.
2. New module `essential_open_api/auth.py`:
   - Token file path from `AZON_API_TOKENS_FILE`. Format: a JSON list of `{"name": "admin-ui", "role": "admin", "sha256": "<hex digest of the token>"}`. Only hashes are stored.
   - Load once at startup. Raise and abort startup if the variable is unset, the file is missing, the list is empty, or a role is unknown.
   - Generate tokens with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Print each token once to the human; never write the plain token to disk or logs.
3. Enforce on the blueprint:

   ```python
   ROLE_RANK = {"read": 1, "write": 2, "admin": 3}
   ADMIN_ENDPOINTS = {"api.publish", "api.publish_status", "api.switch_repository"}

   def required_role() -> str:
       if request.endpoint in ADMIN_ENDPOINTS:
           return "admin"
       return "read" if request.method in ("GET", "HEAD") else "write"

   @api_bp.before_request
   def authenticate():
       header = request.headers.get("Authorization", "")
       if not header.startswith("Bearer "):
           return jsonify({"error": "Authentication required."}), 401
       digest = hashlib.sha256(header[7:].strip().encode()).hexdigest()
       match = None
       for entry in TOKENS:                      # no early exit
           if hmac.compare_digest(entry["sha256"], digest):
               match = entry
       if match is None:
           return jsonify({"error": "Authentication required."}), 401
       if ROLE_RANK[match["role"]] < ROLE_RANK[required_role()]:
           return jsonify({"error": "Forbidden."}), 403
       g.principal = match["name"]
   ```

   Confirm the endpoint names against the actual view function names (`publish`, `publish_status`, `switch_repository`).
4. Every non-`GET` request must carry `Content-Type: application/json`; otherwise return 415. Remove the lenient fallback in `load_json_body()` that parses bodies without the header.
5. `/health` stays unauthenticated and returns only `{"status": "ok", "kb_loaded": bool}`.
6. Register Swagger only when `AZON_API_DOCS=1`; default off.

**Accept when** (with tokens `R`, `W`, `A`):

| Request | Expected |
|---|---|
| `GET /health`, no token | 200 |
| `GET /api/classes/`, no token | 401 |
| `GET /api/classes/`, bad token | 401 |
| `GET /api/classes/`, `R` | 200 |
| `POST /api/instances`, `R` | 403 |
| `POST /api/instances`, `W`, `text/plain` body | 415 |
| `POST /api/repository`, `W` | 403 |
| `OPTIONS /api/instances` with an `Origin` header | no `Access-Control-Allow-Origin` in the response |
| `GET /api/` (Swagger) with docs off | 404 |
| Start with `AZON_API_TOKENS_FILE` unset | process exits non-zero |

### Phase 3 — Knowledge-base safety, limits, errors

1. **One lock.** Add `KB_LOCK = threading.RLock()` in `jvm.py`. Acquire it in the blueprint's `before_request` after authentication succeeds and set `g.kb_locked = True`; release it in `teardown_request` when that flag is set. `reload_pprj` and `save_project` run under the same lock.
2. **Writes pause during publish.** Track the active publish job id. While its status is running, return 409 to write and repository-switch requests.
3. **Slot rules on create and update:** reject any slot name starting with `:`; reject slots that are not template slots of the instance's class (**VERIFY** the Protégé call, likely `cls.hasTemplateSlot(slot)`).
4. **Limits**, each configurable by environment variable with these defaults:

   | Limit | Default | Response when exceeded |
   |---|---|---|
   | `MAX_CONTENT_LENGTH` | 1 MB | 413 |
   | Batch size (`/instances/batch`) | 200 | 400 |
   | Nested-object depth in a payload | 3 | 400 |
   | `maxdepth` query parameter | 5, even when `slots` is given | 400 |
   | `count` | default 500, maximum 2,000 | clamp |

5. **Errors.** Replace every response that interpolates an exception (`f"...{exc}"`) with a generic message plus a request id. Log the full exception server-side with the same id.
6. **Audit log.** One line per write, delete, publish and repository switch: timestamp, request id, token name (`g.principal`), action, instance id or class. Never log tokens, passwords or request bodies.
7. **Backups.** Before the first save after startup, and before every repository switch, copy the active `.pprj`, `.pont` and `.pins` to `<repo>/backups/<timestamp>/`. Keep the newest 20.
8. Document in the README: do not open the same repository in Protégé desktop while the API is running; both save whole files and the last writer wins.

**Accept when:** unit tests cover each limit and slot rule; a test firing 20 concurrent writes leaves a repository that reloads cleanly with all 20 instances; no response body contains `Traceback`, `java.`, or a filesystem path.

### Phase 4 — Publish and repository switching

**Publish**

1. `POST /api/publish` only; `GET` returns 405. JSON body: `{"target": "<name>"}`.
2. Targets come from `AZON_VIEWER_TARGETS_FILE`, a JSON object such as `{"local": {"url": "http://localhost:8080/A230Viewer", "user": "publisher", "password_file": "/run/secrets/viewer_pwd"}}`. The request never supplies a URL.
3. Viewer credentials are read from the target's secret file. If a target has no `password_file`, accept `user` and `pwd` in the JSON body for that call only. Never accept them in the query string, and remove the `alice` / `s3cr3t` / `host.docker.internal:9090` defaults.
4. Add `GET /api/publish-targets` (admin role) returning target names only, so Admin can show a dropdown.
5. `GET /api/publish-status?id=` stays, admin role. Strip anything resembling credentials from returned logs.

**Repository switching**

`list_repositories()` in `jvm.py` scans `REPO_ROOT` one level deep: `.pprj` files directly in the root, and every `.pprj` inside each subfolder. A folder can hold more than one, so the folder name is not a unique key.

1. Give each entry an `id`: its path relative to `REPO_ROOT` with forward slashes, for example `Arthur/Arthur.pprj`. `GET /api/repositories` returns `[{ "id", "name", "file", "active" }]` and `GET /api/repository` returns the same shape for the active one. Remove the absolute `path` field from both.
2. `POST /api/repository` takes `{"id": "<id from the list>"}`. Today Admin posts `{"path": <absolute path>}` and `reload_pprj()` loads whatever it is given; remove that. Accept the request only if the `id` exactly equals an `id` in a fresh `list_repositories()` result, then additionally confirm containment and reject anything else with 400:

   ```python
   root = REPO_ROOT.resolve()
   candidate = (root / repo_id).resolve()
   if not candidate.is_relative_to(root) or candidate.suffix.lower() != ".pprj" or not candidate.is_file():
       return error 400
   ```
3. `config/active.json` stores the repository **`id`**, not an absolute path. Ignore and log a stored value that fails the same validation.

**Accept when:** publish to an unlisted target returns 400; `GET /api/publish?...` returns 405; a publish to the local Viewer completes and the Viewer shows the snapshot; `POST /api/repository` with `..`, an absolute path, or an unknown `id` returns 400; switching between two scratch repositories works and survives a restart.

### Phase 5 — Production entry point (shared by both modes)

1. Delete `startup.sh` and `sshd_config`. Remove every `FLASK_DEBUG`, `flask run` and `--debug` reference.
2. Start command, both modes:

   ```
   waitress-serve --listen=127.0.0.1:5200 --threads=8 --ident=azon-api wsgi:app
   ```
   In Docker use `--listen=0.0.0.0:5200`; the port is not published to the host (Phase 6).
3. Configuration is environment-only. Document every variable in `README.md` and ship `.env.example` with placeholder values only:
   `PPRJ_FILE`, `PPRJ_DIR`, `REPO_ROOT`, `CONFIG_FILE`, `JARS_DIR`, `AZON_API_TOKENS_FILE`, `AZON_VIEWER_TARGETS_FILE`, `AZON_API_DOCS`, and the limit variables.

### Phase 6 — Docker install

Create a deployment folder outside both repositories, for example `%USERPROFILE%\azon-stack\`, holding `docker-compose.yml`, `nginx/azon.conf`, and `secrets/` (git-ignored if the folder is ever versioned).

**API `Dockerfile`** (in the API repo):

```dockerfile
FROM python:3.11-slim-bookworm            # pin by digest: python:3.11-slim-bookworm@sha256:...
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends openjdk-17-jre-headless \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --create-home azon
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY essential_open_api/ essential_open_api/
COPY jars/ jars/
COPY resources/forms/ resources/forms/
COPY wsgi.py .
USER 10001
EXPOSE 5200
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5200/health')"
CMD ["waitress-serve", "--listen=0.0.0.0:5200", "--threads=8", "wsgi:app"]
```

Add a `.dockerignore` excluding `.git`, `config/`, `repositories/`, `logs/`, `demo/`, `new/`, `secrets/`, `__pycache__/`.

**`docker-compose.yml`:**

```yaml
services:
  api:
    build: ../essential-open-api
    restart: unless-stopped
    # no "ports:" — reachable only from the web container
    environment:
      REPO_ROOT: /data/repositories
      PPRJ_FILE: /data/repositories/Arthur/Arthur.pprj
      CONFIG_FILE: /data/config/active.json
      AZON_API_TOKENS_FILE: /run/secrets/api_tokens
      AZON_VIEWER_TARGETS_FILE: /run/secrets/viewer_targets
    volumes:
      - C:\AzonDev1\repositories:/data/repositories
      - api-config:/data/config
    secrets: [api_tokens, viewer_targets, viewer_pwd]
    extra_hosts: ["host.docker.internal:host-gateway"]   # publish to Tomcat on the host
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]

  web:
    image: nginxinc/nginx-unprivileged:stable-alpine      # pin by digest
    restart: unless-stopped
    depends_on: [api]
    ports: ["127.0.0.1:8082:8080"]                        # loopback only
    volumes:
      - ../azon-open-admin/public:/usr/share/nginx/html:ro
      - ./nginx/azon.conf:/etc/nginx/conf.d/default.conf:ro
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]

secrets:
  api_tokens:     { file: ./secrets/api_tokens.json }
  viewer_targets: { file: ./secrets/viewer_targets.json }
  viewer_pwd:     { file: ./secrets/viewer_pwd.txt }

volumes:
  api-config:
```

Notes for the implementer:

- In Docker the Viewer target URL is `http://host.docker.internal:8080/A230Viewer`.
- The source tree is **not** mounted into the container.
- **VERIFY** that Protégé starts with a read-only root filesystem. If it needs to write beside the working directory, add a `tmpfs` for that path instead of dropping `read_only`.
- **VERIFY** that uid 10001 can write to the repositories bind mount on Docker Desktop for Windows.
- The MCP server runs on the host and needs the API. Either point it at `http://localhost:8082/api` (through Nginx), or add `ports: ["127.0.0.1:5200:5200"]` to `api`. Prefer the first.
- Retire the Admin repo's own `Dockerfile`, `entrypoint.sh` and `sshd_config`: they enable root SSH with a known password and write the Viewer password into the HTML.

**Accept when:** `docker compose up -d --build` is healthy; `docker compose exec api id -u` prints `10001`; from the host, port 5200 refuses connections and `http://127.0.0.1:8082/health` returns 200; from another machine on the LAN, port 8082 is unreachable; `docker compose exec api sh -c "ls /usr/sbin/sshd"` fails.

### Phase 7 — Native Windows install

1. Create a venv with the chosen Python: `py -3.12 -m venv %USERPROFILE%\essential-open-api\.venv`, then install with `--require-hashes`.
2. Java: set `JAVA_HOME` for the API process only (in the start script, not system-wide) to a Java 17 runtime, or to `C:\AzonDev1\jre` if Phase 0 verification shows it is new enough and the API loads the repository with it.
3. Secrets: put `api_tokens.json`, `viewer_targets.json` and `viewer_pwd.txt` in `%USERPROFILE%\azon-stack\secrets\`. Restrict the folder to the current user: `icacls <folder> /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"`.
4. Rewrite `C:\AzonDev1\start-azon-api.bat` to offer both modes. Native mode sets the environment variables and runs `waitress-serve --listen=127.0.0.1:5200 ...`. Change every health probe in `start-azon-api.bat`, `start-azon-admin.bat` and `check-azon-mcp.bat` from `/api/classes` to `/health`. Show the human the diffs before saving.
5. Nginx (Scoop): replace the `server` block in `conf\nginx.conf` with the shared config below, using `listen 127.0.0.1:8082;`, `root C:/Users/<you>/azon-open-admin/public;` and `proxy_pass http://127.0.0.1:5200;`. Run `nginx -t` before reloading.

**Shared Nginx server block** (`nginx/azon.conf` for Docker: `listen 8080;`, `root /usr/share/nginx/html;`, `proxy_pass http://api:5200;`):

```nginx
server {
    listen 127.0.0.1:8082;
    server_name localhost 127.0.0.1;
    server_tokens off;
    client_max_body_size 1m;
    root C:/Users/<you>/azon-open-admin/public;

    # add_header in a location replaces these, so repeat them there if you add any.
    add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;

    location /api/ {
        proxy_pass http://127.0.0.1:5200;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 120s;
    }
    location = /health { proxy_pass http://127.0.0.1:5200; }
    location / { index index.html; try_files $uri $uri/ =404; }
}
```

**Network profile (only if the human asks for access from other machines):** change Nginx to `listen 443 ssl;` with a certificate from the organisation's CA or `mkcert`, add `Strict-Transport-Security`, redirect port 80 to 443, add a Windows Firewall rule limited to the required source addresses, and keep the API on loopback or the Docker-internal network.

**Accept when:** `netstat -ano | findstr ":5200 :8082"` shows only `127.0.0.1` listeners; the Phase 2 table passes through `http://127.0.0.1:8082`; response headers include the three security headers.

### Phase 8 — Azon Admin changes (`azon-open-admin`)

1. **Same origin.** Replace `const apiOrigin = "http://localhost:5200"` with `const apiOrigin = ""` so every call is relative (`/api/...`).
2. **Externalise inline code** so the CSP above can forbid inline scripts and styles: move the `<script>` body to `public/app.js` and the `<style>` body to `public/app.css`; replace inline `style="..."` attributes and inline event handlers with classes and `addEventListener`. If this is deferred, the CSP must temporarily allow `'unsafe-inline'` for the affected directive; record that as an open risk.
3. **Token sign-in.** On load, show a small form with `<input type="password" autocomplete="current-password">` asking for the API token. Keep the token in a module-level variable only; do not write it to `localStorage`, cookies or the URL. Validate it with one authenticated `GET /api/repository`.
4. **One fetch wrapper.** Route all 15 `fetch(...)` calls through a helper that adds `Authorization: Bearer <token>`, sets `Content-Type: application/json` on bodies, and on 401 clears the token and shows the sign-in form again. On 403 show "your token does not allow this action".
5. **Publish dialog.** Replace the URL, user and password inputs with a target dropdown filled from `GET /api/publish-targets`; submit with `POST /api/publish` and a JSON body. Keep user and password inputs only for targets the API reports as needing them, and send them in the body.
6. **Repository switcher.** Post `{"id": ...}` (it posts `{path: selectedPath}` today, line ~3728) and display names, not paths.
7. **Output escaping.** Audit every `innerHTML` use (32 occurrences). Any that includes API data (instance names, class names, slot values, error messages) must use `textContent` or DOM construction instead.
8. Remove the `__API_ORIGIN__`, `__VIEWER_URL__`, `__VIEWER_USER__`, `__VIEWER_PWD__` placeholder mechanism.

**Accept when:** with the browser console open at `http://127.0.0.1:8082`, there are no CSP violations; browse, create, edit, delete, publish and repository switch all work with an admin token; a read token can browse but gets a clear message on save; an instance named `<img src=x onerror=alert(1)>` renders as text everywhere; no request URL in the Network tab contains a password or token.

### Phase 9 — MCP server (`azon-mcp`)

1. In `server.py`, `get_http_client()` (line ~145): read `AZON_API_TOKEN` from the environment (the server already loads `.env`) and pass `headers={"Authorization": f"Bearer {token}"}` to `httpx.AsyncClient`. Fail at startup with a clear message if it is unset.
2. Issue the MCP its own token with the lowest role that covers its tools: `write` if it creates, updates and deletes instances; `admin` only if its publish tool must stay enabled.
3. Change the publish tool to `POST /api/publish` with a `target` name; remove its `viewer_url` argument.
4. Set `AZON_API_BASE` to match the chosen mode (`http://localhost:8082/api` for Docker, `http://localhost:5200/api` for native).

**Accept when:** MCP read, create, update and delete tools work against the scratch repository; with the token removed the MCP server refuses to start.

### Phase 10 — Final verification and handover

1. Run the full Phase 2 table and every phase's acceptance checks once in Docker mode and once in native mode. Record both results.
2. Run a dependency audit (`pip-audit` against the lock file) and an image scan (`docker scout cves` or `trivy image`). Fix or justify every high or critical finding.
3. `git grep -nE "s3cr3t|Docker!|alice|FLASK_DEBUG|flask run|0\.0\.0\.0"` in the API and Admin repos returns nothing except the Docker `waitress` listen address.
4. Switch `REPO_ROOT` from the scratch copy to `C:\AzonDev1\repositories` only after the human approves.
5. Hand over: the list of **VERIFY** results, any deferred items (for example CSP `'unsafe-inline'`), where secrets live, and how to rotate a token (generate, replace the hash in `api_tokens.json`, restart the API).

---

## 4. Definition of done

- No endpoint except `/health` answers without a valid token, in either mode.
- The API port is not reachable from outside the machine; the Admin port is loopback-only unless the network profile was explicitly requested, in which case it is TLS-only.
- No default credentials, no SSH server, no debug mode, no root process in any container.
- Publish can only send data to allowlisted Viewer targets, and repository switching can only load projects under `REPO_ROOT`.
- Concurrent writes do not corrupt a repository; backups exist before the first write.
- Azon Admin and the MCP server both work end to end with their own tokens.
- All acceptance tests pass in both modes and the results are recorded.
