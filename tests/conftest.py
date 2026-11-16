"""Test fixtures. These tests exercise authentication and routing only; the
JVM and Protege project are not started (jvm.load_pprj is stubbed)."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

# jvm.py reads its paths at import time, so set them before importing the app.
_TMP = Path(tempfile.mkdtemp(prefix="azon-api-tests-"))
_REPOS = _TMP / "repositories"
(_REPOS / "Alpha").mkdir(parents=True)
(_REPOS / "Alpha" / "Alpha.pprj").write_text("; test", encoding="utf-8")
(_REPOS / "Beta.pprj").write_text("; test", encoding="utf-8")
(_TMP / "outside.pprj").write_text("; test", encoding="utf-8")

os.environ["REPO_ROOT"] = str(_REPOS)
os.environ["PPRJ_FILE"] = str(_REPOS / "Alpha" / "Alpha.pprj")
os.environ["CONFIG_FILE"] = str(_TMP / "config" / "active.json")
os.environ.pop("AZON_API_DOCS", None)

TOKENS = {"read": "test-read-token", "write": "test-write-token", "admin": "test-admin-token"}


def _write_tokens(path: Path) -> None:
    path.write_text(
        json.dumps(
            [
                {"name": f"{role}-client", "role": role, "sha256": hashlib.sha256(tok.encode()).hexdigest()}
                for role, tok in TOKENS.items()
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def tmp_root() -> Path:
    return _TMP


@pytest.fixture()
def app(monkeypatch, tmp_path):
    tokens_file = tmp_path / "tokens.json"
    _write_tokens(tokens_file)
    targets_file = tmp_path / "targets.json"
    pwd_file = tmp_path / "viewer_pwd.txt"
    pwd_file.write_text("viewer-secret\n", encoding="utf-8")
    targets_file.write_text(
        json.dumps(
            {
                "stored": {"url": "http://viewer.test/A230Viewer", "user": "publisher", "password_file": str(pwd_file)},
                "prompt": {"url": "http://viewer.test/Other"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AZON_API_TOKENS_FILE", str(tokens_file))
    monkeypatch.setenv("AZON_VIEWER_TARGETS_FILE", str(targets_file))

    import essential_open_api
    from essential_open_api import jvm

    monkeypatch.setattr(jvm, "load_pprj", lambda: None)
    application = essential_open_api.create_app()
    application.testing = True
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def bearer(role: str) -> dict:
    return {"Authorization": f"Bearer {TOKENS[role]}"}
