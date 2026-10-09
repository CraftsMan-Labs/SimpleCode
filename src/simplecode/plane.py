"""Stdlib Plane REST client for the software factory.

Talks to the documented v1 work-item API with ``X-API-Key`` auth. A transport
or urllib opener can be injected so unit tests never open a network socket.
Errors are sanitized so the API key cannot leak through exception text.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol, overload
from urllib.request import OpenerDirector, Request

from simplecode.errors import SimpleCodeError

_API_PREFIX = "/api/v1"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_PER_PAGE = 100
_MAX_PAGES = 200
_MAX_ERROR_BODY = 240
_HEADER_SECRET_RE = re.compile(
    r"(?i)(x-api-key|authorization)\s*[:=]\s*\S+",
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


class PlaneError(SimpleCodeError):
    """Base error for Plane client failures."""


class PlaneAPIError(PlaneError):
    """Plane returned an unexpected HTTP status or an unusable payload."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        self.status = status
        self.method = method
        self.path = path
        super().__init__(message)


class PlaneAuthError(PlaneAPIError):
    """Missing or rejected Plane credentials (401/403)."""


class PlaneNotFoundError(PlaneAPIError):
    """Requested Plane resource does not exist."""


class PlaneTransport(Protocol):
    """Minimal HTTP transport used by :class:`PlaneClient`."""

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        """Return ``(status_code, body)``. Network failures should raise."""


@dataclass(frozen=True)
class PlaneProject:
    """Defensively parsed workspace project (id + identifier)."""

    id: str
    identifier: str | None = None
    name: str = ""
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PlaneWorkItem:
    """Defensively parsed work-item fields the factory needs."""

    id: str
    project_id: str
    name: str
    identifier: str | None = None
    sequence_id: int | None = None
    description: str = ""
    description_html: str = ""
    state_id: str | None = None
    state_name: str | None = None
    priority: str | None = None
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PlaneComment:
    """A work-item comment. ``body`` is plain text when Plane provides it."""

    id: str
    body: str = ""
    html: str = ""
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PlaneWorkItemContext:
    """Full work-item snapshot: core fields plus comments and relations."""

    work_item: PlaneWorkItem
    comments: tuple[PlaneComment, ...] = ()
    assignees: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()


class _UrllibTransport:
    """Adapter around :class:`urllib.request.OpenerDirector`."""

    def __init__(self, opener: OpenerDirector) -> None:
        self._opener = opener

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        req = Request(url, data=body, method=method, headers=dict(headers))
        try:
            with self._opener.open(req, timeout=timeout) as response:
                status = response.getcode()
                payload = response.read()
        except urllib.error.HTTPError as exc:
            payload = exc.read() if exc.fp is not None else b""
            return exc.code, payload
        except urllib.error.URLError as exc:
            reason = sanitize_error_text(str(exc.reason), secret="")
            raise PlaneError(f"Plane request failed: {reason}") from None
        except TimeoutError:
            raise PlaneError("Plane request timed out") from None
        return int(status or 200), payload


