"""Durable webhook intake and database-backed factory workers."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol

from simplecode.factory import Factory, sanitize_factory_error
from simplecode.factory_config import FactoryConfig, FactoryProjectMapping
from simplecode.factory_progress import FactoryProgressTracker
from simplecode.github import CommandRunner, SubprocessCommandRunner, sanitize_git_text
from simplecode.store import FactoryJobRow, FactoryTaskRow, SessionStore

logger = logging.getLogger(__name__)

_MAX_WEBHOOK_BODY = 1024 * 1024
_PLANE_EVENT = "workitem.updated"
_GITHUB_EVENTS = frozenset({"pull_request", "pull_request_review", "check_suite"})


class WebhookInputError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


def verify_plane_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature)


def verify_github_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature)


def _json_object(body: bytes) -> dict[str, object]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookInputError(HTTPStatus.BAD_REQUEST, "invalid JSON") from exc
    if not isinstance(value, dict):
        raise WebhookInputError(HTTPStatus.BAD_REQUEST, "payload must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WebhookInputError(HTTPStatus.BAD_REQUEST, f"missing {field}")
    return value.strip()


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _nested_text(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, dict):
        return _optional_text(value.get("id"))
    return _optional_text(value)


class FactoryWebhookIntake:
    """Validate signed deliveries and retain only minimal routing metadata."""

    def __init__(self, config: FactoryConfig, store: SessionStore) -> None:
        self.config = config
        self.store = store

    def accept_plane(self, headers: dict[str, str], body: bytes, secret: str) -> bool:
        signature = headers.get("x-plane-signature", "")
        if not verify_plane_signature(body, signature, secret):
            raise WebhookInputError(HTTPStatus.UNAUTHORIZED, "invalid signature")
        payload = _json_object(body)
        if payload.get("version") != "v2":
            raise WebhookInputError(HTTPStatus.BAD_REQUEST, "Plane webhook version must be v2")
        event_type = _required_text(payload.get("event"), "event")
        header_event = headers.get("x-plane-event", "")
        if header_event and header_event != event_type:
            raise WebhookInputError(HTTPStatus.BAD_REQUEST, "event header mismatch")
        event_id = _required_text(payload.get("event_id"), "event_id")
        delivery_id = _required_text(payload.get("delivery_id"), "delivery_id")
        entity_id = _required_text(payload.get("entity_id"), "entity_id")
        if event_type != _PLANE_EVENT:
            return False
        raw_data = payload.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        project_id = _nested_text(data, "project_id") or _nested_text(data, "project")
        if project_id is None:
            raise WebhookInputError(HTTPStatus.BAD_REQUEST, "missing data.project_id")
        metadata: dict[str, object] = {
            "project_id": project_id,
            "work_item_id": entity_id,
            "workspace_id": _optional_text(payload.get("workspace_id")) or "",
        }
        return self.store.record_factory_delivery(
            source="plane",
            source_event_id=event_id,
            delivery_id=delivery_id,
            event_type=event_type,
            subject_id=entity_id,
            project_id=project_id,
            repo=None,
            payload_sha256=hashlib.sha256(body).hexdigest(),
            task_type="factory.delivery.run",
            metadata=metadata,
        )

    def accept_github(self, headers: dict[str, str], body: bytes, secret: str) -> bool:
        signature = headers.get("x-hub-signature-256", "")
        if not verify_github_signature(body, signature, secret):
            raise WebhookInputError(HTTPStatus.UNAUTHORIZED, "invalid signature")
        payload = _json_object(body)
        event_type = _required_text(headers.get("x-github-event"), "X-GitHub-Event")
        delivery_id = _required_text(headers.get("x-github-delivery"), "X-GitHub-Delivery")
        if event_type not in _GITHUB_EVENTS:
            return False
        repo_obj = payload.get("repository")
        repo_data = repo_obj if isinstance(repo_obj, dict) else {}
        repo = _required_text(repo_data.get("full_name"), "repository.full_name")
        pr_number = _github_pr_number(payload, event_type)
        if pr_number is None:
            return False
        metadata = {
            "repo": repo,
            "pr_number": pr_number,
            "event_type": event_type,
            "action": _optional_text(payload.get("action")) or "",
        }
        return self.store.record_factory_delivery(
            source="github",
            source_event_id=delivery_id,
            delivery_id=delivery_id,
            event_type=event_type,
            subject_id=str(pr_number),
            project_id=None,
            repo=repo,
            payload_sha256=hashlib.sha256(body).hexdigest(),
            task_type="factory.github.sync",
            metadata=metadata,
        )


def _github_pr_number(payload: dict[str, object], event_type: str) -> int | None:
    if event_type in {"pull_request", "pull_request_review"}:
        pr = payload.get("pull_request")
        pr_number = pr.get("number") if isinstance(pr, dict) else None
        if isinstance(pr_number, int) and not isinstance(pr_number, bool):
            return pr_number
        payload_number = payload.get("number")
        if isinstance(payload_number, int) and not isinstance(payload_number, bool):
            return payload_number
        return None
    suite = payload.get("check_suite")
    if not isinstance(suite, dict):
        return None
    pulls = suite.get("pull_requests")
    if not isinstance(pulls, list) or not pulls or not isinstance(pulls[0], dict):
        return None
    first = pulls[0]
    assert isinstance(first, dict)
    number = first.get("number")
    return number if isinstance(number, int) and not isinstance(number, bool) else None


class _WebhookHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FactoryWebhookServer:
    """Small signed-webhook server intended to sit behind an HTTPS reverse proxy."""

    def __init__(self, config: FactoryConfig, database_url: str) -> None:
        self.config = config
        self.database_url = database_url
        self._server: _WebhookHTTPServer | None = None

    def serve_forever(self) -> None:
        plane_secret = self.config.events.resolve_secret("plane")
        github_secret = self.config.events.resolve_secret("github")
        config = self.config
        database_url = self.database_url

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/health/live":
                    self._reply(HTTPStatus.OK, {"status": "ok"})
                    return
                if self.path == "/health/ready":
                    store: SessionStore | None = None
                    try:
                        store = SessionStore(database_url)
                        store.list_factory_tasks(limit=1)
                    except Exception:
                        self._reply(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "offline"})
                    else:
                        self._reply(HTTPStatus.OK, {"status": "ready"})
                    finally:
                        if store is not None:
                            store.close()
                    return
                self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_POST(self) -> None:
                if self.path not in {"/webhooks/plane", "/webhooks/github"}:
                    self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._reply(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
                    return
                if length < 1 or length > _MAX_WEBHOOK_BODY:
                    status = (
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                        if length > _MAX_WEBHOOK_BODY
                        else HTTPStatus.BAD_REQUEST
                    )
                    self._reply(status, {"error": "invalid body size"})
                    return
                body = self.rfile.read(length)
                headers = {key.lower(): value for key, value in self.headers.items()}
                store: SessionStore | None = None
                try:
                    store = SessionStore(database_url)
                    intake = FactoryWebhookIntake(config, store)
                    if self.path.endswith("/plane"):
                        created = intake.accept_plane(headers, body, plane_secret)
                    else:
                        created = intake.accept_github(headers, body, github_secret)
                except WebhookInputError as exc:
                    self._reply(exc.status, {"error": str(exc)})
                    return
                except Exception:
                    logger.exception("Webhook intake failed")
                    self._reply(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "intake unavailable"})
                    return
                finally:
                    if store is not None:
                        store.close()
                self._reply(
                    HTTPStatus.ACCEPTED,
                    {"status": "queued" if created else "duplicate_or_ignored"},
                )

            def _reply(self, status: int, payload: dict[str, str]) -> None:
                encoded = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                logger.debug("Webhook HTTP %s", format % args)

        self._server = _WebhookHTTPServer(
            (self.config.events.bind, self.config.events.port), Handler
        )
        logger.info(
            "Factory webhook API listening on http://%s:%d",
            self.config.events.bind,
            self.config.events.port,
        )
        try:
            self._server.serve_forever(poll_interval=0.5)
        finally:
            self._server.server_close()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()


@dataclass(frozen=True)
class GitHubPullRequestState:
    state: str
    review: str
    ci: str
    url: str


class GitHubStateReader(Protocol):
    def pull_request_state(self, repo: str, number: int) -> GitHubPullRequestState: ...


class GitHubCLIStateReader:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or SubprocessCommandRunner()

    def pull_request_state(self, repo: str, number: int) -> GitHubPullRequestState:
        result = self._runner.run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "state,isDraft,reviewDecision,statusCheckRollup,mergedAt,url",
            ],
            cwd=Path.cwd(),
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(sanitize_git_text(result.stderr or result.stdout))
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise RuntimeError("gh returned an invalid pull-request response")
        raw_state = str(data.get("state", "")).upper()
        if data.get("mergedAt") or raw_state == "MERGED":
            state = "merged"
        elif raw_state == "CLOSED":
            state = "closed"
        elif data.get("isDraft"):
            state = "draft"
        else:
            state = "open"
        review = str(data.get("reviewDecision") or "pending").lower()
        ci = _aggregate_checks(data.get("statusCheckRollup"))
        return GitHubPullRequestState(
            state=state,
            review=review,
            ci=ci,
            url=str(data.get("url") or ""),
        )


def _aggregate_checks(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "unknown"
    failed = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
    pending = {"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED"}
    states: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            states.add(str(item.get("conclusion") or item.get("state") or "").upper())
    if states & failed:
        return "failed"
    if states & pending or "" in states:
        return "pending"
    return "passed"


class _LeaseHeartbeat:
    def __init__(
        self,
        database_url: str,
        task: FactoryTaskRow,
        worker_id: str,
        lease_seconds: int,
        repo: str | None,
    ) -> None:
        self.database_url = database_url
        self.task = task
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.repo = repo
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _LeaseHeartbeat:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        interval = max(1.0, self.lease_seconds / 4)
        while not self._stop.wait(interval):
            store: SessionStore | None = None
            try:
                store = SessionStore(self.database_url)
                task_ok = store.renew_factory_task(
                    self.task.task_id,
                    self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
                repo_ok = True
                if self.repo is not None:
                    repo_ok = store.renew_repo_lease(
                        self.repo,
                        self.worker_id,
                        self.task.task_id,
                        lease_seconds=self.lease_seconds,
                    )
                if not task_ok or not repo_ok:
                    self.lost.set()
                    return
            except Exception:
                logger.exception("Factory lease heartbeat failed")
                self.lost.set()
                return
            finally:
                if store is not None:
                    store.close()


class EventFactoryWorker:
    """Claim durable tasks and route them to idempotent factory handlers."""

    def __init__(
        self,
        config: FactoryConfig,
        plane: object,
        store: SessionStore,
        *,
        factory: Factory | None = None,
        github: GitHubStateReader | None = None,
        repos_dir: Path | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.config = config
        self.plane = plane
        self.store = store
        self.factory = factory or Factory(config, plane, store, repos_dir=repos_dir)  # type: ignore[arg-type]
        self.github = github or GitHubCLIStateReader()
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

    def run_once(self) -> bool:
        task = self.store.claim_factory_task(
            self.worker_id, lease_seconds=self.config.events.task_lease_seconds
        )
        if task is None:
            return False
        repo = task.repo or self._task_repo(task)
        if repo is not None and not self.store.acquire_repo_lease(
            repo,
            self.worker_id,
            task.task_id,
            lease_seconds=self.config.events.task_lease_seconds,
        ):
            self.store.defer_factory_task(task.task_id, self.worker_id, delay_seconds=5)
            return True
        heartbeat = _LeaseHeartbeat(
            self.store.database_url,
            task,
            self.worker_id,
            self.config.events.task_lease_seconds,
            repo,
        )
        try:
            with heartbeat:
                self._handle(task, heartbeat)
            if heartbeat.lost.is_set():
                raise RuntimeError("factory task lease lost")
            self.store.complete_factory_task(task.task_id, self.worker_id)
        except Exception as exc:
            error = sanitize_factory_error(exc)
            delay = min(
                300.0,
                self.config.events.retry_base_seconds * (2 ** max(0, task.attempt_count - 1)),
            )
            delay *= random.uniform(0.8, 1.2)
            self.store.fail_factory_task(
                task.task_id,
                self.worker_id,
                error,
                max_attempts=self.config.events.max_attempts,
                delay_seconds=delay,
            )
            logger.error("Factory task %s failed: %s", task.task_id, error)
        finally:
            if repo is not None:
                self.store.release_repo_lease(repo, self.worker_id, task.task_id)
        return True

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        try:
            while stop_event is None or not stop_event.is_set():
                if not self.run_once():
                    if stop_event is not None:
                        stop_event.wait(1)
                    else:
                        time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Factory worker stopped")

    def _task_repo(self, task: FactoryTaskRow) -> str | None:
        if task.task_type != "factory.delivery.run":
            return _optional_text(task.metadata.get("repo"))
        project_id = _required_text(task.metadata.get("project_id"), "project_id")
        mapping = self._mapping_for_project(project_id)
        return None if mapping is None else mapping.repo

    def _mapping_for_project(self, project_id: str) -> FactoryProjectMapping | None:
        direct = self.config.mapping_for(project_id=project_id)
        if direct is not None:
            return direct
        for mapping in self.config.projects:
            if self.plane.resolve_project_id(mapping.key) == project_id:  # type: ignore[attr-defined]
                return mapping
        return None

    def _handle(self, task: FactoryTaskRow, heartbeat: _LeaseHeartbeat) -> None:
        if task.task_type == "factory.delivery.run":
            project_id = _required_text(task.metadata.get("project_id"), "project_id")
            work_item_id = _required_text(task.metadata.get("work_item_id"), "work_item_id")
            self.factory.process_work_item(
                project_id,
                work_item_id,
                allow_existing_claim=True,
                cancel_check=heartbeat.lost.is_set,
                defer_plane_review=True,
            )
            return
        if task.task_type == "factory.github.sync":
            self._sync_github(task)
            return
        if task.task_type == "factory.plane.review.sync":
            self._sync_plane_review(task)
            return
        if task.task_type == "factory.workspace.cleanup":
            if task.external_key is None:
                raise RuntimeError("Workspace cleanup task is missing external_key")
            attempt = task.metadata.get("attempt")
            if not isinstance(attempt, int) or attempt < 1:
                raise RuntimeError("Workspace cleanup task is missing a valid attempt")
            self.factory.cleanup_workspace_attempt(task.external_key, attempt)
            return
        raise RuntimeError(f"unknown factory task type: {task.task_type}")

    def _sync_github(self, task: FactoryTaskRow) -> None:
        repo = _required_text(task.metadata.get("repo"), "repo")
        number_value = task.metadata.get("pr_number")
        if not isinstance(number_value, int):
            raise RuntimeError("GitHub task is missing pr_number")
        job = self.store.find_factory_job_by_pr(repo, number_value)
        if job is None:
            logger.info("Ignoring GitHub event for unknown PR %s#%d", repo, number_value)
            return
        state = self.github.pull_request_state(repo, number_value)
        self.store.update_factory_job(
            job.external_key,
            job.status,
            pr_state=state.state,
            review_state=state.review,
            ci_state=state.ci,
        )
        tracker = FactoryProgressTracker(
            self.store,
            self.plane,  # type: ignore[arg-type]
            job.external_key,
            job.project_id,
            job.work_item_id,
        )
        tracker.milestone(
            "github_status_updated",
            "github",
            status=job.status,
            message=f"PR {state.state}; review {state.review}; CI {state.ci}",
        )
        if state.state == "merged":
            self._move_plane(job, self.config.states.done)
            tracker.milestone(
                "pull_request_merged", "completed", status=job.status, message="PR merged"
            )
        elif state.state == "closed":
            self._move_plane(job, self.config.states.needs_attention)
            tracker.milestone(
                "pull_request_closed",
                "needs_attention",
                status=job.status,
                message="PR closed without merge",
            )

    def _sync_plane_review(self, task: FactoryTaskRow) -> None:
        if task.external_key is None:
            raise RuntimeError("Plane review sync task is missing external_key")
        job = self.store.load_factory_job(task.external_key)
        self._move_plane(job, self.config.states.in_review)
        self.store.update_factory_job(
            job.external_key,
            "completed",
            completed_at=datetime.now(UTC),
        )

    def _move_plane(self, job: FactoryJobRow, state_name: str) -> None:
        state_id = self.plane.resolve_state_id(job.project_id, state_name)  # type: ignore[attr-defined]
        self.plane.update_work_item_state(  # type: ignore[attr-defined]
            job.project_id, job.work_item_id, state_id
        )


class FactoryReconciler:
    """Backstop missed Plane deliveries without executing work in the reconciler."""

    def __init__(self, config: FactoryConfig, plane: object, store: SessionStore) -> None:
        self.config = config
        self.plane = plane
        self.store = store

    def run_once(self) -> int:
        queued = 0
        for mapping in self.config.projects:
            project_id = self.plane.resolve_project_id(mapping.key)  # type: ignore[attr-defined]
            items = self.plane.list_ready_work_items(  # type: ignore[attr-defined]
                project_id, ready_state=self.config.states.ready
            )
            for item in items:
                if self.store.enqueue_factory_task(
                    "factory.delivery.run",
                    f"reconcile:{self.config.plane.workspace_slug}:{project_id}:{item.id}",
                    repo=mapping.repo,
                    metadata={"project_id": project_id, "work_item_id": item.id},
                ):
                    queued += 1
        for job in self.store.list_factory_jobs(status="published", limit=1000):
            if self.store.enqueue_factory_task(
                "factory.plane.review.sync",
                f"plane-review:{job.external_key}",
                external_key=job.external_key,
                repo=job.repo,
            ):
                queued += 1
        for workspace in self.store.find_factory_workspace_cleanup_candidates(limit=1000):
            job = self.store.load_factory_job(workspace.external_key)
            if self.store.enqueue_factory_task(
                "factory.workspace.cleanup",
                f"workspace-cleanup:{workspace.external_key}:{workspace.attempt}",
                external_key=workspace.external_key,
                repo=job.repo,
                metadata={"attempt": workspace.attempt},
            ):
                queued += 1
        return queued

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    self.run_once()
                except Exception:
                    logger.exception("Factory reconciliation failed")
                if stop_event is not None:
                    stop_event.wait(self.config.events.reconcile_interval)
                else:
                    time.sleep(self.config.events.reconcile_interval)
        except KeyboardInterrupt:
            logger.info("Factory reconciler stopped")
