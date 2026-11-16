import json

import pytest

from conftest import bearer


# ----- startup fails closed --------------------------------------------------

def test_refuses_to_start_without_token_file(monkeypatch):
    import essential_open_api
    from essential_open_api.auth import AuthConfigError

    monkeypatch.delenv("AZON_API_TOKENS_FILE", raising=False)
    with pytest.raises(AuthConfigError):
        essential_open_api.create_app()


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        "not json",
        json.dumps([{"name": "x", "role": "root", "sha256": "0" * 64}]),
        json.dumps([{"name": "x", "role": "read", "sha256": "short"}]),
        json.dumps([{"name": "x", "role": "read", "sha256": "0" * 64}] * 2),
    ],
)
def test_refuses_bad_token_files(monkeypatch, tmp_path, content):
    import essential_open_api
    from essential_open_api.auth import AuthConfigError

    bad = tmp_path / "bad.json"
    bad.write_text(content, encoding="utf-8")
    monkeypatch.setenv("AZON_API_TOKENS_FILE", str(bad))
    with pytest.raises(AuthConfigError):
        essential_open_api.create_app()


def test_refuses_bad_targets_file(monkeypatch, tmp_path, app):
    import essential_open_api
    from essential_open_api.targets import TargetConfigError

    bad = tmp_path / "bad_targets.json"
    bad.write_text(json.dumps({"x": {"url": "file:///etc/passwd"}}), encoding="utf-8")
    monkeypatch.setenv("AZON_VIEWER_TARGETS_FILE", str(bad))
    with pytest.raises(TargetConfigError):
        essential_open_api.create_app()


# ----- authentication --------------------------------------------------------

def test_health_is_open_and_minimal(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert set(response.get_json()) == {"status", "kb_loaded"}


@pytest.mark.parametrize(
    "path",
    ["/api/classes/", "/api/repositories", "/api/repository", "/api/instances/x", "/api/publish-targets",
     "/api/publish-status?id=1", "/api/list_items?class=X"],
)
def test_get_requires_token(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic dGVzdDp0ZXN0"},
        {"Authorization": "test-read-token"},
    ],
)
def test_bad_credentials_rejected(client, header):
    assert client.get("/api/repositories", headers=header).status_code == 401


def test_token_in_query_string_is_ignored(client):
    assert client.get("/api/repositories?token=test-admin-token").status_code == 401


@pytest.mark.parametrize("method,path", [("post", "/api/instances"), ("post", "/api/instances/batch"),
                                         ("patch", "/api/instances/x"), ("delete", "/api/instances/x"),
                                         ("post", "/api/repository"), ("post", "/api/publish")])
def test_writes_require_token(client, method, path):
    assert getattr(client, method)(path, json={}).status_code == 401


# ----- roles -----------------------------------------------------------------

def test_read_token_can_read(client):
    assert client.get("/api/repositories", headers=bearer("read")).status_code == 200


@pytest.mark.parametrize("method,path", [("post", "/api/instances"), ("post", "/api/instances/batch"),
                                         ("patch", "/api/instances/x"), ("delete", "/api/instances/x")])
def test_read_token_cannot_write(client, method, path):
    assert getattr(client, method)(path, json={}, headers=bearer("read")).status_code == 403


def test_write_token_passes_auth_on_writes(client):
    # No knowledge base in these tests, so the handler itself answers 500;
    # the point is that authentication and authorisation let it through.
    response = client.post("/api/instances", json={"className": "X", "name": "y"}, headers=bearer("write"))
    assert response.status_code == 500
    assert "Knowledge Base" in response.get_json()["error"]


@pytest.mark.parametrize("role", ["read", "write"])
@pytest.mark.parametrize("method,path", [("post", "/api/repository"), ("post", "/api/publish"),
                                         ("get", "/api/publish-targets"), ("get", "/api/publish-status?id=1")])
def test_admin_endpoints_need_admin(client, role, method, path):
    kwargs = {"json": {}} if method == "post" else {}
    assert getattr(client, method)(path, headers=bearer(role), **kwargs).status_code == 403


# ----- request hygiene -------------------------------------------------------

def test_non_json_body_rejected(client):
    response = client.post("/api/instances", data='{"className":"X","name":"y"}',
                           headers={**bearer("write"), "Content-Type": "text/plain"})
    assert response.status_code == 415


