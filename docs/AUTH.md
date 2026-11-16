# AZON Admin API: sign-in

Added 16 November 2026. Code: `essential_open_api/auth.py`, `essential_open_api/users_cli.py`. Tests: `tests/test_auth.py`.

## Turning it on and off

One setting, read once when the API starts. There is no runtime toggle and no URL that changes it.

| `AZON_AUTH_MODE` | Behaviour |
|---|---|
| `off` (or `anonymous`) | No sign-in. Every request has full access as `anonymous-full-access`. The page shows a gold **Sign-in off** chip. For development and demonstrations. |
| `auth` | Sign-in required for everything except the page itself, `/health` and the sign-in endpoints. |
| unset, empty or anything else | Treated as `auth`. |

`docker-compose.yml` passes `${AZON_AUTH_MODE:-off}`, so **the development stack starts with sign-in off**. To switch, put this in a `.env` file next to `docker-compose.yml` (already git-ignored) and run `docker compose up -d`:

```
AZON_AUTH_MODE=auth
AZON_TOTP_KEY=<output of the genkey command below>
```

Delete the first line, or set it to `off`, to go back.

With sign-in off, two protections stay on: the API no longer sends CORS headers, and a write that carries an `Origin` header from another site is refused. So a web page elsewhere can no longer change the repository through your browser. Anyone who can reach the port directly still has full access, which is why the compose file binds it to `127.0.0.1`.

This is a deliberate simplification of the Viewer design, which also requires `AZON_ANONYMOUS_ACKNOWLEDGE` before running without sign-in.

## Settings

| Variable | Default | Secret | Notes |
|---|---|---|---|
| `AZON_AUTH_MODE` | `auth` | no | See above. |
| `AZON_CONFIG_DIR` | required in `auth` | no | Folder holding `users.xml` and the audit log. Keep it outside the application folder. Compose mounts `./auth-config` (git-ignored) at `/azon-config`. |
| `AZON_TOTP_KEY` | required in `auth` | **yes** | Base64, 32 bytes. Encrypts authenticator seeds in `users.xml`. Start-up fails if it is missing, malformed or a placeholder. If it is lost, every user must re-enrol. |
| `AZON_TOTP_REQUIRED` | `true` | no | `false` lets accounts with no authenticator sign in with a password alone. |
| `AZON_SESSION_IDLE_MINUTES` | `15` | no | |
| `AZON_SESSION_ABSOLUTE_HOURS` | `8` | no | |
| `AZON_COOKIE_SECURE` | `true` | no | Leave on. Chrome, Edge and Firefox accept `Secure` cookies on `http://localhost`. |
| `AZON_ADMIN_STATIC_DIR` | unset | no | Folder containing the Admin page (`index.html`). When set, the API serves the page at `/`. |
| `AZON_ALLOWED_ORIGINS` | empty | no | Comma-separated extra origins allowed to send writes. Normally not needed. |
| `AZON_AUDIT_LOG` | `<AZON_CONFIG_DIR>/admin-audit.log` | no | One JSON line per sign-in event and per write. No audit log is written when neither this nor `AZON_CONFIG_DIR` is set. |

Start-up **fails** in `auth` mode if the config folder, `users.xml` or the key is missing or invalid.

## One origin

The page and the API must share an origin, so the session cookie is sent and the origin check passes. Two ways:

1. **The API serves the page** (compose default): open `http://localhost:5200/`.
2. **Nginx serves the page and proxies** `/api`, `/auth`, `/health`, `/apispec.json` and `/flasgger_static` to the API with `proxy_set_header Host $http_host;`. The Admin repository's `nginx.conf` and `nginx-dev.conf` now do this.

`flask-cors` has been removed.

## Managing users

Users live in `users.xml`. The API only reads it and picks up changes without a restart. There is no self-service registration or password reset.

