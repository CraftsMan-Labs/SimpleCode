"""Embedded, read-only HTTP dashboard for factory progress."""

# ruff: noqa: E501

from __future__ import annotations

import json
import re
import socket
import threading
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from secrets import token_urlsafe
from typing import Any
from urllib.parse import unquote, urlsplit

from simplecode.errors import StoreError
from simplecode.plane import sanitize_error_text
from simplecode.store import FactoryEventRow, FactoryJobRow, SessionStore

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_RECENT_LIMIT = 100
_EVENT_LIMIT = 500
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|api[-_ ]?key|token|password|secret)"
    r"(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_URL_CREDENTIAL_RE = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _safe_http_url(value: str | None) -> str | None:
    """Return an absolute credential-free HTTP(S) URL, or no link."""
    if not value or any(unicodedata.category(char) == "Cc" for char in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    return value


def _sanitize_last_error(value: str | None) -> str | None:
    """Defensively clean the already-sanitized factory error for display."""
    if not value:
        return None
    cleaned = _URL_CREDENTIAL_RE.sub(r"\1<redacted>@", value)
    cleaned = _SECRET_ASSIGNMENT_RE.sub(r"\1\2<redacted>", cleaned)
    cleaned = sanitize_error_text(cleaned, secret="")
    cleaned = "".join(
        " " if unicodedata.category(char).startswith("C") else char for char in cleaned
    )
    cleaned = " ".join(cleaned.split())
    return (cleaned[:1000] + "…") if len(cleaned) > 1000 else cleaned


def _job_payload(job: FactoryJobRow) -> dict[str, object]:
    """Serialize only fields approved for the read-only dashboard."""
    return {
        "external_key": job.external_key,
        "workspace_slug": job.workspace_slug,
        "project_id": job.project_id,
        "project_key": job.project_key,
        "work_item_id": job.work_item_id,
        "work_item_ref": job.work_item_ref,
        "title": job.title,
        "status": job.status,
        "repo": job.repo,
        "branch": job.branch,
        "session_id": job.session_id,
        "pr_url": _safe_http_url(job.pr_url),
        "last_error": _sanitize_last_error(job.last_error),
        "current_stage": job.current_stage,
        "current_agent": job.current_agent,
        "current_prompt": job.current_prompt,
        "last_event_at": _iso(job.last_event_at),
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
    }


def _event_payload(event: FactoryEventRow) -> dict[str, object]:
    """Serialize event structure without messages, metadata, or agent output."""
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "stage": event.stage,
        "status": event.status,
        "created_at": _iso(event.created_at),
    }


def _snapshot_payload(store: SessionStore, limit: int) -> dict[str, object]:
    jobs = store.list_factory_jobs(limit=limit)
    counts = Counter(job.status for job in jobs)
    active = sum(
        count
        for status, count in counts.items()
        if status not in {"completed", "failed", "cancelled"}
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "total": len(jobs),
            "active": active,
            "completed": counts.get("completed", 0),
            "failed": counts.get("failed", 0),
            "by_status": dict(sorted(counts.items())),
        },
        "jobs": [_job_payload(job) for job in jobs],
    }


class _DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        database_url: str,
        recent_limit: int,
    ) -> None:
        self.database_url = database_url
        self.recent_limit = recent_limit
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, _DashboardRequestHandler)
        bound_host = str(self.server_address[0])
        bound_port = int(self.server_address[1])
        self.expected_host = (
            f"[{bound_host}]:{bound_port}" if ":" in bound_host else f"{bound_host}:{bound_port}"
        )


