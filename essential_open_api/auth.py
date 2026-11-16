"""Authentication and authorisation for the AZON Admin API.

One switch, read once at start-up:

    AZON_AUTH_MODE=auth   every /api request needs a signed-in session (the default, also for unset or unknown values)
    AZON_AUTH_MODE=off    no sign-in; every request is the fixed identity "anonymous-full-access"

The scheme follows the AZON 230 Viewer authentication design: users in an XML file outside the
application, Argon2id password hashes, TOTP as a second factor with the seed encrypted under
AZON_TOTP_KEY, in-memory lockout, server-side sessions, a CSRF token on every write, deny by default.

Nothing here imports the JVM, so the module can be tested without Protégé.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from defusedxml import ElementTree as SafeET
from flask import Blueprint, Flask, g, jsonify, request, send_from_directory

MODE_AUTH = "auth"
MODE_OFF = "off"
ANONYMOUS_USER = "anonymous-full-access"

COOKIE_NAME = "azon_admin_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

ROLE_EDITOR = "editor"
ROLE_READER = "reader"
ROLES = {ROLE_EDITOR, ROLE_READER}
STATUSES = {"active", "locked", "disabled"}

MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
IP_MAX_FAILURES = 30
PENDING_TOTP_SECONDS = 5 * 60
PENDING_TOTP_ATTEMPTS = 5

TOTP_DIGITS = 6
TOTP_STEP = 30

_PLACEHOLDERS = {"changeme", "change-me", "change_me", "secret", "password", "todo", "example", "placeholder"}

# Same parameters as the Viewer design: 64 MiB, 3 iterations, parallelism 1, 16-byte salt, 32-byte hash.
PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=1, hash_len=32, salt_len=16)


class AuthConfigError(RuntimeError):
    """Raised at start-up when the configuration is missing or unsafe. The API must not start."""


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class AuthSettings:
    mode: str
    config_dir: Optional[Path] = None
    totp_key: Optional[bytes] = None
    totp_required: bool = True
    idle_seconds: int = 15 * 60
    absolute_seconds: int = 8 * 3600
    cookie_secure: bool = True
    audit_log: Optional[Path] = None
    allowed_origins: frozenset = frozenset()
    static_dir: Optional[Path] = None

    @property
    def users_file(self) -> Optional[Path]:
        return self.config_dir / "users.xml" if self.config_dir else None


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise AuthConfigError(f"{name} must be a whole number, got {raw!r}") from exc
    if value <= 0:
        raise AuthConfigError(f"{name} must be greater than zero")
    return value


def parse_totp_key(raw: Optional[str]) -> bytes:
    """Decode AZON_TOTP_KEY: base64, exactly 32 bytes, not a placeholder."""
    value = (raw or "").strip()
    if not value or value.lower() in _PLACEHOLDERS:
        raise AuthConfigError("AZON_TOTP_KEY is required in auth mode (base64, 32 bytes). Generate one with: python -m essential_open_api.users_cli genkey")
    try:
        key = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise AuthConfigError("AZON_TOTP_KEY is not valid base64") from exc
    if len(key) != 32:
        raise AuthConfigError(f"AZON_TOTP_KEY must decode to 32 bytes, got {len(key)}")
    if len(set(key)) == 1:
        raise AuthConfigError("AZON_TOTP_KEY is a placeholder (all bytes identical)")
    return key


def load_settings(env: Optional[Mapping[str, str]] = None) -> AuthSettings:
    """Read the configuration once. Raises AuthConfigError instead of starting in an unsafe state."""
    env = os.environ if env is None else env

    raw_mode = (env.get("AZON_AUTH_MODE") or "").strip().lower()
    # Only an explicit "off" (or the Viewer's word, "anonymous") turns sign-in off. Anything else is auth.
    mode = MODE_OFF if raw_mode in {"off", "anonymous"} else MODE_AUTH

    static_raw = (env.get("AZON_ADMIN_STATIC_DIR") or "").strip()
    static_dir = Path(static_raw) if static_raw else None
    if static_dir is not None and not static_dir.is_dir():
        raise AuthConfigError(f"AZON_ADMIN_STATIC_DIR does not exist: {static_dir}")

    config_raw = (env.get("AZON_CONFIG_DIR") or "").strip()
    config_dir = Path(config_raw) if config_raw else None
    audit_raw = (env.get("AZON_AUDIT_LOG") or "").strip()
    audit_log = Path(audit_raw) if audit_raw else (config_dir / "admin-audit.log" if config_dir else None)
    origins = frozenset(o.strip().rstrip("/").lower() for o in (env.get("AZON_ALLOWED_ORIGINS") or "").split(",") if o.strip())

    common = dict(
        idle_seconds=_positive_int(env, "AZON_SESSION_IDLE_MINUTES", 15) * 60,
        absolute_seconds=_positive_int(env, "AZON_SESSION_ABSOLUTE_HOURS", 8) * 3600,
        cookie_secure=_flag(env, "AZON_COOKIE_SECURE", True),
        audit_log=audit_log,
        allowed_origins=origins,
        static_dir=static_dir,
    )

    if mode == MODE_OFF:
        return AuthSettings(mode=MODE_OFF, config_dir=config_dir, **common)

    if config_dir is None:
        raise AuthConfigError("AZON_CONFIG_DIR is required in auth mode (the folder holding users.xml). To run without sign-in set AZON_AUTH_MODE=off.")
    if not config_dir.is_dir():
        raise AuthConfigError(f"AZON_CONFIG_DIR does not exist: {config_dir}")

    settings = AuthSettings(
        mode=MODE_AUTH,
        config_dir=config_dir,
        totp_key=parse_totp_key(env.get("AZON_TOTP_KEY")),
        totp_required=_flag(env, "AZON_TOTP_REQUIRED", True),
        **common,
    )
    if not settings.users_file.is_file():
        raise AuthConfigError(f"{settings.users_file} not found. Create the first user with: python -m essential_open_api.users_cli add <username>")
    load_users(settings.users_file)  # fail now, not at the first login
    return settings


# --------------------------------------------------------------------------- users.xml


@dataclass
class User:
    username: str
    password_hash: str
    role: str = ROLE_READER
    status: str = "active"
    totp_secret: str = ""
    ea_user_name: str = ""
    tenant_id: str = "azon230"
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    must_change_password: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.username


_FIELDS = [
    ("username", "username"), ("eaUserName", "ea_user_name"), ("passwordHash", "password_hash"),
    ("totpSecret", "totp_secret"), ("status", "status"), ("adminRole", "role"), ("tenantId", "tenant_id"),
    ("email", "email"), ("firstName", "first_name"), ("lastName", "last_name"),
]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def load_users(path: Path) -> Dict[str, User]:
    """Parse users.xml. Rejects duplicates and unknown status or role values. Keys are lower-case usernames."""
    try:
        root = SafeET.parse(str(path)).getroot()
    except Exception as exc:
        raise AuthConfigError(f"{path} is not readable XML: {exc}") from exc
    if _local(root.tag) != "users":
        raise AuthConfigError(f"{path}: root element must be <users>")

    users: Dict[str, User] = {}
    for node in root:
        if _local(node.tag) != "user":
            continue
        values = {_local(child.tag): (child.text or "").strip() for child in node}
        username = values.get("username", "")
        if not username:
            raise AuthConfigError(f"{path}: a <user> has no <username>")
        key = username.lower()
        if key in users:
            raise AuthConfigError(f"{path}: duplicate username {username!r}")
        user = User(username=username, password_hash=values.get("passwordHash", ""))
        for xml_name, attr in _FIELDS:
            if xml_name in values and values[xml_name] != "":
                setattr(user, attr, values[xml_name])
        user.must_change_password = values.get("mustChangePassword", "false").lower() == "true"
        if user.status not in STATUSES:
            raise AuthConfigError(f"{path}: user {username!r} has unknown status {user.status!r}")
        if user.role not in ROLES:
            raise AuthConfigError(f"{path}: user {username!r} has unknown adminRole {user.role!r}")
        if not user.password_hash.startswith("$argon2id$"):
            raise AuthConfigError(f"{path}: user {username!r} has no Argon2id passwordHash")
        users[key] = user
    return users


def save_users(path: Path, users: Dict[str, User]) -> None:
    """Write users.xml atomically: backup, temporary file, rename."""
    root = ET.Element("users", {"version": "1"})
    for key in sorted(users):
        user = users[key]
        node = ET.SubElement(root, "user")
        for xml_name, attr in _FIELDS:
            ET.SubElement(node, xml_name).text = getattr(user, attr)
        ET.SubElement(node, "mustChangePassword").text = "true" if user.must_change_password else "false"
    ET.indent(root)
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    path = Path(path)
    if path.exists():
        path.with_suffix(".xml.bak").write_bytes(path.read_bytes())
    tmp = path.with_suffix(".xml.tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class UserStore:
    """users.xml in memory, re-read when the file changes so new users need no restart."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._mtime: Optional[int] = None
        self._users: Dict[str, User] = {}
        self._refresh()

    def _refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return  # keep the last good copy
        if mtime == self._mtime:
            return
        try:
            self._users = load_users(self.path)
            self._mtime = mtime
        except AuthConfigError as exc:
            if self._mtime is None:
                raise
            print(f"[auth] users.xml changed but is invalid; keeping the previous version: {exc}")

    def get(self, username: str) -> Optional[User]:
        with self._lock:
            self._refresh()
            return self._users.get((username or "").strip().lower())


