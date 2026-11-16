"""Bearer-token authentication and role checks for the AZON API.

Tokens are configured in a JSON file named by ``AZON_API_TOKENS_FILE``::

    [
      {"name": "admin-ui", "role": "admin", "sha256": "<hex digest of the token>"},
      {"name": "mcp",      "role": "write", "sha256": "..."}
    ]

Only SHA-256 digests are stored. Tokens are long random strings (see
``tools/make_token.py``), so an unsalted fast hash is sufficient: there is
nothing to brute-force.

The application refuses to start when the file is missing, empty or malformed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from flask import current_app, g, jsonify, request

log = logging.getLogger("azon.auth")

ROLE_RANK = {"read": 1, "write": 2, "admin": 3}

# Endpoints that need the admin role whatever the HTTP method.
ADMIN_ENDPOINTS = {
    "api.publish",
    "api.publish_status",
    "api.publish_targets",
    "api.switch_repository",
}

_BODY_METHODS = {"POST", "PUT", "PATCH"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AuthConfigError(RuntimeError):
    """Raised at startup when the token configuration is unusable."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_tokens(path: Optional[str] = None) -> list[dict]:
    """Load and validate the token file. Raises AuthConfigError on any problem."""
    path = path or os.environ.get("AZON_API_TOKENS_FILE")
    if not path:
        raise AuthConfigError(
            "AZON_API_TOKENS_FILE is not set. The API will not start without a token file."
        )

    file_path = Path(path)
    if not file_path.is_file():
        raise AuthConfigError(f"Token file not found: {file_path}")

    try:
        entries = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthConfigError(f"Token file is not valid JSON: {file_path}") from exc

    if not isinstance(entries, list) or not entries:
        raise AuthConfigError("Token file must be a non-empty JSON list.")

    names = set()
    tokens = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise AuthConfigError(f"Token entry {index} must be an object.")
        name = entry.get("name")
        role = entry.get("role")
        digest = str(entry.get("sha256", "")).lower()
        if not isinstance(name, str) or not name.strip():
            raise AuthConfigError(f"Token entry {index} needs a non-empty 'name'.")
        if name in names:
            raise AuthConfigError(f"Duplicate token name: {name}")
        if role not in ROLE_RANK:
            raise AuthConfigError(
                f"Token '{name}' has unknown role {role!r}; use one of {sorted(ROLE_RANK)}."
            )
        if not _SHA256_RE.match(digest):
            raise AuthConfigError(f"Token '{name}' needs a 64-character hex 'sha256'.")
        names.add(name)
        tokens.append({"name": name, "role": role, "sha256": digest})

    return tokens


def required_role() -> str:
    """Role needed for the current request."""
    if request.endpoint in ADMIN_ENDPOINTS:
        return "admin"
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return "read"
    return "write"


def _unauthorised():
    response = jsonify({"error": "Authentication required."})
    response.status_code = 401
    response.headers["WWW-Authenticate"] = 'Bearer realm="azon-api"'
    return response


def authenticate():
    """``before_request`` hook for the API blueprint.

    Returns a response to stop the request, or ``None`` to let it continue.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, credential = header.partition(" ")
    credential = credential.strip()
    if scheme.lower() != "bearer" or not credential:
        return _unauthorised()

    digest = hash_token(credential)
    match = None
    # Compare against every entry so timing does not reveal which one matched.
    for entry in current_app.config["AZON_TOKENS"]:
        if hmac.compare_digest(entry["sha256"], digest):
            match = entry
    if match is None:
        log.warning("Rejected token from %s for %s %s", request.remote_addr, request.method, request.path)
        return _unauthorised()

    needed = required_role()
    if ROLE_RANK[match["role"]] < ROLE_RANK[needed]:
        log.warning(
            "Token '%s' (role %s) denied %s %s (needs %s)",
            match["name"], match["role"], request.method, request.path, needed,
        )
        return jsonify({"error": "Forbidden."}), 403

    limit = current_app.config.get("MAX_CONTENT_LENGTH")
    if limit and request.content_length and request.content_length > limit:
        return jsonify({"error": "Request body too large."}), 413

    # Bodies must be declared as JSON. A browser cannot send this content type
    # cross-site without a CORS preflight, and this API grants no CORS access.
    if request.method in _BODY_METHODS and not request.is_json:
        return jsonify({"error": "Content-Type must be application/json."}), 415

    g.principal = match["name"]
    g.role = match["role"]
    return None