class _DashboardRequestHandler(BaseHTTPRequestHandler):
    server: _DashboardHTTPServer
    protocol_version = "HTTP/1.1"

    def __getattr__(self, name: str) -> Any:
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def do_GET(self) -> None:  # noqa: N802
        self._serve(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(send_body=False)

    def do_POST(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_CONNECT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_TRACE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def _serve(self, *, send_body: bool) -> None:
        if not self._validate_host(send_body=send_body):
            return
        store: SessionStore | None = None
        try:
            store = SessionStore(self.server.database_url)
            self._route(store, send_body=send_body)
        except Exception:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "dashboard data unavailable"},
                send_body=send_body,
            )
        finally:
            if store is not None:
                store.close()

    def _route(self, store: SessionStore, *, send_body: bool) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            nonce = token_urlsafe(18)
            page = _dashboard_page(nonce).encode("utf-8")
            self._respond(
                HTTPStatus.OK,
                page,
                "text/html; charset=utf-8",
                send_body=send_body,
                content_security_policy=(
                    "default-src 'none'; "
                    f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
                    "connect-src 'self'; img-src 'self'; "
                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                ),
            )
            return
        if path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok"}, send_body=send_body)
            return
        if path == "/api/snapshot":
            self._json(
                HTTPStatus.OK,
                _snapshot_payload(store, self.server.recent_limit),
                send_body=send_body,
            )
            return

        prefix = "/api/jobs/"
        if path.startswith(prefix) and "/" not in path[len(prefix) :]:
            encoded_key = path[len(prefix) :]
            if not encoded_key:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"}, send_body=send_body)
                return
            try:
                external_key = unquote(encoded_key, errors="strict")
            except UnicodeDecodeError:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid job key encoding"},
                    send_body=send_body,
                )
                return
            if not external_key or any(
                unicodedata.category(char).startswith("C") for char in external_key
            ):
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid job key"},
                    send_body=send_body,
                )
                return
            try:
                job = store.load_factory_job(external_key)
            except StoreError:
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "job not found"},
                    send_body=send_body,
                )
                return
            events = store.list_factory_events(external_key, limit=_EVENT_LIMIT, newest=True)
            self._json(
                HTTPStatus.OK,
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "job": _job_payload(job),
                    "events": [_event_payload(event) for event in events],
                },
                send_body=send_body,
            )
            return

        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"}, send_body=send_body)

    def _method_not_allowed(self) -> None:
        if not self._validate_host(send_body=True):
            return
        self._json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": "method not allowed"},
            send_body=True,
            extra_headers={"Allow": "GET, HEAD"},
        )

    def _validate_host(self, *, send_body: bool) -> bool:
        host_headers = self.headers.get_all("Host", [])
        if len(host_headers) == 1 and host_headers[0] == self.server.expected_host:
            return True
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid host header"},
            send_body=send_body,
        )
        return False

    def _json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        send_body: bool,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._respond(
            status,
            body,
            "application/json; charset=utf-8",
            send_body=send_body,
            extra_headers=extra_headers,
        )

    def _respond(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        send_body: bool,
        content_security_policy: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_security_policy:
            self.send_header("Content-Security-Policy", content_security_policy)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the embedding CLI's output free of per-poll access logs."""


class DashboardServer:
    """Start and stop an embedded factory dashboard in a background thread."""

    def __init__(
        self,
        database_url: str,
        host: str = _DEFAULT_HOST,
        port: int = 0,
        *,
        recent_limit: int = _DEFAULT_RECENT_LIMIT,
    ) -> None:
        if not host:
            raise ValueError("host must not be empty")
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if recent_limit < 1:
            raise ValueError("recent_limit must be positive")
        self.database_url = database_url
        self.bind_host = host
        self.requested_port = port
        self.recent_limit = recent_limit
        self._server: _DashboardHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        if self._server is None:
            return self.bind_host
        return str(self._server.server_address[0])

    @property
    def port(self) -> int:
        """Actual bound port; available after ``start()``, including for port 0."""
        if self._server is None:
            return self.requested_port
        return int(self._server.server_address[1])

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def start(self) -> DashboardServer:
        if self.is_running:
            return self
        server = _DashboardHTTPServer(
            (self.bind_host, self.requested_port),
            self.database_url,
            self.recent_limit,
        )
        thread = threading.Thread(
            target=server.serve_forever,
            name="simplecode-dashboard",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        try:
            thread.start()
        except BaseException:
            server.server_close()
            self._server = None
            self._thread = None
            raise
        return self

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        if server is None:
            return
        if thread is not None and thread.is_alive():
            server.shutdown()
        server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._server = None
        self._thread = None

    def __enter__(self) -> DashboardServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def _dashboard_page(nonce: str) -> str:
    """Return the complete dependency-free dashboard document."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SimpleCode Factory Control</title>
<style nonce="{nonce}">
:root{{--bg:#090c0e;--panel:#111619;--line:#2a3438;--muted:#829097;--text:#e7ecee;
--amber:#e0a229;--green:#58bd79;--red:#e05b55;--steel:#9fb0b7}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);
font:14px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace}}
body:before{{content:"";position:fixed;inset:0;pointer-events:none;opacity:.12;
background:repeating-linear-gradient(0deg,transparent 0 3px,#fff 4px)}}
.shell{{max-width:1440px;margin:auto;padding:22px}}header{{display:flex;align-items:end;
justify-content:space-between;border-bottom:2px solid var(--amber);padding:0 0 14px}}
h1{{font:700 clamp(24px,4vw,42px)/1 system-ui,sans-serif;letter-spacing:-.04em;margin:0}}
.eyebrow,.label{{color:var(--amber);font-size:11px;letter-spacing:.16em;text-transform:uppercase}}
#connection{{color:var(--green)}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);
gap:12px;margin:16px 0}}.card,.panel{{background:var(--panel);border:1px solid var(--line);
box-shadow:inset 3px 0 var(--steel)}}.card{{padding:14px}}.metric{{font:700 28px/1 system-ui;
margin-top:8px}}main{{display:grid;grid-template-columns:minmax(280px,.8fr) minmax(0,2fr);gap:14px}}
.panel{{padding:16px;min-width:0}}h2{{font:700 16px system-ui;margin:0 0 14px;
text-transform:uppercase;letter-spacing:.08em}}button{{width:100%;text-align:left;color:inherit;
background:#0d1113;border:1px solid var(--line);padding:12px;margin:0 0 8px;cursor:pointer;
font:inherit}}button:hover,button.active{{border-color:var(--amber);background:#181b18}}
.job-head{{display:flex;justify-content:space-between;gap:8px}}.muted{{color:var(--muted)}}
.status{{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--amber)}}
.status.completed{{color:var(--green)}}.status.failed,.error{{color:var(--red)}}
.facts{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:14px 0}}
.fact{{border-top:1px solid var(--line);padding-top:8px;overflow-wrap:anywhere}}
.fact b{{display:block;margin-top:4px}}a{{color:#79b9d4}}.rail{{display:flex;overflow:auto;
gap:5px;margin:18px 0;padding-bottom:5px}}.rail span{{min-width:max-content;color:var(--muted);
border:1px solid var(--line);padding:7px 10px}}.rail .current{{color:#111;background:var(--amber)}}
.rail .done{{color:var(--green);border-color:var(--green)}}.timeline{{border-left:1px solid var(--line);
margin-left:7px;padding-left:18px}}.event{{position:relative;padding:0 0 16px}}.event:before{{
content:"";position:absolute;left:-23px;top:5px;width:8px;height:8px;background:var(--amber);
border:2px solid var(--bg)}}.event strong{{display:block}}.empty{{color:var(--muted);padding:24px 0}}
@media(max-width:850px){{.grid{{grid-template-columns:repeat(2,1fr)}}main{{grid-template-columns:1fr}}
.facts{{grid-template-columns:1fr 1fr}}}}@media(max-width:480px){{.shell{{padding:12px}}
.grid{{grid-template-columns:1fr 1fr}}.facts{{grid-template-columns:1fr}}}}
</style>
</head>
<body><div class="shell">
<header><div><div class="eyebrow">SimpleCode / Operations</div><h1>Factory Control</h1></div>
<div><span id="connection">● CONNECTING</span><br><span class="muted" id="refreshed">—</span></div></header>
<section class="grid" aria-label="Factory overview">
<div class="card"><div class="label">Recent jobs</div><div class="metric" id="total">0</div></div>
<div class="card"><div class="label">Active</div><div class="metric" id="active">0</div></div>
<div class="card"><div class="label">Completed</div><div class="metric" id="completed">0</div></div>
<div class="card"><div class="label">Blocked</div><div class="metric" id="failed">0</div></div>
</section>
<main><section class="panel"><h2>Recent production</h2><div id="jobs"></div></section>
<section class="panel"><h2>Selected work unit</h2><div id="detail" class="empty">
Select a job to inspect its progress.</div></section></main>
</div>
<script nonce="{nonce}">
"use strict";
const $=id=>document.getElementById(id);
const stages=["claim","checkout","explore","build","adversarial","fix","qa","publish","plane_review"];
let selected=null;
const text=(tag,value,cls)=>{{const e=document.createElement(tag);if(cls)e.className=cls;
e.textContent=value??"—";return e}};
const appendFact=(root,label,value)=>{{const box=text("div","", "fact");box.append(text("span",label,"label"),
text("b",value));root.append(box)}};
const safeUrl=value=>{{if(typeof value!=="string"||!value)return null;try{{const u=new URL(value);
return (u.protocol==="http:"||u.protocol==="https:")&&u.username===""&&u.password===""?u.href:null
}}catch(_e){{return null}}}};
function statusClass(s){{return "status "+(s==="completed"?"completed":s==="failed"?"failed":"")}}
function renderJobs(jobs){{const root=$("jobs");root.replaceChildren();
if(!jobs.length){{root.append(text("div","No factory jobs recorded.","empty"));return}}
for(const job of jobs){{const b=document.createElement("button");b.type="button";
if(job.external_key===selected)b.className="active";
const head=text("div","", "job-head");head.append(text("strong",job.work_item_ref||job.title||job.external_key),
text("span",job.status,statusClass(job.status)));b.append(head,
text("div",job.title||job.repo||"Untitled work item","muted"));
b.addEventListener("click",()=>{{selected=job.external_key;renderJobs(jobs);loadDetail()}});root.append(b)}}}}
function renderDetail(data){{const j=data.job,root=$("detail");root.className="";root.replaceChildren();
const top=text("div","", "job-head");top.append(text("h2",j.work_item_ref||j.external_key),
text("span",j.status,statusClass(j.status)));root.append(top,text("div",j.title||"Untitled work item","muted"));
const facts=text("div","", "facts");
appendFact(facts,"Workspace",j.workspace_slug);appendFact(facts,"Project",j.project_key||j.project_id);
appendFact(facts,"Work item",j.work_item_id);appendFact(facts,"Repository",j.repo);
appendFact(facts,"Stage",j.current_stage);appendFact(facts,"Agent / prompt",
(j.current_agent||"—")+(j.current_prompt!==null?" / "+j.current_prompt:""));
appendFact(facts,"Session",j.session_id);appendFact(facts,"Branch",j.branch);
const pr=text("div","", "fact");pr.append(text("span","Draft pull request","label"));
const href=safeUrl(j.pr_url);if(href){{const a=text("a",j.pr_url);a.href=href;a.rel="noopener noreferrer";
a.target="_blank";pr.append(a)}}else pr.append(text("b","—"));facts.append(pr);root.append(facts);
if(j.last_error)root.append(text("div",j.last_error,"error"));
const rail=text("div","", "rail"),current=stages.indexOf(j.current_stage);
stages.forEach((stage,i)=>rail.append(text("span",stage.replace("_"," "),i===current?"current":
(current>=0&&i<current?"done":""))));root.append(rail,text("h2","Event timeline"));
const timeline=text("div","", "timeline");if(!data.events.length)timeline.append(text("div","No events yet.","empty"));
for(const e of data.events){{const item=text("div","", "event");item.append(text("strong",
e.event_type.replaceAll("_"," ")),text("span",[e.stage,e.status].filter(Boolean).join(" · "),"muted"),
text("div",new Date(e.created_at).toLocaleString(),"muted"));timeline.append(item)}}root.append(timeline)}}
async function loadDetail(){{if(!selected)return;try{{const r=await fetch("/api/jobs/"+encodeURIComponent(selected),
{{cache:"no-store"}});if(!r.ok)throw new Error("detail unavailable");renderDetail(await r.json())}}
catch(_e){{$("detail").replaceChildren(text("div","Selected job is unavailable.","error"))}}}}
async function refresh(){{try{{const r=await fetch("/api/snapshot",{{cache:"no-store"}});
if(!r.ok)throw new Error("snapshot unavailable");const data=await r.json();
for(const k of ["total","active","completed","failed"])$(k).textContent=String(data.summary[k]??0);
if(selected&&!data.jobs.some(j=>j.external_key===selected))selected=null;
if(!selected&&data.jobs.length)selected=data.jobs[0].external_key;renderJobs(data.jobs);await loadDetail();
$("connection").textContent="● ONLINE";$("connection").className="";$("refreshed").textContent=
"REFRESHED "+new Date(data.generated_at).toLocaleTimeString()}}
catch(_e){{$("connection").textContent="● DATA OFFLINE";$("connection").className="error";
$("refreshed").textContent="RETRYING"}}}}
refresh();setInterval(refresh,2000);
</script></body></html>"""
