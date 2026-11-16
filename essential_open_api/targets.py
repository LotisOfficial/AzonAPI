"""Allowlist of Viewer publish targets.

``AZON_VIEWER_TARGETS_FILE`` names a JSON file::

    {
      "local": {
        "url": "http://localhost:8080/A230Viewer",
        "user": "publisher",
        "password_file": "C:/path/to/secrets/viewer_pwd.txt"
      }
    }

Callers choose a target by name; they can never supply a URL. ``user`` and
``password_file`` are optional: when a target has no ``password_file`` the
caller must send ``user`` and ``pwd`` in the JSON body of the publish request.

If the variable is unset, publishing is disabled. If it is set but the file is
missing or malformed, the application refuses to start.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit


class TargetConfigError(RuntimeError):
    """Raised at startup when the publish-target configuration is unusable."""


def load_targets(path: Optional[str] = None) -> dict[str, dict]:
    path = path or os.environ.get("AZON_VIEWER_TARGETS_FILE")
    if not path:
        return {}

    file_path = Path(path)
    if not file_path.is_file():
        raise TargetConfigError(f"Viewer targets file not found: {file_path}")
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TargetConfigError(f"Viewer targets file is not valid JSON: {file_path}") from exc
    if not isinstance(raw, dict):
        raise TargetConfigError("Viewer targets file must be a JSON object keyed by target name.")

    targets: dict[str, dict] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise TargetConfigError(f"Target '{name}' must be an object.")
        url = str(spec.get("url", "")).strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise TargetConfigError(f"Target '{name}' needs an http(s) 'url'.")
        if parts.username or parts.password:
            raise TargetConfigError(f"Target '{name}' must not embed credentials in its URL.")
        password_file = spec.get("password_file")
        if password_file and not Path(password_file).is_file():
            raise TargetConfigError(f"Target '{name}': password_file not found.")
        targets[str(name)] = {
            "url": url.rstrip("/"),
            "user": spec.get("user") or None,
            "password_file": password_file or None,
        }
    return targets


def read_password(target: dict) -> Optional[str]:
    """Return the stored password for a target, or None if it has none."""
    password_file = target.get("password_file")
    if not password_file:
        return None
    return Path(password_file).read_text(encoding="utf-8").strip() or None
