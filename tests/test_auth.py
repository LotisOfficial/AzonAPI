"""Offline tests for essential_open_api.auth. No JVM, no network.

Run from the repository root:  python -m pytest tests/test_auth.py -q
"""

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from flask import Flask, jsonify

# Load auth.py directly so the tests do not import the package __init__ (which starts the JVM).
_PATH = Path(__file__).resolve().parent.parent / "essential_open_api" / "auth.py"
_spec = importlib.util.spec_from_file_location("azon_auth_under_test", _PATH)
auth_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = auth_mod
_spec.loader.exec_module(auth_mod)

KEY = bytes(range(32))
KEY_B64 = base64.b64encode(KEY).decode()
SEED = b"12345678901234567890"  # RFC 6238 SHA-1 test seed
PASSWORD = "correct horse battery"
T0 = 1_700_000_000.0


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def make_users(tmp_path, **overrides):
    users = {
        "alice": auth_mod.User(username="Alice", password_hash=auth_mod.hash_password(PASSWORD), role="editor",
                               totp_secret=auth_mod.encrypt_seed(KEY, SEED), first_name="Alice", last_name="Test"),
        "rita": auth_mod.User(username="rita", password_hash=auth_mod.hash_password(PASSWORD), role="reader",
                              totp_secret=auth_mod.encrypt_seed(KEY, SEED)),
        "nototp": auth_mod.User(username="nototp", password_hash=auth_mod.hash_password(PASSWORD), role="editor"),
        "off": auth_mod.User(username="off", password_hash=auth_mod.hash_password(PASSWORD), status="disabled",
                             totp_secret=auth_mod.encrypt_seed(KEY, SEED)),
        "mustchange": auth_mod.User(username="mustchange", password_hash=auth_mod.hash_password(PASSWORD),
                                    must_change_password=True),
    }
    users.update(overrides)
    auth_mod.save_users(tmp_path / "users.xml", users)
    return users


def env_for(tmp_path, **extra):
    env = {"AZON_AUTH_MODE": "auth", "AZON_CONFIG_DIR": str(tmp_path), "AZON_TOTP_KEY": KEY_B64}
    env.update(extra)
    return env


def make_app(tmp_path, clock=None, **extra):
    clock = clock or Clock()
    app = Flask(__name__)
    auth = auth_mod.init_auth(app, auth_mod.load_settings(env_for(tmp_path, **extra)), clock)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/classes/")
    def classes():
        return jsonify([])

    @app.route("/api/instances", methods=["POST"])
    @app.route("/api/instances/<iid>", methods=["PATCH", "DELETE"])
    def write(iid=None):
        return jsonify({"ok": True})

    return app, auth, clock