In Docker (the image's entrypoint ignores arguments, so override it):

```bash
docker compose run --rm --entrypoint python api -m essential_open_api.users_cli genkey
```

```bash
docker compose run --rm --entrypoint python api -m essential_open_api.users_cli add alice --role editor --first-name Alice --last-name Smith
```

`add` asks for a password (12 characters minimum), creates an authenticator seed and prints the secret key and an `otpauth://` setup link **once**. Other commands: `passwd <user>`, `totp <user>` (re-enrol), `role <user> editor|reader`, `status <user> active|locked|disabled`, `list`.

Roles: `editor` can read and write. `reader` can read; the API refuses writes and the page hides the write buttons.

## How a request is checked

1. A write (`POST`, `PATCH`, `DELETE`) with an `Origin` header from another site is refused. Both modes.
2. `off` mode: allowed.
3. Public endpoints are allowed: `/`, the page's static files, `/health`, `/auth/login`, `/auth/totp`, `/auth/me`.
4. Anything else needs an active session, or the answer is `401`. This includes Swagger at `/api/`.
5. A write also needs the session's CSRF token in `X-CSRF-Token`, and the `editor` role.

Sign-in is two steps: `POST /auth/login` with username and password, then `POST /auth/totp` with the six-digit code. The session id is replaced after the second step. `GET /auth/me` returns the user, whether they can edit, and the CSRF token. `POST /auth/logout` ends the session.

Other rules, all following the Viewer design: Argon2id (64 MiB, 3 iterations, parallelism 1); unknown user, wrong password and disabled account give the same answer; 5 failures lock a username for 15 minutes, with a looser limit per client address; TOTP is RFC 6238 with one step of drift, and a used code is refused; cookie is `HttpOnly`, `Secure`, `SameSite=Lax`; passwords, seeds and codes are never logged.

## File formats (the contract for the Viewer build)

The Viewer's authentication layer is not built yet, so this module defines the shared formats. The Viewer build must match them, or both must change together.

```xml
<users version="1">
  <user>
    <username>alice</username>
    <eaUserName></eaUserName>            <!-- EA_User name in the repository; used by the Viewer, ignored here -->
    <passwordHash>$argon2id$v=19$m=65536,t=3,p=1$...</passwordHash>
    <totpSecret>base64(12-byte nonce || AES-256-GCM ciphertext || 16-byte tag)</totpSecret>
    <status>active</status>               <!-- active | locked | disabled -->
    <adminRole>editor</adminRole>         <!-- editor | reader; Admin only -->
    <tenantId>azon230</tenantId>
    <email></email><firstName></firstName><lastName></lastName>
    <mustChangePassword>false</mustChangePassword>
  </user>
</users>
```

- The TOTP seed is 20 random bytes, encrypted with no additional data.
- **Difference from the proposal:** edit rights come from `adminRole`, not from `EA_User` clearances in the repository. Neither the `Arthur` nor the `a230_clean` repository contains any `EA_User` instance, so clearance-based rights would have locked everyone out.
- An account with `mustChangePassword=true` cannot sign in to the Admin; `passwd` clears the flag.

## What was verified, and what was not

**Ran** (`python -m pytest tests/test_auth.py`, 56 passed, offline):

- Argon2id round trip and parameters; RFC 6238 test vectors; drift window; replay rejection; seed encryption and wrong-key failure.
- Start-up failures for missing or bad settings; mode parsing (only `off` / `anonymous` disables sign-in).
- `users.xml`: duplicates, bad values, broken XML and entity payloads rejected; atomic save with backup; live reload.
- Ten protected paths return `401` without a session; password alone gives no access; session id changes after the code; cookie flags; security headers; no CORS header.
- Same response for unknown user, wrong password and disabled account; lockout and its expiry; TOTP attempt limit and pending-session expiry.
- CSRF token required on writes; reader role refused on writes but able to sign out; cross-site `Origin` refused; logout invalidates and the old cookie cannot be replayed; idle and absolute timeouts; forged cookie ignored.
- Audit log contents, with no password or code in it.
- `off` mode: no sign-in, no `users.xml` needed, cross-site writes still refused.

**Ran in a browser** against this module in front of fake data (no JVM): sign-in panel, wrong password, code step, loading and saving as an editor, write refused without the CSRF token, cookie not readable by script, sign out, and the **Sign-in off** chip in `off` mode.

**Not verified:**

- The complete API (`create_app()` with the JVM and a real repository) was not started; Docker was not running. The wiring in `__init__.py` and the `POST /api/publish` change were syntax-checked only.
- The compose file and the Nginx proxy rules were not run.
- The reader role was checked by test, not in the browser.

## Known limits

- Single instance: sessions, lockout counters and used-code tracking are in memory. A restart signs everyone out.
- Behind a proxy the client address in the audit log and the per-address limit is the proxy's.
- **The AZON MCP server and any other script that calls this API will get `401` when sign-in is on.** They have no way to sign in. Adding service credentials is a design decision that has not been made.
- `POST /api/publish` now requires `url`, `user` and `pwd` in a JSON body. The old `GET` form and its built-in default credentials are gone. The publish target URL is still not restricted.
