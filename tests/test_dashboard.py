"""HTTP contract tests for the embedded factory dashboard."""

from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import pytest

import simplecode.dashboard.server as dashboard_server_module
from simplecode.dashboard import DashboardServer
from simplecode.store import SessionStore, factory_job_external_key


@pytest.fixture
def dashboard(tmp_path: Path) -> Generator[tuple[DashboardServer, str]]:
    db_path = tmp_path / "dashboard.db"
    store = SessionStore(str(db_path))
    store.ensure_schema()
    assert store.claim_factory_job(
        "space <script>",
        "project/one",
        "item?one",
        project_key="ENG",
        work_item_ref="ENG-4",
        title="<img src=x onerror=alert(1)>",
        repo="acme/widget",
        branch="factory/ENG-4",
        session_id="session-4",
    )
    key = factory_job_external_key("space <script>", "project/one", "item?one")
    store.update_factory_job(
        key,
        "running",
        current_stage="build",
        current_agent="builder <svg/onload=alert(1)>",
        current_prompt=2,
        pr_url="javascript:alert(1)",
        last_error="Authorization: Bearer very-secret\nfailed\x00now",
    )
    store.append_factory_event(
        key,
        "agent_started",
        stage="build",
        status="running",
        message="<script>not public</script>",
        metadata={"task_body": "not public", "agent_output": "not public"},
    )
    assert store.claim_factory_job(
        "space",
        "project-two",
        "item-two",
        project_key="OPS",
        work_item_ref="OPS-2",
        title="Published",
    )
    safe_key = factory_job_external_key("space", "project-two", "item-two")
    store.update_factory_job(
        safe_key,
        "completed",
        pr_url="https://github.com/acme/widget/pull/2",
    )
    store.close()

    server = DashboardServer(str(db_path), port=0).start()
    try:
        yield server, key
    finally:
        server.stop()