# --------------------------------------------------------------------------- passwords and TOTP


def hash_password(password: str) -> str:
    return PASSWORD_HASHER.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def encrypt_seed(key: bytes, seed: bytes) -> str:
    """AES-256-GCM. Stored form: base64(12-byte nonce || ciphertext || 16-byte tag)."""
    nonce = secrets.token_bytes(12)
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, seed, None)).decode("ascii")


def decrypt_seed(key: bytes, stored: str) -> Optional[bytes]:
    try:
        blob = base64.b64decode(stored, validate=True)
        return AESGCM(key).decrypt(blob[:12], blob[12:], None)
    except (InvalidTag, ValueError, TypeError):
        return None


def totp_code(seed: bytes, counter: int, digits: int = TOTP_DIGITS) -> str:
    """RFC 6238 with HMAC-SHA1 (the authenticator-app default)."""
    digest = hmac.new(seed, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % (10 ** digits)).zfill(digits)


def match_totp(seed: bytes, code: str, now: float, last_counter: int = -1) -> Optional[int]:
    """Return the matching time step (one step of drift either way), or None. Steps already used are refused."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    current = int(now // TOTP_STEP)
    matched = None
    for counter in (current - 1, current, current + 1):
        if hmac.compare_digest(totp_code(seed, counter), code) and counter > last_counter:
            matched = counter if matched is None else matched
    return matched


# --------------------------------------------------------------------------- sessions and throttling


@dataclass
class Session:
    sid: str
    username: str
    full_name: str
    role: str
    state: str  # "pending_totp" or "active"
    created: float
    last_seen: float
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    attempts: int = 0


class SessionStore:
    """Server-side sessions, in memory. Single instance only; a restart signs everyone out."""

    def __init__(self, settings: AuthSettings, clock: Callable[[], float]):
        self._settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: Dict[str, Session] = {}

    def create(self, user: User, state: str) -> Session:
        now = self._clock()
        session = Session(sid=secrets.token_urlsafe(32), username=user.username, full_name=user.full_name,
                          role=user.role, state=state, created=now, last_seen=now)
        with self._lock:
            self._sessions[session.sid] = session
        return session

    def get(self, sid: Optional[str], touch: bool = True) -> Optional[Session]:
        if not sid:
            return None
        now = self._clock()
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return None
            if session.state == "pending_totp":
                expired = now - session.created > PENDING_TOTP_SECONDS
            else:
                expired = (now - session.last_seen > self._settings.idle_seconds
                           or now - session.created > self._settings.absolute_seconds)
            if expired:
                del self._sessions[sid]
                return None
            if touch and session.state == "active":
                session.last_seen = now
            return session

    def destroy(self, sid: Optional[str]) -> None:
        with self._lock:
            self._sessions.pop(sid or "", None)


class Throttle:
    """Counts failures per key. After `limit` failures inside `window` seconds the key is blocked until the window passes."""

    def __init__(self, limit: int, window: int, clock: Callable[[], float]):
        self._limit, self._window, self._clock = limit, window, clock
        self._lock = threading.Lock()
        self._failures: Dict[str, List[float]] = {}

    def _recent(self, key: str) -> List[float]:
        cutoff = self._clock() - self._window
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self._limit

    def fail(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(self._clock())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


# --------------------------------------------------------------------------- Flask wiring


class Auth:
    """Holds the state for one application. Created by init_auth()."""

    def __init__(self, settings: AuthSettings, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.clock = clock
        self.sessions = SessionStore(settings, clock)
        self.user_failures = Throttle(MAX_FAILURES, LOCKOUT_SECONDS, clock)
        self.ip_failures = Throttle(IP_MAX_FAILURES, LOCKOUT_SECONDS, clock)
        self.users = UserStore(settings.users_file) if settings.mode == MODE_AUTH else None
        self._last_totp_step: Dict[str, int] = {}
        self._audit_lock = threading.Lock()
        self._last_off_warning = 0.0

    # ---- audit

    def audit(self, event: str, user: str, status: int, **extra) -> None:
        if self.settings.audit_log is None:
            return
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())),
            "mode": self.settings.mode, "event": event, "user": user,
            "ip": request.remote_addr, "method": request.method, "path": request.path, "status": status, **extra,
        }
        try:
            with self._audit_lock, open(self.settings.audit_log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError as exc:
            print(f"[auth] could not write audit log: {exc}")

    # ---- helpers

    def current_session(self, state: str = "active") -> Optional[Session]:
        session = self.sessions.get(request.cookies.get(COOKIE_NAME))
        return session if session is not None and session.state == state else None

    def set_cookie(self, response, sid: str) -> None:
        response.set_cookie(COOKIE_NAME, sid, httponly=True, secure=self.settings.cookie_secure, samesite="Lax", path="/")

    def clear_cookie(self, response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=self.settings.cookie_secure, samesite="Lax")

    def origin_allowed(self) -> bool:
        """Writes that carry an Origin header must come from this site (or a configured origin)."""
        origin = request.headers.get("Origin")
        if not origin:
            return True  # not a browser cross-site request; the CSRF token still applies in auth mode
        origin = origin.rstrip("/").lower()
        if origin in self.settings.allowed_origins:
            return True
        return urlparse(origin).netloc == request.host.lower()


def _error(code: str, status: int, message: str = ""):
    return jsonify({"error": code, "message": message or code}), status


def init_auth(app: Flask, settings: Optional[AuthSettings] = None, clock: Callable[[], float] = time.time) -> Auth:
    """Install the sign-in endpoints and the deny-by-default check on `app`. Call before registering other routes."""
    settings = settings or load_settings()
    auth = Auth(settings, clock)
    app.extensions["azon_auth"] = auth
    public_endpoints = {"health", "admin_index", "admin_static", "auth.login", "auth.totp", "auth.me"}

    if settings.mode == MODE_OFF:
        print("[auth] WARNING: AZON_AUTH_MODE=off. Sign-in is disabled and every request has full access. "
              "Use only on a development or demonstration machine.")
    else:
        print(f"[auth] Sign-in required. Users: {settings.users_file}")

    bp = Blueprint("auth", __name__, url_prefix="/auth")

    @bp.post("/login")
    def login():
        if settings.mode == MODE_OFF:
            return _error("auth_off", 400, "Sign-in is switched off on this server.")
        data = request.get_json(silent=True) or {}
        username, password = data.get("username"), data.get("password")
        if not isinstance(username, str) or not isinstance(password, str) or not username.strip() or len(username) > 128 or len(password) > 1024:
            return _error("invalid_credentials", 401, "The username or password is not correct.")
        key, ip = username.strip().lower(), request.remote_addr or "unknown"

        if auth.user_failures.blocked(key) or auth.ip_failures.blocked(ip):
            auth.audit("login_blocked", key, 429)
            return _error("too_many_attempts", 429, "Too many failed attempts. Try again in 15 minutes.")

        user = auth.users.get(key)
        # Always do one Argon2 verification so unknown users take as long as wrong passwords.
        password_ok = verify_password(user.password_hash if user else _DUMMY_HASH, password)
        if user is None or not password_ok or user.status != "active":
            auth.user_failures.fail(key)
            auth.ip_failures.fail(ip)
            auth.audit("login_failed", key, 401)
            return _error("invalid_credentials", 401, "The username or password is not correct.")

        if user.must_change_password:
            auth.audit("login_denied_password_change", user.username, 403)
            return _error("password_change_required", 403, "This account must change its password before it can sign in. Ask an administrator to reset it.")

        if not user.totp_secret:
            if settings.totp_required:
                auth.audit("login_denied_no_totp", user.username, 403)
                return _error("totp_not_enrolled", 403, "This account has no authenticator set up. Ask an administrator to enrol it.")
            auth.user_failures.reset(key)
            session = auth.sessions.create(user, "active")
            auth.audit("login", user.username, 200)
            response = jsonify({"totpRequired": False})
            auth.set_cookie(response, session.sid)
            return response

        auth.sessions.destroy(request.cookies.get(COOKIE_NAME))
        pending = auth.sessions.create(user, "pending_totp")
        response = jsonify({"totpRequired": True})
        auth.set_cookie(response, pending.sid)
        return response

    @bp.post("/totp")
    def totp():
        if settings.mode == MODE_OFF:
            return _error("auth_off", 400, "Sign-in is switched off on this server.")
        pending = auth.current_session("pending_totp")
        if pending is None:
            return _error("not_authenticated", 401, "Sign in with your username and password first.")
        key, ip = pending.username.lower(), request.remote_addr or "unknown"
        user = auth.users.get(key)
        code = (request.get_json(silent=True) or {}).get("code")
        seed = decrypt_seed(settings.totp_key, user.totp_secret) if user and user.status == "active" and user.totp_secret else None
        step = match_totp(seed, code if isinstance(code, str) else "", auth.clock(), auth._last_totp_step.get(key, -1)) if seed else None

        if auth.user_failures.blocked(key) or step is None:
            pending.attempts += 1
            auth.user_failures.fail(key)
            auth.ip_failures.fail(ip)
            auth.audit("totp_failed", pending.username, 401)
            response = _error("invalid_code", 401, "That code is not correct or has already been used.")
            if pending.attempts >= PENDING_TOTP_ATTEMPTS or auth.user_failures.blocked(key):
                auth.sessions.destroy(pending.sid)
            return response

        auth._last_totp_step[key] = step
        auth.user_failures.reset(key)
        auth.sessions.destroy(pending.sid)
        session = auth.sessions.create(user, "active")  # new session id after the second factor
        auth.audit("login", user.username, 200)
        response = jsonify({"ok": True})
        auth.set_cookie(response, session.sid)
        return response

    @bp.get("/me")
    def me():
        if settings.mode == MODE_OFF:
            return jsonify({"mode": MODE_OFF, "user": ANONYMOUS_USER, "fullName": "Sign-in off", "canEdit": True, "csrfToken": None})
        session = auth.current_session()
        if session is None:
            return _error("not_authenticated", 401, "Sign in to continue.")
        return jsonify({"mode": MODE_AUTH, "user": session.username, "fullName": session.full_name,
                        "canEdit": session.role == ROLE_EDITOR, "csrfToken": session.csrf})

    @bp.post("/logout")
    def logout():
        sid = request.cookies.get(COOKIE_NAME)
        session = auth.sessions.get(sid, touch=False)
        if session is not None:
            auth.audit("logout", session.username, 204)
        auth.sessions.destroy(sid)
        response = app.response_class(status=204)
        auth.clear_cookie(response)
        return response

    app.register_blueprint(bp)

    if settings.static_dir is not None:
        static_root = str(settings.static_dir.resolve())

        @app.get("/")
        def admin_index():
            return send_from_directory(static_root, "index.html")

        @app.get("/<path:filename>")
        def admin_static(filename):
            return send_from_directory(static_root, filename)

    @app.before_request
    def enforce():
        g.auth_user = None
        is_write = request.method not in SAFE_METHODS

        if is_write and not auth.origin_allowed():
            auth.audit("bad_origin", "-", 403, origin=request.headers.get("Origin"))
            return _error("bad_origin", 403, "This request did not come from the AZON Admin page.")

        if settings.mode == MODE_OFF:
            g.auth_user = ANONYMOUS_USER
            now = auth.clock()
            if now - auth._last_off_warning > 60:
                auth._last_off_warning = now
                print("[auth] WARNING: serving requests with sign-in off (AZON_AUTH_MODE=off).")
            return None

        if request.endpoint in public_endpoints:
            return None

        session = auth.current_session()
        if session is None:
            return _error("not_authenticated", 401, "Sign in to continue.")
        g.auth_user = session.username

        if is_write:
            sent = request.headers.get(CSRF_HEADER, "")
            if not hmac.compare_digest(sent.encode(), session.csrf.encode()):
                auth.audit("csrf_rejected", session.username, 403)
                return _error("csrf", 403, "The page is out of date. Reload it and try again.")
            if request.endpoint != "auth.logout" and session.role != ROLE_EDITOR:
                auth.audit("read_only_rejected", session.username, 403)
                return _error("read_only", 403, "Your account can view but not change the repository.")
        return None

    @app.after_request
    def finish(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        )
        if settings.cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.path.startswith(("/api", "/auth")):
            response.headers["Cache-Control"] = "no-store"
        if request.method not in SAFE_METHODS and request.path.startswith("/api") and g.get("auth_user"):
            auth.audit("write", g.auth_user, response.status_code)
        return response

    return auth