class PlaneClient:
    """Small Plane API client: list, fetch, comment, resolve and move states."""

    def __init__(
        self,
        base_url: str,
        workspace_slug: str,
        api_key: str,
        *,
        transport: PlaneTransport | None = None,
        opener: OpenerDirector | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        if transport is not None and opener is not None:
            raise TypeError("pass transport or opener, not both")
        key = api_key.strip()
        if not key:
            raise PlaneError("Plane API key is empty")
        slug = workspace_slug.strip()
        if not slug:
            raise PlaneError("workspace_slug is required")
        if timeout <= 0:
            raise PlaneError("timeout must be positive")

        self.base_url = normalize_plane_url(base_url)
        self.workspace_slug = slug
        self._api_key = key
        self._timeout = timeout
        self._transport = transport
        self._opener = opener
        self._state_cache: dict[tuple[str, str], str] = {}
        self._project_cache: dict[str, str] = {}

    def list_ready_work_items(
        self,
        project_id: str,
        *,
        ready_state: str | None = None,
        ready_state_id: str | None = None,
        per_page: int = _DEFAULT_PER_PAGE,
    ) -> list[PlaneWorkItem]:
        """Return paginated work items in the project's ready state."""
        state_id = ready_state_id or self.resolve_state_id(
            project_id, ready_state or "Ready for Agent"
        )
        params = {
            "state": state_id,
            "per_page": str(_page_size(per_page)),
            "expand": "state",
        }
        items: list[PlaneWorkItem] = []
        path = self._work_items_path(project_id)
        for raw in self._paginate(path, params):
            parsed = parse_work_item(raw, default_project_id=project_id)
            if parsed is None:
                continue
            if _state_mismatch(parsed, state_id, ready_state):
                continue
            items.append(parsed)
        return items

    def get_work_item(self, project_id: str, work_item_id: str) -> PlaneWorkItem:
        """Fetch one work item by UUID."""
        raw = self._json(
            "GET",
            self._work_item_path(project_id, work_item_id),
            query={"expand": "state,assignees,labels"},
        )
        parsed = parse_work_item(raw, default_project_id=project_id)
        if parsed is None:
            raise PlaneAPIError(
                "Plane work-item response was missing a usable id",
                method="GET",
                path=self._safe_path(self._work_item_path(project_id, work_item_id)),
            )
        return parsed

    def get_work_item_context(
        self,
        project_id: str,
        work_item_id: str,
    ) -> PlaneWorkItemContext:
        """Fetch the work item plus comments and expanded relations."""
        item = self.get_work_item(project_id, work_item_id)
        comments = self.list_comments(project_id, work_item_id)
        return PlaneWorkItemContext(
            work_item=item,
            comments=tuple(comments),
            assignees=_relation_names(item.raw.get("assignees")),
            labels=_relation_names(item.raw.get("labels")),
        )

    def post_comment(
        self,
        project_id: str,
        work_item_id: str,
        comment: str,
    ) -> PlaneComment:
        """Post a comment. Plain text is wrapped as HTML; HTML is passed through."""
        html = comment if _looks_like_html(comment) else _plain_to_html(comment)
        raw = self._json(
            "POST",
            self._comments_path(project_id, work_item_id),
            payload={"comment_html": html},
        )
        parsed = parse_comment(raw)
        if parsed is None:
            raise PlaneAPIError(
                "Plane comment response was missing a usable id",
                method="POST",
                path=self._safe_path(self._comments_path(project_id, work_item_id)),
            )
        return parsed

    def list_comments(self, project_id: str, work_item_id: str) -> list[PlaneComment]:
        """Return all comments on one work item."""
        comments: list[PlaneComment] = []
        path = self._comments_path(project_id, work_item_id)
        for raw in self._paginate(path, {"per_page": str(_DEFAULT_PER_PAGE)}):
            parsed = parse_comment(raw)
            if parsed is not None:
                comments.append(parsed)
        return comments

    def update_comment(
        self,
        project_id: str,
        work_item_id: str,
        comment_id: str,
        comment: str,
    ) -> PlaneComment:
        """Update an existing comment using Plane's comment detail endpoint."""
        html = comment if _looks_like_html(comment) else _plain_to_html(comment)
        path = self._comment_path(project_id, work_item_id, comment_id)
        raw = self._json(
            "PATCH",
            path,
            payload={"comment_html": html},
        )
        if raw is None:
            return PlaneComment(id=comment_id, body=comment, html=html)
        parsed = parse_comment(raw)
        if parsed is None:
            raise PlaneAPIError(
                "Plane comment response was missing a usable id",
                method="PATCH",
                path=self._safe_path(path),
            )
        return parsed

    def resolve_project_id(self, project_key: str) -> str:
        """Resolve a factory project key to a Plane project UUID.

        Exact ``id`` match wins. If none, the project ``identifier`` is matched
        case-insensitively. Missing or ambiguous results are rejected.
        """
        key = project_key.strip()
        if not key:
            raise PlaneError("project_key is required")
        cached = self._project_cache.get(key) or self._project_cache.get(key.lower())
        if cached is not None:
            return cached

        projects = self._list_projects()
        uuid_hits = list(dict.fromkeys(project.id for project in projects if project.id == key))
        if len(uuid_hits) == 1:
            self._project_cache[key] = uuid_hits[0]
            return uuid_hits[0]
        if len(uuid_hits) > 1:
            raise PlaneAPIError(
                f"Plane project {key!r} is ambiguous",
                method="GET",
                path=self._safe_path(self._projects_path()),
            )

        ident_hits = list(
            dict.fromkeys(
                project.id
                for project in projects
                if project.identifier is not None and project.identifier.lower() == key.lower()
            )
        )
        if not ident_hits:
            raise PlaneNotFoundError(
                f"Plane project {key!r} was not found in the workspace",
                status=404,
                method="GET",
                path=self._safe_path(self._projects_path()),
            )
        if len(ident_hits) > 1:
            raise PlaneAPIError(
                f"Plane project {key!r} is ambiguous",
                method="GET",
                path=self._safe_path(self._projects_path()),
            )
        self._project_cache[key] = ident_hits[0]
        self._project_cache[key.lower()] = ident_hits[0]
        return ident_hits[0]

    def resolve_state_id(self, project_id: str, state_name: str) -> str:
        """Resolve a project state name to its UUID (case-insensitive)."""
        name = state_name.strip()
        if not name:
            raise PlaneError("state name is required")
        cache_key = (project_id, name.lower())
        cached = self._state_cache.get(cache_key)
        if cached is not None:
            return cached

        matches = [
            state_id
            for raw in self._iter_states(project_id)
            if (state_id := _state_id(raw))
            and _as_str(raw.get("name"), default="").lower() == name.lower()
        ]
        unique = list(dict.fromkeys(matches))
        if not unique:
            raise PlaneNotFoundError(
                f"Plane state {name!r} was not found in the project",
                status=404,
                method="GET",
                path=self._safe_path(self._states_path(project_id)),
            )
        if len(unique) > 1:
            raise PlaneAPIError(
                f"Plane state {name!r} is ambiguous in the project",
                method="GET",
                path=self._safe_path(self._states_path(project_id)),
            )
        self._state_cache[cache_key] = unique[0]
        return unique[0]

    def update_work_item_state(
        self,
        project_id: str,
        work_item_id: str,
        state_id: str,
    ) -> PlaneWorkItem:
        """Move a work item to ``state_id`` (v1 body field ``state``)."""
        target = state_id.strip()
        if not target:
            raise PlaneError("state_id is required")
        raw = self._json(
            "PATCH",
            self._work_item_path(project_id, work_item_id),
            payload={"state": target},
        )
        parsed = parse_work_item(raw, default_project_id=project_id)
        if parsed is None:
            # Some Plane versions return an empty body on PATCH; re-fetch.
            return self.get_work_item(project_id, work_item_id)
        return parsed

    def _list_projects(self) -> list[PlaneProject]:
        projects: list[PlaneProject] = []
        for raw in self._paginate(self._projects_path(), {"per_page": str(_DEFAULT_PER_PAGE)}):
            parsed = parse_project(raw)
            if parsed is not None:
                projects.append(parsed)
        return projects

    def _iter_states(self, project_id: str) -> Iterator[dict[str, object]]:
        path = self._states_path(project_id)
        params = {"per_page": "100"}
        payload = self._json("GET", path, query=params)
        records, next_cursor = _page_records(payload)
        if records or (isinstance(payload, dict) and isinstance(payload.get("results"), list)):
            yield from records
            if not next_cursor:
                return
            params_with_cursor = dict(params)
            seen = {next_cursor}
            cursor: str = next_cursor
            for _ in range(_MAX_PAGES - 1):
                params_with_cursor["cursor"] = cursor
                page = self._json("GET", path, query=params_with_cursor)
                more, following = _page_records(page)
                yield from more
                if not following or following in seen:
                    return
                seen.add(following)
                cursor = following
            raise PlaneAPIError(
                "Plane listing exceeded the pagination safety limit",
                method="GET",
                path=self._safe_path(path),
            )
        yield from _iter_state_records(payload)

    def _paginate(
        self,
        path: str,
        params: dict[str, str],
    ) -> Iterator[dict[str, object]]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_PAGES):
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            payload = self._json("GET", path, query=query)
            records, next_cursor = _page_records(payload)
            yield from records
            if not next_cursor or next_cursor in seen_cursors:
                return
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise PlaneAPIError(
            "Plane listing exceeded the pagination safety limit",
            method="GET",
            path=self._safe_path(path),
        )

    def _json(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> object:
        url = self._url(path, query)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "X-API-Key": self._api_key,
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        status, raw = self._send(method, url, headers, body)
        safe_path = self._safe_path(path)
        if status in {401, 403}:
            raise PlaneAuthError(
                self._error_message(status, raw, "Plane authentication failed"),
                status=status,
                method=method,
                path=safe_path,
            )
        if status == 404:
            raise PlaneNotFoundError(
                self._error_message(status, raw, "Plane resource not found"),
                status=status,
                method=method,
                path=safe_path,
            )
        if status >= 400:
            raise PlaneAPIError(
                self._error_message(status, raw, f"Plane request failed ({status})"),
                status=status,
                method=method,
                path=safe_path,
            )
        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PlaneAPIError(
                f"Plane returned non-JSON for {method} {safe_path}",
                status=status,
                method=method,
                path=safe_path,
            ) from None

    def _send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, bytes]:
        transport = self._transport
        if transport is None:
            opener = self._opener or urllib.request.build_opener()
            transport = _UrllibTransport(opener)
        try:
            return transport.request(method, url, headers, body, self._timeout)
        except (PlaneError, AssertionError):
            raise
        except Exception as exc:
            detail = sanitize_error_text(str(exc), secret=self._api_key)
            raise PlaneError(f"Plane request failed: {detail}") from None

    def _url(self, path: str, query: Mapping[str, str] | None = None) -> str:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return url

    def _safe_path(self, path: str) -> str:
        return path.split("?", 1)[0]

    def _work_items_path(self, project_id: str) -> str:
        return (
            f"{_API_PREFIX}/workspaces/{_seg(self.workspace_slug)}"
            f"/projects/{_seg(project_id)}/work-items/"
        )

    def _work_item_path(self, project_id: str, work_item_id: str) -> str:
        return f"{self._work_items_path(project_id)}{_seg(work_item_id)}/"

    def _comments_path(self, project_id: str, work_item_id: str) -> str:
        return f"{self._work_item_path(project_id, work_item_id)}comments/"

    def _comment_path(self, project_id: str, work_item_id: str, comment_id: str) -> str:
        return f"{self._comments_path(project_id, work_item_id)}{_seg(comment_id)}/"

    def _projects_path(self) -> str:
        return f"{_API_PREFIX}/workspaces/{_seg(self.workspace_slug)}/projects/"

    def _states_path(self, project_id: str) -> str:
        return (
            f"{_API_PREFIX}/workspaces/{_seg(self.workspace_slug)}"
            f"/projects/{_seg(project_id)}/states/"
        )

    def _error_message(self, status: int, raw: bytes, fallback: str) -> str:
        snippet = sanitize_error_text(
            _error_snippet(raw) or fallback,
            secret=self._api_key,
        )
        return (
            f"{fallback} (HTTP {status}): {snippet}"
            if snippet != fallback
            else (f"{fallback} (HTTP {status})")
        )