def _request(
    server: DashboardServer,
    path: str,
    *,
    method: str = "GET",
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(server.host, server.port, timeout=3)
    connection.request(method, path)
    response = connection.getresponse()
    body = response.read()
    headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, headers, body


def _request_with_host(
    server: DashboardServer,
    path: str,
    host_header: str | None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(server.host, server.port, timeout=3)
    connection.putrequest("GET", path, skip_host=True)
    if host_header is not None:
        connection.putheader("Host", host_header)
    connection.endheaders()
    response = connection.getresponse()
    body = response.read()
    headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, headers, body


def test_page_is_inline_hardened_and_polling(dashboard: tuple[DashboardServer, str]) -> None:
    server, _ = dashboard
    status, headers, body = _request(server, "/")
    page = body.decode()

    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert headers["cache-control"] == "no-store"
    assert "default-src 'none'" in headers["content-security-policy"]
    assert "setInterval(refresh,2000)" in page
    assert "<style nonce=" in page
    assert "<script nonce=" in page
    assert "innerHTML" not in page
    assert ".textContent=" in page
    assert "<img src=x onerror=alert(1)>" not in page
    assert "task_body" not in page


def test_snapshot_contract_and_serialization(dashboard: tuple[DashboardServer, str]) -> None:
    server, key = dashboard
    status, headers, body = _request(server, "/api/snapshot")
    payload = json.loads(body)

    assert status == 200
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert headers["cache-control"] == "no-store"
    assert payload["summary"] == {
        "total": 2,
        "active": 1,
        "completed": 1,
        "failed": 0,
        "by_status": {"completed": 1, "running": 1},
    }
    exposed = next(job for job in payload["jobs"] if job["external_key"] == key)
    assert exposed["current_agent"] == "builder <svg/onload=alert(1)>"
    assert exposed["current_prompt"] == 2
    assert exposed["pr_url"] is None
    assert "very-secret" not in exposed["last_error"]
    assert "\x00" not in exposed["last_error"]
    assert "plane_progress_comment_id" not in exposed

    safe = next(job for job in payload["jobs"] if job["work_item_ref"] == "OPS-2")
    assert safe["pr_url"] == "https://github.com/acme/widget/pull/2"


def test_host_header_accepts_only_bound_authority(
    dashboard: tuple[DashboardServer, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _ = dashboard
    host = f"[{server.host}]" if ":" in server.host else server.host
    expected = f"{host}:{server.port}"
    assert _request_with_host(server, "/health", expected)[0] == 200

    opened = 0

    def unexpected_store(_database_url: str) -> SessionStore:
        nonlocal opened
        opened += 1
        raise AssertionError("invalid Host must be rejected before opening the store")

    monkeypatch.setattr(dashboard_server_module, "SessionStore", unexpected_store)
    for malicious in (
        None,
        "evil.example",
        f"evil.example:{server.port}",
        host,
        f"{host}:{server.port + 1}",
        f"{expected}.evil.example",
    ):
        status, headers, body = _request_with_host(server, "/api/snapshot", malicious)
        assert status == 400
        assert headers["cache-control"] == "no-store"
        assert json.loads(body) == {"error": "invalid host header"}
    assert opened == 0


def test_url_decoded_detail_has_ordered_safe_events(
    dashboard: tuple[DashboardServer, str],
) -> None:
    server, key = dashboard
    status, _, body = _request(server, f"/api/jobs/{quote(key, safe='')}")
    payload = json.loads(body)

    assert status == 200
    assert payload["job"]["external_key"] == key
    assert payload["job"]["project_id"] == "project/one"
    assert [event["event_type"] for event in payload["events"]] == ["agent_started"]
    assert list(payload["events"][0]) == [
        "event_id",
        "event_type",
        "stage",
        "status",
        "created_at",
    ]
    serialized = body.decode()
    assert "not public" not in serialized
    assert "metadata" not in serialized
    assert "message" not in serialized


def test_detail_returns_newest_event_window_in_ascending_order(
    dashboard: tuple[DashboardServer, str],
) -> None:
    server, key = dashboard
    store = SessionStore(server.database_url)
    try:
        for index in range(505):
            store.append_factory_event(key, f"event_{index}", stage="build")
    finally:
        store.close()

    status, _, body = _request(server, f"/api/jobs/{quote(key, safe='')}")
    events = json.loads(body)["events"]
    assert status == 200
    assert len(events) == 500
    assert events[0]["event_type"] == "event_5"
    assert events[-1]["event_type"] == "event_504"
    assert [event["event_id"] for event in events] == sorted(
        event["event_id"] for event in events
    )


def test_health_unknown_and_get_only_contract(dashboard: tuple[DashboardServer, str]) -> None:
    server, _ = dashboard
    status, headers, body = _request(server, "/health")
    assert status == 200
    assert json.loads(body) == {"status": "ok"}
    assert headers["cache-control"] == "no-store"

    status, _, body = _request(server, "/missing")
    assert status == 404
    assert json.loads(body) == {"error": "not found"}

    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "PROPFIND"):
        status, headers, body = _request(server, "/api/snapshot", method=method)
        assert status == 405
        assert headers["allow"] == "GET, HEAD"
        assert json.loads(body) == {"error": "method not allowed"}


def test_head_matches_get_headers_without_body(dashboard: tuple[DashboardServer, str]) -> None:
    server, _ = dashboard
    get_status, get_headers, get_body = _request(server, "/api/snapshot")
    head_status, head_headers, head_body = _request(server, "/api/snapshot", method="HEAD")

    assert head_status == get_status == 200
    assert head_body == b""
    assert head_headers["content-length"] == str(len(get_body))
    assert head_headers["content-type"] == get_headers["content-type"]


def test_invalid_and_missing_job_keys(dashboard: tuple[DashboardServer, str]) -> None:
    server, _ = dashboard
    status, _, _ = _request(server, "/api/jobs/%00bad")
    assert status == 400
    status, _, _ = _request(server, "/api/jobs/missing%3Akey")
    assert status == 404


def test_concurrent_refreshes_use_thread_safe_connections(
    dashboard: tuple[DashboardServer, str],
) -> None:
    server, _ = dashboard

    def refresh(_: int) -> int:
        status, _headers, body = _request(server, "/api/snapshot")
        assert json.loads(body)["summary"]["total"] == 2
        return status

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(refresh, range(24))) == [200] * 24


def test_loopback_default_port_zero_and_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    store = SessionStore(str(db_path))
    store.ensure_schema()
    store.close()
    server = DashboardServer(str(db_path))

    assert server.host == "127.0.0.1"
    assert server.port == 0
    assert not server.is_running
    server.start()
    first_port = server.port
    assert first_port > 0
    assert server.is_running
    assert _request(server, "/health")[0] == 200
    server.stop()
    server.stop()
    assert not server.is_running

    server.start()
    try:
        assert server.port > 0
        assert _request(server, "/health")[0] == 200
    finally:
        server.stop()


def test_ipv6_loopback_uses_bracketed_host_header(tmp_path: Path) -> None:
    if not socket.has_ipv6:
        pytest.skip("IPv6 is unavailable")
    db_path = tmp_path / "ipv6.db"
    store = SessionStore(str(db_path))
    store.ensure_schema()
    store.close()
    server = DashboardServer(str(db_path), host="::1")
    try:
        server.start()
    except OSError:
        pytest.skip("IPv6 loopback binding is unavailable")
    try:
        expected = f"[{server.host}]:{server.port}"
        assert server.url == f"http://{expected}"
        assert _request_with_host(server, "/health", expected)[0] == 200
        assert _request_with_host(server, "/health", f"{server.host}:{server.port}")[0] == 400
    finally:
        server.stop()