def test_no_cors_headers(client):
    response = client.get("/api/repositories", headers={**bearer("read"), "Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in response.headers
    preflight = client.options("/api/instances", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "Access-Control-Allow-Origin" not in preflight.headers


def test_oversized_body_rejected(client):
    response = client.post("/api/instances", data=b"x" * (1024 * 1024 + 10),
                           headers={**bearer("write"), "Content-Type": "application/json"})
    assert response.status_code == 413


def test_swagger_off_by_default(client):
    assert client.get("/api/").status_code == 404
    assert client.get("/apispec.json").status_code == 404


# ----- publish ---------------------------------------------------------------

def test_publish_is_post_only(client):
    assert client.get("/api/publish?url=http://evil.example&user=a&pwd=b", headers=bearer("admin")).status_code == 405


def test_publish_targets_hide_urls_and_secrets(client):
    body = client.get("/api/publish-targets", headers=bearer("admin")).get_data(as_text=True)
    assert "viewer.test" not in body and "viewer-secret" not in body and "password_file" not in body
    assert json.loads(body)["targets"] == [
        {"name": "prompt", "needsCredentials": True},
        {"name": "stored", "needsCredentials": False},
    ]


def test_publish_rejects_unknown_target_and_caller_url(client, monkeypatch):
    from essential_open_api import routes

    monkeypatch.setattr(routes, "get_project", lambda: object())
    called = []
    monkeypatch.setattr(routes, "call_publish_async", lambda *a: called.append(a) or "job-1")

    for body in ({"target": "nope"}, {"url": "http://evil.example"}, {"target": "http://evil.example"}, {}):
        assert client.post("/api/publish", json=body, headers=bearer("admin")).status_code == 400
    assert called == []


def test_publish_uses_server_side_url_and_credentials(client, monkeypatch):
    from essential_open_api import routes

    project = object()
    monkeypatch.setattr(routes, "get_project", lambda: project)
    called = []
    monkeypatch.setattr(routes, "call_publish_async", lambda *a: called.append(a) or "job-1")

    # Caller-supplied url/user/pwd are ignored for a target with stored credentials.
    response = client.post("/api/publish", headers=bearer("admin"),
                           json={"target": "stored", "url": "http://evil.example", "user": "x", "pwd": "y"})
    assert response.status_code == 200
    assert called == [(project, "http://viewer.test/A230Viewer", "publisher", "viewer-secret")]
    assert "viewer-secret" not in response.get_data(as_text=True)

    # A target without stored credentials needs them in the body.
    assert client.post("/api/publish", json={"target": "prompt"}, headers=bearer("admin")).status_code == 400
    response = client.post("/api/publish", json={"target": "prompt", "user": "u", "pwd": "p"}, headers=bearer("admin"))
    assert response.status_code == 200
    assert called[-1] == (project, "http://viewer.test/Other", "u", "p")


# ----- repository switching --------------------------------------------------

def test_repository_listing_has_ids_not_paths(client, tmp_root):
    body = client.get("/api/repositories", headers=bearer("read")).get_data(as_text=True)
    data = json.loads(body)
    assert [r["id"] for r in data["repositories"]] == ["Alpha/Alpha.pprj", "Beta.pprj"]
    assert all("path" not in r for r in data["repositories"])
    assert str(tmp_root) not in body and str(tmp_root).replace("\\", "\\\\") not in body

    current = client.get("/api/repository", headers=bearer("read")).get_json()
    assert set(current) == {"id", "name", "file", "loaded"}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"id": ""},
        {"id": "../outside.pprj"},
        {"id": "Alpha/../../outside.pprj"},
        {"id": "Alpha\\Alpha.pprj"},
        {"id": "Missing/Missing.pprj"},
        {"id": ["Alpha/Alpha.pprj"]},
        {"path": "Alpha/Alpha.pprj"},
    ],
)
def test_repository_switch_rejects_unlisted(client, monkeypatch, body):
    from essential_open_api import routes

    called = []
    monkeypatch.setattr(routes, "reload_pprj", lambda repo_id: called.append(repo_id) or (True, "ok"))
    assert client.post("/api/repository", json=body, headers=bearer("admin")).status_code == 400
    assert called == []


def test_repository_switch_rejects_absolute_path(client, monkeypatch, tmp_root):
    from essential_open_api import jvm, routes

    called = []
    monkeypatch.setattr(routes, "reload_pprj", lambda repo_id: called.append(repo_id) or (True, "ok"))
    absolute = str(tmp_root / "outside.pprj")
    assert client.post("/api/repository", json={"id": absolute}, headers=bearer("admin")).status_code == 400
    assert called == []
    assert jvm.resolve_repository_id(absolute) is None
    assert jvm.resolve_repository_id(str(tmp_root / "repositories" / "Alpha" / "Alpha.pprj")) is None


def test_repository_switch_accepts_listed_id(client, monkeypatch):
    from essential_open_api import routes

    called = []
    monkeypatch.setattr(routes, "reload_pprj", lambda repo_id: called.append(repo_id) or (True, "Repository loaded."))
    response = client.post("/api/repository", json={"id": "Beta.pprj"}, headers=bearer("admin"))
    assert response.status_code == 200
    assert called == ["Beta.pprj"]