def normalize_plane_url(url: str) -> str:
    """Normalize a Plane app or API root to ``scheme://host[:port][/prefix]``."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PlaneError("base_url must be an absolute http(s) URL with a host")
    if parsed.username or parsed.password:
        raise PlaneError("base_url must not contain credentials")
    path = parsed.path.rstrip("/")
    for suffix in ("/api/v2", "/api/v1", "/api"):
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def sanitize_error_text(text: str, *, secret: str) -> str:
    """Redact credentials and truncate so logs/exceptions stay safe."""
    cleaned = text
    if secret:
        cleaned = cleaned.replace(secret, "<redacted>")
    cleaned = _HEADER_SECRET_RE.sub(r"\1: <redacted>", cleaned)
    cleaned = cleaned.replace("\n", " ").strip()
    if len(cleaned) > _MAX_ERROR_BODY:
        return cleaned[:_MAX_ERROR_BODY] + "…"
    return cleaned


def parse_project(value: object) -> PlaneProject | None:
    """Parse a project object; return None when the payload is unusable."""
    if not isinstance(value, dict):
        return None
    raw = {str(key): item for key, item in value.items()}
    project_id = _as_str(raw.get("id"))
    if not project_id:
        return None
    return PlaneProject(
        id=project_id,
        identifier=_as_str(raw.get("identifier")),
        name=_as_str(raw.get("name"), default=""),
        raw=raw,
    )


def parse_work_item(
    value: object,
    *,
    default_project_id: str | None = None,
) -> PlaneWorkItem | None:
    """Parse a work-item object; return None when the payload is unusable."""
    if not isinstance(value, dict):
        return None
    raw = {str(key): item for key, item in value.items()}
    item_id = _as_str(raw.get("id"))
    if not item_id:
        return None
    project_id = (
        _nested_id(raw.get("project_id"))
        or _nested_id(raw.get("project"))
        or (default_project_id.strip() if default_project_id else None)
    )
    if not project_id:
        return None
    state_id, state_name = _parse_state_fields(raw)
    return PlaneWorkItem(
        id=item_id,
        project_id=project_id,
        name=_as_str(raw.get("name"), default=""),
        identifier=_as_str(raw.get("identifier")),
        sequence_id=_as_int(raw.get("sequence_id")),
        description=_as_str(raw.get("description_stripped"))
        or _as_str(raw.get("description"), default=""),
        description_html=_as_str(raw.get("description_html"), default=""),
        state_id=state_id,
        state_name=state_name,
        priority=_as_str(raw.get("priority")),
        raw=raw,
    )


def parse_comment(value: object) -> PlaneComment | None:
    """Parse a comment object; return None when the payload is unusable."""
    if not isinstance(value, dict):
        return None
    raw = {str(key): item for key, item in value.items()}
    comment_id = _as_str(raw.get("id"))
    if not comment_id:
        return None
    html = _as_str(raw.get("comment_html"), default="")
    body = _as_str(raw.get("comment_stripped")) or _strip_html(html)
    return PlaneComment(id=comment_id, body=body, html=html, raw=raw)


def _page_records(payload: object) -> tuple[list[dict[str, object]], str | None]:
    if isinstance(payload, list):
        return [_dict_item(item) for item in payload if isinstance(item, dict)], None
    if not isinstance(payload, dict):
        return [], None
    results = payload.get("results")
    records: list[dict[str, object]] = []
    if isinstance(results, list):
        records = [_dict_item(item) for item in results if isinstance(item, dict)]
    next_cursor: str | None = None
    if payload.get("next_page_results"):
        cursor = payload.get("next_cursor")
        if isinstance(cursor, str) and cursor.strip():
            next_cursor = cursor.strip()
    return records, next_cursor


def _iter_state_records(payload: object) -> Iterator[dict[str, object]]:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield _dict_item(item)
        return
    if not isinstance(payload, dict):
        return
    results = payload.get("results")
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict):
                yield _dict_item(item)
        return
    for value in payload.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and _as_str(item.get("id")):
                    yield _dict_item(item)


def _parse_state_fields(raw: dict[str, object]) -> tuple[str | None, str | None]:
    state = raw.get("state")
    explicit_id = _nested_id(raw.get("state_id"))
    if isinstance(state, dict):
        return explicit_id or _nested_id(state), _as_str(state.get("name"))
    if isinstance(state, str):
        if explicit_id is not None:
            return explicit_id, state
        return state, None
    return explicit_id, None


def _state_mismatch(
    item: PlaneWorkItem,
    state_id: str,
    ready_state: str | None,
) -> bool:
    if item.state_id != state_id:
        return True
    if ready_state and item.state_name and item.state_name.lower() != ready_state.lower():
        return True
    return False


def _state_id(raw: dict[str, object]) -> str | None:
    return _nested_id(raw.get("id"))


def _relation_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            name = (
                _as_str(item.get("display_name"))
                or _as_str(item.get("name"))
                or _as_str(item.get("email"))
                or _as_str(item.get("id"))
            )
            if name:
                names.append(name)
    return tuple(names)


def _error_snippet(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        parsed: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace")[:_MAX_ERROR_BODY]
    if isinstance(parsed, dict):
        for key in ("detail", "error", "message"):
            text = parsed.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _looks_like_html(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("<") and ">" in stripped


def _plain_to_html(text: str) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    paragraphs = [part.replace("\n", "<br>") for part in escaped.split("\n\n")]
    return "".join(f"<p>{part}</p>" for part in paragraphs) or "<p></p>"


def _strip_html(html: str) -> str:
    return _HTML_TAG_RE.sub("", html).strip()


def _page_size(per_page: int) -> int:
    if per_page <= 0:
        return _DEFAULT_PER_PAGE
    return min(per_page, _DEFAULT_PER_PAGE)


def _seg(value: str) -> str:
    return urllib.parse.quote(value.strip(), safe="")


def _dict_item(value: dict[object, object]) -> dict[str, object]:
    return {str(key): item for key, item in value.items()}


@overload
def _as_str(value: object, default: str) -> str: ...


@overload
def _as_str(value: object, default: None = None) -> str | None: ...


def _as_str(value: object, default: str | None = None) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _nested_id(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        return _as_str(value.get("id"))
    return None
