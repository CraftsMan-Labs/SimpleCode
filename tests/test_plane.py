"""Tests for the Plane REST client. Injected transport only — no network."""

from __future__ import annotations

import json
from collections.abc import Mapping
from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

import pytest

from simplecode.plane import (
    PlaneAPIError,
    PlaneAuthError,
    PlaneClient,
    PlaneError,
    PlaneNotFoundError,
    normalize_plane_url,
    parse_project,
    parse_work_item,
    sanitize_error_text,
)

SECRET = "plane_api_SUPERSECRET_DO_NOT_LEAK"


class FakeTransport:
    """Path-keyed HTTP stub. Records calls for assertions."""

    def __init__(self, responses: dict[tuple[str, str], tuple[int, object]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        path = urlparse(url).path
        self.calls.append((method, url, dict(headers), body))
        try:
            status, payload = self.responses[(method, path)]
        except KeyError as exc:
            raise AssertionError(f"unexpected {method} {path}") from exc
        if isinstance(payload, bytes):
            return status, payload
        if payload is None:
            return status, b""
        return status, json.dumps(payload).encode("utf-8")


class SequenceTransport:
    """Return queued responses for the same path (pagination)."""

    def __init__(self, responses: dict[tuple[str, str], list[tuple[int, object]]]) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        path = urlparse(url).path
        self.calls.append((method, url))
        queue = self.responses.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected {method} {path}")
        status, payload = queue.pop(0)
        encoded = b"" if payload is None else json.dumps(payload).encode("utf-8")
        return status, encoded


def _client(transport: FakeTransport | SequenceTransport) -> PlaneClient:
    return PlaneClient(
        "http://localhost:8180/",
        "craftsman",
        SECRET,
        transport=transport,
    )


def _item(
    item_id: str = "wi-1",
    *,
    project: str = "proj-1",
    state_id: str = "state-ready",
    state_name: str = "Ready for Agent",
    name: str = "Do the thing",
) -> dict[str, object]:
    return {
        "id": item_id,
        "name": name,
        "identifier": "ENG-12",
        "sequence_id": 12,
        "project_id": project,
        "description_stripped": "Implement login",
        "description_html": "<p>Implement login</p>",
        "priority": "high",
        "state_id": state_id,
        "state": {"id": state_id, "name": state_name, "group": "unstarted"},
    }


def test_normalize_plane_url() -> None:
    assert normalize_plane_url("http://localhost:8180/api/v1/") == "http://localhost:8180"
    assert normalize_plane_url("https://api.plane.so/api/v2") == "https://api.plane.so"
    with pytest.raises(PlaneError, match="absolute http"):
        normalize_plane_url("localhost:8180")
    with pytest.raises(PlaneError, match="credentials"):
        normalize_plane_url("http://user:pass@localhost:8180")


def test_constructor_rejects_empty_key() -> None:
    with pytest.raises(PlaneError, match="API key is empty"):
        PlaneClient("http://localhost:8180", "ws", "  ")


def test_list_ready_work_items_paginated() -> None:
    ready = "state-ready"
    transport = SequenceTransport(
        {
            ("GET", "/api/v1/workspaces/craftsman/projects/proj-1/states/"): [
                (
                    200,
                    {
                        "results": [
                            {"id": ready, "name": "Ready for Agent"},
                            {"id": "state-wip", "name": "In Progress"},
                        ],
                        "next_page_results": False,
                    },
                )
            ],
            ("GET", "/api/v1/workspaces/craftsman/projects/proj-1/work-items/"): [
                (
                    200,
                    {
                        "results": [_item("wi-1"), "skip-me", {"name": "no id"}],
                        "next_page_results": True,
                        "next_cursor": "100:1:0",
                    },
                ),
                (
                    200,
                    {
                        "results": [
                            _item("wi-2"),
                            _item("wi-other", state_id="state-wip", state_name="In Progress"),
                        ],
                        "next_page_results": False,
                    },
                ),
            ],
        }
    )
    items = _client(transport).list_ready_work_items("proj-1")
    assert [item.id for item in items] == ["wi-1", "wi-2"]
    assert items[0].identifier == "ENG-12"
    assert items[0].description == "Implement login"
    assert any("state=state-ready" in url for _, url in transport.calls if "work-items" in url)


def test_list_ready_accepts_bare_list_and_state_id() -> None:
    transport = FakeTransport(
        {
            ("GET", "/api/v1/workspaces/craftsman/projects/proj-1/work-items/"): (
                200,
                [_item("wi-1")],
            )
        }
    )
    items = _client(transport).list_ready_work_items("proj-1", ready_state_id="state-ready")
    assert len(items) == 1
    assert items[0].state_name == "Ready for Agent"


def test_list_ready_excludes_items_without_state() -> None:
    item_without_state = _item("wi-missing-state")
    item_without_state.pop("state")
    item_without_state.pop("state_id")
    transport = FakeTransport(
        {
            ("GET", "/api/v1/workspaces/craftsman/projects/proj-1/work-items/"): (
                200,
                [item_without_state],
            )
        }
    )

    items = _client(transport).list_ready_work_items(
        "proj-1",
        ready_state_id="state-ready",
    )

    assert items == []


def test_get_work_item_context() -> None:
    payload = _item()
    payload["assignees"] = [{"display_name": "ana", "email": "ana@example.com"}]
    payload["labels"] = [{"name": "factory"}]
    transport = FakeTransport(
        {
            ("GET", "/api/v1/workspaces/craftsman/projects/proj-1/work-items/wi-1/"): (
                200,
                payload,
            ),
            (
                "GET",
                "/api/v1/workspaces/craftsman/projects/proj-1/work-items/wi-1/comments/",
            ): (
                200,
                {
                    "results": [
                        {
                            "id": "c1",
                            "comment_html": "<p>hello</p>",
                            "comment_stripped": "hello",
                        },
                        {"name": "broken"},
                    ]
                },
            ),
        }
    )
    context = _client(transport).get_work_item_context("proj-1", "wi-1")
    assert context.work_item.name == "Do the thing"
    assert context.comments[0].body == "hello"
    assert context.assignees == ("ana",)
    assert context.labels == ("factory",)


def test_list_comments_is_public_and_paginated() -> None:
    path = "/api/v1/workspaces/craftsman/projects/proj-1/work-items/wi-1/comments/"
    transport = SequenceTransport(
        {
            ("GET", path): [
                (
                    200,
                    {
                        "results": [
                            {
                                "id": "c1",
                                "comment_html": "<p>first</p>",
                                "comment_stripped": "first",
                            }
                        ],
                        "next_page_results": True,
                        "next_cursor": "100:1:0",
                    },
                ),
                (
                    200,
                    {
                        "results": [
                            {
                                "id": "c2",
                                "comment_html": "<p>second</p>",
                                "comment_stripped": "second",
                            }
                        ],
                        "next_page_results": False,
                    },
                ),
            ]
        }
    )

    comments = _client(transport).list_comments("proj-1", "wi-1")

    assert [comment.id for comment in comments] == ["c1", "c2"]


def test_post_comment_wraps_plain_text() -> None:
    transport = FakeTransport(
        {
            (
                "POST",
                "/api/v1/workspaces/craftsman/projects/proj-1/work-items/wi-1/comments/",
            ): (
                201,
                {"id": "c1", "comment_html": "<p>ok &amp; go</p>"},
            )
        }
    )
    comment = _client(transport).post_comment("proj-1", "wi-1", "ok & go")
    assert comment.id == "c1"
    body = json.loads(transport.calls[0][3] or b"")
    assert body["comment_html"] == "<p>ok &amp; go</p>"
    assert transport.calls[0][2]["X-API-Key"] == SECRET
    assert transport.calls[0][2]["Content-Type"] == "application/json"


def test_post_comment_passes_through_html() -> None:
    transport = FakeTransport(
        {
            (
                "POST",
                "/api/v1/workspaces/craftsman/projects/proj-1/work-items/wi-1/comments/",
            ): (201, {"id": "c1", "comment_html": "<p>Ready</p>"})
        }
    )
    _client(transport).post_comment("proj-1", "wi-1", "<p>Ready</p>")
    body = json.loads(transport.calls[0][3] or b"")
    assert body["comment_html"] == "<p>Ready</p>"


def test_update_comment_uses_patch_on_existing_comment_endpoint() -> None:
    transport = FakeTransport(
        {
            (
                "PATCH",
                "/api/v1/workspaces/craftsman/projects/proj-1/work-items/wi-1/comments/c1/",
            ): (
                200,
                {"id": "c1", "comment_html": "<p>Stage: QA</p>"},
            )
        }
    )
    comment = _client(transport).update_comment("proj-1", "wi-1", "c1", "Stage: QA")
    assert comment.id == "c1"
    assert transport.calls[0][0] == "PATCH"
    body = json.loads(transport.calls[0][3] or b"")
    assert body == {"comment_html": "<p>Stage: QA</p>"}


def _projects_path() -> str:
    return "/api/v1/workspaces/craftsman/projects/"


def _project(
    project_id: str,
    *,
    identifier: str | None = "ENG",
    name: str = "Engineering",
) -> dict[str, object]:
    payload: dict[str, object] = {"id": project_id, "name": name}
    if identifier is not None:
        payload["identifier"] = identifier
    return payload


def test_parse_project_defensive() -> None:
    assert parse_project(None) is None
    assert parse_project({"identifier": "ENG"}) is None
    parsed = parse_project(_project("proj-uuid", identifier="ENG"))
    assert parsed is not None
    assert parsed.id == "proj-uuid"
    assert parsed.identifier == "ENG"
    assert parsed.name == "Engineering"


def test_resolve_project_id_exact_uuid() -> None:
    transport = FakeTransport(
        {
            ("GET", _projects_path()): (
                200,
                {
                    "results": [
                        _project("proj-uuid", identifier="ENG"),
                        _project("other-id", identifier="DES"),
                    ]
                },
            )
        }
    )
    assert _client(transport).resolve_project_id("proj-uuid") == "proj-uuid"


def test_resolve_project_id_uuid_wins_over_identifier() -> None:
    transport = FakeTransport(
        {
            ("GET", _projects_path()): (
                200,
                {
                    "results": [
                        _project("proj-uuid", identifier="ENG"),
                        _project("other-id", identifier="proj-uuid"),
                    ]
                },
            )
        }
    )
    assert _client(transport).resolve_project_id("proj-uuid") == "proj-uuid"


def test_resolve_project_id_identifier_case_insensitive() -> None:
    transport = FakeTransport(
        {
            ("GET", _projects_path()): (
                200,
                [_project("proj-uuid", identifier="ENG"), "skip", {"name": "no id"}],
            )
        }
    )
    client = _client(transport)
    assert client.resolve_project_id("eng") == "proj-uuid"
    assert client.resolve_project_id("ENG") == "proj-uuid"
    assert len(transport.calls) == 1


def test_resolve_project_id_paginated() -> None:
    transport = SequenceTransport(
        {
            ("GET", _projects_path()): [
                (
                    200,
                    {
                        "results": [_project("proj-1", identifier="ENG")],
                        "next_page_results": True,
                        "next_cursor": "100:1:0",
                    },
                ),
                (
                    200,
                    {
                        "results": [_project("proj-2", identifier="DES")],
                        "next_page_results": False,
                    },
                ),
            ]
        }
    )
    assert _client(transport).resolve_project_id("des") == "proj-2"
    assert len(transport.calls) == 2


def test_resolve_project_id_missing() -> None:
    transport = FakeTransport(
        {
            ("GET", _projects_path()): (
                200,
                {"results": [_project("proj-uuid", identifier="ENG")]},
            )
        }
    )
    with pytest.raises(PlaneNotFoundError, match="not found"):
        _client(transport).resolve_project_id("OPS")


def test_resolve_project_id_ambiguous_identifier() -> None:
    transport = FakeTransport(
        {
            ("GET", _projects_path()): (
                200,
                {
                    "results": [
                        _project("proj-1", identifier="ENG"),
                        _project("proj-2", identifier="eng"),
                    ]
                },
            )
        }
    )
    with pytest.raises(PlaneAPIError, match="ambiguous"):
        _client(transport).resolve_project_id("ENG")


def test_resolve_project_id_rejects_empty_key() -> None:
    with pytest.raises(PlaneError, match="project_key is required"):
        _client(FakeTransport({})).resolve_project_id("  ")


def test_resolve_state_id_grouped_and_cached() -> None:
    transport = FakeTransport(
        {
            ("GET", "/api/v1/workspaces/craftsman/projects/proj-1/states/"): (
                200,
                {
                    "unstarted": [{"id": "s-ready", "name": "Ready for Agent"}],
                    "started": [{"id": "s-wip", "name": "In Progress"}],
                },
            )
        }
    )
    client = _client(transport)
    assert client.resolve_state_id("proj-1", "ready for agent") == "s-ready"
    assert client.resolve_state_id("proj-1", "Ready for Agent") == "s-ready"
    assert len(transport.calls) == 1


def test_resolve_state_id_missing() -> None:
    transport = FakeTransport(
        {
            ("GET", "/api/v1/workspaces/craftsman/projects/proj-1/states/"): (
                200,
                {"results": [{"id": "s-wip", "name": "In Progress"}]},
            )
        }
    )
    with pytest.raises(PlaneNotFoundError, match="not found"):
        _client(transport).resolve_state_id("proj-1", "Ready for Agent")


def test_resolve_state_id_ambiguous() -> None:
    transport = FakeTransport(
        {
            ("GET", "/api/v1/workspaces/craftsman/projects/proj-1/states/"): (
                200,
                {
                    "results": [
                        {"id": "s-1", "name": "Ready"},
                        {"id": "s-2", "name": "ready"},
                    ]
                },
            )
        }
    )
    with pytest.raises(PlaneAPIError, match="ambiguous"):
        _client(transport).resolve_state_id("proj-1", "Ready")


def test_update_work_item_state() -> None:
    updated = _item(state_id="state-wip", state_name="In Progress")
    transport = FakeTransport(
        {
            ("PATCH", "/api/v1/workspaces/craftsman/projects/proj-1/work-items/wi-1/"): (
                200,
                updated,
            )
        }
    )
    item = _client(transport).update_work_item_state("proj-1", "wi-1", "state-wip")
    assert item.state_id == "state-wip"
    body = json.loads(transport.calls[0][3] or b"")
    assert body == {"state": "state-wip"}


def test_update_refetches_when_patch_body_empty() -> None:
    transport = FakeTransport(
        {
            ("PATCH", "/api/v1/workspaces/craftsman/projects/proj-1/work-items/wi-1/"): (
                200,
                None,
            ),
            ("GET", "/api/v1/workspaces/craftsman/projects/proj-1/work-items/wi-1/"): (
                200,
                _item(state_id="state-wip", state_name="In Progress"),
            ),
        }
    )
    item = _client(transport).update_work_item_state("proj-1", "wi-1", "state-wip")
    assert item.id == "wi-1"


def test_auth_error_does_not_leak_api_key() -> None:
    transport = FakeTransport(
        {
            ("GET", "/api/v1/workspaces/craftsman/projects/proj-1/work-items/wi-1/"): (
                401,
                {"detail": f"invalid key {SECRET}"},
            )
        }
    )
    with pytest.raises(PlaneAuthError) as exc:
        _client(transport).get_work_item("proj-1", "wi-1")
    message = str(exc.value)
    assert SECRET not in message
    assert "plane_api_" not in message
    assert "<redacted>" in message
    assert exc.value.status == 401


def test_not_found_and_server_error() -> None:
    transport = FakeTransport(
        {
            ("GET", "/api/v1/workspaces/craftsman/projects/proj-1/work-items/missing/"): (
                404,
                {"detail": "gone"},
            ),
            ("GET", "/api/v1/workspaces/craftsman/projects/proj-1/work-items/boom/"): (
                500,
                {"error": "nope"},
            ),
        }
    )
    client = _client(transport)
    with pytest.raises(PlaneNotFoundError, match="HTTP 404"):
        client.get_work_item("proj-1", "missing")
    with pytest.raises(PlaneAPIError, match="HTTP 500"):
        client.get_work_item("proj-1", "boom")


def test_invalid_json_is_sanitized() -> None:
    transport = FakeTransport(
        {
            ("GET", "/api/v1/workspaces/craftsman/projects/proj-1/work-items/wi-1/"): (
                200,
                f"not-json {SECRET}".encode(),
            )
        }
    )
    with pytest.raises(PlaneAPIError, match="non-JSON") as exc:
        _client(transport).get_work_item("proj-1", "wi-1")
    assert SECRET not in str(exc.value)


def test_parse_work_item_defensive() -> None:
    assert parse_work_item(None) is None
    assert parse_work_item({"name": "x"}) is None
    parsed = parse_work_item(
        {
            "id": "wi-1",
            "project": {"id": "proj-1"},
            "state": "In Progress",
            "state_id": "s-1",
            "name": "Task",
        }
    )
    assert parsed is not None
    assert parsed.project_id == "proj-1"
    assert parsed.state_id == "s-1"
    assert parsed.state_name == "In Progress"


def test_sanitize_error_text_redacts_headers() -> None:
    text = sanitize_error_text(
        f"X-API-Key: {SECRET}\nAuthorization: Bearer abc",
        secret=SECRET,
    )
    assert SECRET not in text
    assert "Bearer abc" not in text
    assert "<redacted>" in text


def test_opener_injection_no_network() -> None:
    class _Response:
        def getcode(self) -> int:
            return 200

        def read(self) -> bytes:
            return json.dumps(_item()).encode()

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    class _Opener:
        def __init__(self) -> None:
            self.requests: list[Request] = []

        def open(self, request: Request, timeout: float | None = None) -> _Response:
            self.requests.append(request)
            return _Response()

    opener = _Opener()
    client = PlaneClient(
        "http://localhost:8180/api/v1",
        "craftsman",
        SECRET,
        opener=opener,  # type: ignore[arg-type]
    )
    item = client.get_work_item("proj-1", "wi-1")
    assert item.id == "wi-1"
    assert opener.requests[0].get_header("X-api-key") == SECRET
    assert (
        opener.requests[0]
        .get_full_url()
        .startswith(
            "http://localhost:8180/api/v1/workspaces/craftsman/projects/proj-1/work-items/wi-1/"
        )
    )


def test_urllib_http_error_does_not_leak_secret() -> None:
    class _Opener:
        def open(self, request: Request, timeout: float | None = None) -> Any:
            raise HTTPError(
                request.full_url,
                401,
                "nope",
                hdrs=None,  # type: ignore[arg-type]
                fp=BytesIO(json.dumps({"detail": SECRET}).encode()),
            )

    client = PlaneClient(
        "http://localhost:8180",
        "craftsman",
        SECRET,
        opener=_Opener(),  # type: ignore[arg-type]
    )
    with pytest.raises(PlaneAuthError) as exc:
        client.get_work_item("proj-1", "wi-1")
    assert SECRET not in str(exc.value)


def test_urllib_url_error_sanitized() -> None:
    class _Opener:
        def open(self, request: Request, timeout: float | None = None) -> Any:
            raise URLError("dns failed")

    client = PlaneClient(
        "http://localhost:8180",
        "craftsman",
        SECRET,
        opener=_Opener(),  # type: ignore[arg-type]
    )
    with pytest.raises(PlaneError, match="request failed"):
        client.get_work_item("proj-1", "wi-1")


def test_transport_and_opener_are_exclusive() -> None:
    with pytest.raises(TypeError, match="not both"):
        PlaneClient(
            "http://localhost:8180",
            "craftsman",
            SECRET,
            transport=FakeTransport({}),
            opener=object(),  # type: ignore[arg-type]
        )