def code_at(clock, offset_steps=0):
    return auth_mod.totp_code(SEED, int(clock.now // 30) + offset_steps)


def sign_in(client, clock, username="alice"):
    assert client.post("/auth/login", json={"username": username, "password": PASSWORD}).status_code == 200
    assert client.post("/auth/totp", json={"code": code_at(clock)}).status_code == 200
    return client.get("/auth/me").get_json()


@pytest.fixture
def ctx(tmp_path):
    make_users(tmp_path)
    app, auth, clock = make_app(tmp_path)
    return app, auth, clock, tmp_path


# ---------------------------------------------------------------- passwords and TOTP primitives


def test_argon2id_round_trip_and_parameters():
    encoded = auth_mod.hash_password(PASSWORD)
    assert encoded.startswith("$argon2id$v=19$m=65536,t=3,p=1$")
    assert auth_mod.verify_password(encoded, PASSWORD)
    assert not auth_mod.verify_password(encoded, PASSWORD + "x")
    assert not auth_mod.verify_password("not-a-hash", PASSWORD)


@pytest.mark.parametrize("when,expected", [
    (59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
    (1234567890, "005924"), (2000000000, "279037"), (20000000000, "353130"),
])
def test_totp_rfc6238_vectors(when, expected):
    assert auth_mod.totp_code(SEED, when // 30) == expected


def test_totp_drift_window_and_replay():
    now = 1111111109
    step = now // 30
    assert auth_mod.match_totp(SEED, auth_mod.totp_code(SEED, step - 1), now) == step - 1
    assert auth_mod.match_totp(SEED, auth_mod.totp_code(SEED, step + 1), now) == step + 1
    assert auth_mod.match_totp(SEED, auth_mod.totp_code(SEED, step + 2), now) is None
    assert auth_mod.match_totp(SEED, auth_mod.totp_code(SEED, step), now, last_counter=step) is None
    assert auth_mod.match_totp(SEED, "12345", now) is None
    assert auth_mod.match_totp(SEED, "abcdef", now) is None


def test_seed_encryption_round_trip_and_wrong_key():
    stored = auth_mod.encrypt_seed(KEY, SEED)
    assert SEED not in base64.b64decode(stored)
    assert auth_mod.decrypt_seed(KEY, stored) == SEED
    assert auth_mod.decrypt_seed(bytes(32 * [7]), stored) is None
    assert auth_mod.decrypt_seed(KEY, "garbage") is None


# ---------------------------------------------------------------- configuration: fail closed


def test_mode_defaults_to_auth_and_only_off_disables(tmp_path):
    make_users(tmp_path)
    for value in (None, "", "AUTH", "nonsense", "true", "0"):
        env = env_for(tmp_path)
        if value is None:
            del env["AZON_AUTH_MODE"]
        else:
            env["AZON_AUTH_MODE"] = value
        assert auth_mod.load_settings(env).mode == "auth"
    for value in ("off", "OFF", " anonymous "):
        assert auth_mod.load_settings({"AZON_AUTH_MODE": value}).mode == "off"


@pytest.mark.parametrize("change,fragment", [
    ({"AZON_CONFIG_DIR": ""}, "AZON_CONFIG_DIR is required"),
    ({"AZON_TOTP_KEY": ""}, "AZON_TOTP_KEY is required"),
    ({"AZON_TOTP_KEY": "changeme"}, "AZON_TOTP_KEY is required"),
    ({"AZON_TOTP_KEY": "!!!"}, "not valid base64"),
    ({"AZON_TOTP_KEY": base64.b64encode(b"short").decode()}, "32 bytes"),
    ({"AZON_TOTP_KEY": base64.b64encode(bytes(32)).decode()}, "placeholder"),
    ({"AZON_SESSION_IDLE_MINUTES": "abc"}, "whole number"),
])
def test_startup_fails_on_bad_configuration(tmp_path, change, fragment):
    make_users(tmp_path)
    with pytest.raises(auth_mod.AuthConfigError, match=fragment):
        auth_mod.load_settings(env_for(tmp_path, **change))


def test_startup_fails_without_users_file(tmp_path):
    with pytest.raises(auth_mod.AuthConfigError, match="users.xml not found"):
        auth_mod.load_settings(env_for(tmp_path))


# ---------------------------------------------------------------- users.xml


def test_users_file_round_trip_and_case_insensitive_lookup(tmp_path):
    make_users(tmp_path)
    store = auth_mod.UserStore(tmp_path / "users.xml")
    assert store.get("ALICE").username == "Alice"
    assert store.get("alice").role == "editor"
    assert store.get("nobody") is None


def test_duplicate_username_and_bad_values_rejected(tmp_path):
    path = tmp_path / "users.xml"
    good_hash = auth_mod.hash_password(PASSWORD)

    def write(body):
        path.write_text(f"<users>{body}</users>", encoding="utf-8")

    write(f"<user><username>a</username><passwordHash>{good_hash}</passwordHash></user>"
          f"<user><username>A</username><passwordHash>{good_hash}</passwordHash></user>")
    with pytest.raises(auth_mod.AuthConfigError, match="duplicate"):
        auth_mod.load_users(path)
    write(f"<user><username>a</username><passwordHash>{good_hash}</passwordHash><status>weird</status></user>")
    with pytest.raises(auth_mod.AuthConfigError, match="unknown status"):
        auth_mod.load_users(path)
    write(f"<user><username>a</username><passwordHash>{good_hash}</passwordHash><adminRole>god</adminRole></user>")
    with pytest.raises(auth_mod.AuthConfigError, match="unknown adminRole"):
        auth_mod.load_users(path)
    write("<user><username>a</username><passwordHash>plaintext</passwordHash></user>")
    with pytest.raises(auth_mod.AuthConfigError, match="Argon2id"):
        auth_mod.load_users(path)
    path.write_text("<users><user>", encoding="utf-8")
    with pytest.raises(auth_mod.AuthConfigError, match="not readable XML"):
        auth_mod.load_users(path)


def test_xml_entity_payload_rejected(tmp_path):
    path = tmp_path / "users.xml"
    path.write_text('<?xml version="1.0"?><!DOCTYPE users [<!ENTITY x SYSTEM "file:///etc/passwd">]><users>&x;</users>', encoding="utf-8")
    with pytest.raises(auth_mod.AuthConfigError):
        auth_mod.load_users(path)


def test_save_is_atomic_and_keeps_backup(tmp_path, monkeypatch):
    users = make_users(tmp_path)
    path = tmp_path / "users.xml"
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(auth_mod.os, "replace", boom)
    users["alice"].role = "reader"
    with pytest.raises(OSError):
        auth_mod.save_users(path, users)
    assert path.read_bytes() == before
    monkeypatch.undo()
    auth_mod.save_users(path, users)
    assert (tmp_path / "users.xml.bak").read_bytes() == before
    assert auth_mod.load_users(path)["alice"].role == "reader"


def test_user_store_picks_up_changes_and_survives_bad_edit(tmp_path):
    users = make_users(tmp_path)
    path = tmp_path / "users.xml"
    store = auth_mod.UserStore(path)
    users["alice"].status = "disabled"
    auth_mod.save_users(path, users)
    assert store.get("alice").status == "disabled"
    path.write_text("<broken", encoding="utf-8")
    assert store.get("alice").status == "disabled"  # previous good copy kept


# ---------------------------------------------------------------- enforcement


PROTECTED = ["/api/classes/", "/api/instances/x", "/api/", "/apispec.json", "/api/repository", "/api/repositories",
             "/api/publish-status", "/flasgger_static/x.js", "/api/classes/Foo/instances", "/nope"]


@pytest.mark.parametrize("path", PROTECTED)
def test_everything_but_the_public_list_needs_a_session(ctx, path):
    app = ctx[0]
    response = app.test_client().get(path)
    assert response.status_code == 401
    assert response.get_json()["error"] == "not_authenticated"


def test_public_endpoints_and_headers(ctx):
    client = ctx[0].test_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "Strict-Transport-Security" in response.headers
    assert "Access-Control-Allow-Origin" not in response.headers
    assert client.get("/auth/me").status_code == 401


def test_full_login_flow_cookie_flags_and_session_rotation(ctx):
    app, auth, clock, _ = ctx
    client = app.test_client()
    first = client.post("/auth/login", json={"username": "ALICE", "password": PASSWORD})
    assert first.get_json() == {"totpRequired": True}
    cookie = first.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie and "Path=/" in cookie
    pending_sid = client.get_cookie(auth_mod.COOKIE_NAME).value
    assert client.get("/api/classes/").status_code == 401  # password alone is not enough

    assert client.post("/auth/totp", json={"code": code_at(clock)}).status_code == 200
    active_sid = client.get_cookie(auth_mod.COOKIE_NAME).value
    assert active_sid != pending_sid
    me = client.get("/auth/me").get_json()
    assert me["user"] == "Alice" and me["fullName"] == "Alice Test" and me["canEdit"] is True and me["mode"] == "auth"
    assert client.get("/api/classes/").status_code == 200
    assert client.get("/api/classes/").headers["Cache-Control"] == "no-store"


def test_unknown_user_and_wrong_password_look_the_same(ctx):
    client = ctx[0].test_client()
    a = client.post("/auth/login", json={"username": "ghost", "password": PASSWORD})
    b = client.post("/auth/login", json={"username": "alice", "password": "wrong"})
    c = client.post("/auth/login", json={"username": "off", "password": PASSWORD})  # disabled account
    assert a.status_code == b.status_code == c.status_code == 401
    assert a.get_json() == b.get_json() == c.get_json()
    assert "Set-Cookie" not in a.headers and "Set-Cookie" not in b.headers


@pytest.mark.parametrize("body", [{}, {"username": "alice"}, {"username": 5, "password": PASSWORD},
                                  {"username": "alice", "password": ["x"]}, {"username": " ", "password": "x"}])
def test_malformed_login_is_rejected(ctx, body):
    assert ctx[0].test_client().post("/auth/login", json=body).status_code == 401


def test_lockout_after_five_failures_and_expiry(ctx):
    app, auth, clock, _ = ctx
    client = app.test_client()
    for _ in range(5):
        assert client.post("/auth/login", json={"username": "alice", "password": "wrong"}).status_code == 401
    locked = client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    assert locked.status_code == 429
    clock.now += 14 * 60
    assert client.post("/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 429
    clock.now += 61
    assert client.post("/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200


def test_totp_wrong_code_replay_and_attempt_limit(ctx):
    app, auth, clock, _ = ctx
    client = app.test_client()
    client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    assert client.post("/auth/totp", json={"code": "000000"}).status_code == 401
    good = code_at(clock)
    assert client.post("/auth/totp", json={"code": good}).status_code == 200

    other = app.test_client()  # same code again in the same window, from another browser
    other.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    assert other.post("/auth/totp", json={"code": good}).status_code == 401
    assert other.get("/api/classes/").status_code == 401

    third = app.test_client()
    clock.now += 16 * 60
    third.post("/auth/login", json={"username": "rita", "password": PASSWORD})
    for _ in range(5):
        assert third.post("/auth/totp", json={"code": "111111"}).status_code == 401
    assert third.post("/auth/totp", json={"code": code_at(clock)}).status_code == 401  # pending session destroyed


def test_totp_without_password_step_is_refused(ctx):
    app, _, clock, _ = ctx
    assert app.test_client().post("/auth/totp", json={"code": code_at(clock)}).status_code == 401


def test_totp_pending_session_expires(ctx):
    app, _, clock, _ = ctx
    client = app.test_client()
    client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    clock.now += 5 * 60 + 1
    assert client.post("/auth/totp", json={"code": code_at(clock)}).status_code == 401


def test_account_without_totp_denied_unless_relaxed(tmp_path):
    make_users(tmp_path)
    app, _, _ = make_app(tmp_path)
    response = app.test_client().post("/auth/login", json={"username": "nototp", "password": PASSWORD})
    assert response.status_code == 403 and response.get_json()["error"] == "totp_not_enrolled"

    relaxed, _, _ = make_app(tmp_path, AZON_TOTP_REQUIRED="false")
    client = relaxed.test_client()
    assert client.post("/auth/login", json={"username": "nototp", "password": PASSWORD}).get_json() == {"totpRequired": False}
    assert client.get("/api/classes/").status_code == 200


def test_must_change_password_denied(ctx):
    response = ctx[0].test_client().post("/auth/login", json={"username": "mustchange", "password": PASSWORD})
    assert response.status_code == 403 and response.get_json()["error"] == "password_change_required"


def test_writes_need_csrf_token_and_editor_role(ctx):
    app, _, clock, _ = ctx
    client = app.test_client()
    me = sign_in(client, clock)
    assert client.post("/api/instances", json={"name": "x"}).status_code == 403
    assert client.post("/api/instances", json={"name": "x"}, headers={"X-CSRF-Token": "wrong"}).get_json()["error"] == "csrf"
    headers = {"X-CSRF-Token": me["csrfToken"]}
    assert client.post("/api/instances", json={"name": "x"}, headers=headers).status_code == 200
    assert client.patch("/api/instances/a", json={}, headers=headers).status_code == 200
    assert client.delete("/api/instances/a", headers=headers).status_code == 200

    clock.now += 16 * 60
    reader = app.test_client()
    me = sign_in(reader, clock, "rita")
    assert me["canEdit"] is False
    headers = {"X-CSRF-Token": me["csrfToken"]}
    assert reader.get("/api/classes/").status_code == 200
    for call in (reader.post("/api/instances", json={}, headers=headers),
                 reader.patch("/api/instances/a", json={}, headers=headers),
                 reader.delete("/api/instances/a", headers=headers)):
        assert call.status_code == 403 and call.get_json()["error"] == "read_only"
    assert reader.post("/auth/logout", headers=headers).status_code == 204  # readers can still sign out


def test_cross_site_write_is_refused_even_with_token(ctx):
    app, _, clock, _ = ctx
    client = app.test_client()
    me = sign_in(client, clock)
    headers = {"X-CSRF-Token": me["csrfToken"], "Origin": "https://evil.example"}
    response = client.post("/api/instances", json={}, headers=headers)
    assert response.status_code == 403 and response.get_json()["error"] == "bad_origin"
    headers["Origin"] = "http://localhost"
    assert client.post("/api/instances", json={}, headers=headers).status_code == 200


def test_logout_invalidates_session(ctx):
    app, _, clock, _ = ctx
    client = app.test_client()
    me = sign_in(client, clock)
    sid = client.get_cookie(auth_mod.COOKIE_NAME).value
    assert client.post("/auth/logout").status_code == 403  # needs the CSRF token
    assert client.post("/auth/logout", headers={"X-CSRF-Token": me["csrfToken"]}).status_code == 204
    assert client.get("/api/classes/").status_code == 401
    client.set_cookie(auth_mod.COOKIE_NAME, sid)  # replaying the old cookie does not revive it
    assert client.get("/api/classes/").status_code == 401


def test_idle_and_absolute_timeouts(ctx):
    app, _, clock, _ = ctx
    client = app.test_client()
    sign_in(client, clock)
    clock.now += 14 * 60
    assert client.get("/api/classes/").status_code == 200  # activity keeps it alive
    clock.now += 15 * 60 + 1
    assert client.get("/api/classes/").status_code == 401  # idle

    clock.now += 16 * 60
    sign_in(client, clock)
    for _ in range(8 * 6 + 1):  # stay active every 10 minutes until just past 8 hours
        clock.now += 10 * 60
        client.get("/api/classes/")
    assert client.get("/api/classes/").status_code == 401  # absolute limit


def test_forged_cookie_creates_no_session(ctx):
    client = ctx[0].test_client()
    client.set_cookie(auth_mod.COOKIE_NAME, "made-up-value")
    assert client.get("/api/classes/").status_code == 401


def test_disabling_a_user_blocks_their_next_login(ctx):
    app, _, clock, tmp_path = ctx
    users = auth_mod.load_users(tmp_path / "users.xml")
    users["alice"].status = "disabled"
    auth_mod.save_users(tmp_path / "users.xml", users)
    assert app.test_client().post("/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 401


def test_audit_log_records_events_without_secrets(ctx):
    app, _, clock, tmp_path = ctx
    client = app.test_client()
    client.post("/auth/login", json={"username": "alice", "password": "wrong-password-value"})
    me = sign_in(client, clock)
    client.post("/api/instances", json={"name": "x"}, headers={"X-CSRF-Token": me["csrfToken"]})
    text = (tmp_path / "admin-audit.log").read_text(encoding="utf-8")
    events = [json.loads(line) for line in text.splitlines()]
    assert [e["event"] for e in events] == ["login_failed", "login", "write"]
    assert events[2]["user"] == "Alice" and events[2]["path"] == "/api/instances" and events[2]["mode"] == "auth"
    assert "wrong-password-value" not in text and PASSWORD not in text and code_at(clock) not in text


# ---------------------------------------------------------------- auth off


def test_off_mode_needs_no_login_and_no_users_file(tmp_path):
    app = Flask(__name__)
    auth_mod.init_auth(app, auth_mod.load_settings({"AZON_AUTH_MODE": "off"}))

    @app.get("/api/classes/")
    def classes():
        return jsonify([])

    @app.post("/api/instances")
    def write():
        return jsonify({"ok": True})

    client = app.test_client()
    assert client.get("/api/classes/").status_code == 200
    assert client.post("/api/instances", json={}).status_code == 200
    me = client.get("/auth/me").get_json()
    assert me == {"mode": "off", "user": "anonymous-full-access", "fullName": "Sign-in off", "canEdit": True, "csrfToken": None}
    assert client.post("/auth/login", json={"username": "a", "password": "b"}).status_code == 400
    # A cross-site page still cannot write.
    assert client.post("/api/instances", json={}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_static_page_served_from_same_origin(tmp_path):
    make_users(tmp_path)
    public = tmp_path / "public"
    public.mkdir()
    (public / "index.html").write_text("<h1>admin</h1>", encoding="utf-8")
    app, _, _ = make_app(tmp_path, AZON_ADMIN_STATIC_DIR=str(public))
    client = app.test_client()
    assert client.get("/").data == b"<h1>admin</h1>"
    assert client.get("/index.html").status_code == 200
    assert client.get("/../users.xml").status_code in (401, 404)
    assert client.get("/api/classes/").status_code == 401
