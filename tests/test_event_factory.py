"""Focused tests for durable event-driven factory primitives."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from simplecode.event_factory import (
    EventFactoryWorker,
    FactoryWebhookIntake,
    GitHubPullRequestState,
)
from simplecode.factory_config import (
    FactoryConfig,
    FactoryEventsConfig,
    FactoryProjectMapping,
    FactoryStateNames,
    PlaneConnectionConfig,
)
from simplecode.plane import PlaneComment
from simplecode.store import SessionStore, factory_job_external_key


def _store(path: Path) -> SessionStore:
    store = SessionStore(str(path))
    store.ensure_schema()
    return store


def _config(tmp_path: Path) -> FactoryConfig:
    return FactoryConfig(
        plane=PlaneConnectionConfig("http://plane", "acme", "PLANE_API_KEY"),
        poll_interval=30,
        states=FactoryStateNames(
            ready="Ready for Agent",
            in_progress="In Progress",
            in_review="In Review",
            needs_attention="Needs Attention",
            done="Done",
        ),
        pipeline=tmp_path / "pipeline",
        projects=(FactoryProjectMapping("ENG", "acme/api", "main"),),
        events=FactoryEventsConfig(enabled=True),
    )


def _plane_delivery() -> tuple[dict[str, str], bytes]:
    body = json.dumps(
        {
            "version": "v2",
            "delivery_id": "delivery-1",
            "event_id": "event-1",
            "entity_id": "wi-1",
            "entity_type": "issue",
            "event": "workitem.updated",
            "workspace_id": "workspace-1",
            "data": {"project_id": "project-1", "description": "private ticket body"},
            "previous_attributes": {},
        }
    ).encode()
    signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    return {
        "x-plane-signature": signature,
        "x-plane-event": "workitem.updated",
    }, body


def test_plane_intake_is_deduplicated_and_stores_minimal_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path / "factory.db")
    intake = FactoryWebhookIntake(_config(tmp_path), store)
    headers, body = _plane_delivery()

    assert intake.accept_plane(headers, body, "secret") is True
    assert intake.accept_plane(headers, body, "secret") is False

    delivery = store.load_factory_delivery("plane", "event-1")
    assert delivery.payload_sha256 == hashlib.sha256(body).hexdigest()
    assert delivery.metadata == {
        "project_id": "project-1",
        "work_item_id": "wi-1",
        "workspace_id": "workspace-1",
    }
    assert "private ticket body" not in json.dumps(delivery.metadata)
    tasks = store.list_factory_tasks()
    assert len(tasks) == 1
    assert tasks[0].task_type == "factory.delivery.run"
    store.close()


def test_task_claim_defer_retry_and_complete(tmp_path: Path) -> None:
    store = _store(tmp_path / "factory.db")
    assert store.enqueue_factory_task("test", "one")
    task = store.claim_factory_task("worker-1", lease_seconds=60)
    assert task is not None
    assert task.attempt_count == 1
    assert store.defer_factory_task(task.task_id, "worker-1", delay_seconds=0)

    task = store.claim_factory_task("worker-2", lease_seconds=60)
    assert task is not None
    assert task.attempt_count == 1
    assert store.complete_factory_task(task.task_id, "worker-2")
    assert store.load_factory_task(task.task_id).status == "completed"
    store.close()


def test_repo_lease_serializes_workers(tmp_path: Path) -> None:
    store = _store(tmp_path / "factory.db")
    assert store.acquire_repo_lease("acme/api", "worker-1", "task-1", lease_seconds=60)
    assert not store.acquire_repo_lease("acme/api", "worker-2", "task-2", lease_seconds=60)
    assert store.acquire_repo_lease("acme/web", "worker-2", "task-2", lease_seconds=60)
    store.release_repo_lease("acme/api", "worker-1", "task-1")
    assert store.acquire_repo_lease("acme/api", "worker-2", "task-2", lease_seconds=60)
    store.close()


def test_publication_and_review_task_are_atomic(tmp_path: Path) -> None:
    store = _store(tmp_path / "factory.db")
    assert store.claim_factory_job("acme", "project-1", "wi-1", repo="acme/api")
    key = factory_job_external_key("acme", "project-1", "wi-1")
    store.mark_factory_job_published(
        key,
        session_id="session-1",
        branch="simplecode/eng-1",
        pr_url="https://github.com/acme/api/pull/7",
        pr_repo="acme/api",
        pr_number=7,
        enqueue_review_sync=True,
    )
    job = store.load_factory_job(key)
    assert job.status == "published"
    assert job.pr_repo == "acme/api"
    assert job.pr_number == 7
    tasks = store.list_factory_tasks()
    assert [task.task_type for task in tasks] == ["factory.plane.review.sync"]
    store.close()


class _Plane:
    def __init__(self) -> None:
        self.transitions: list[tuple[str, str, str]] = []
        self.comments: list[PlaneComment] = []

    def resolve_state_id(self, project_id: str, state_name: str) -> str:
        return f"state:{state_name}"

    def update_work_item_state(self, project_id: str, item_id: str, state_id: str) -> object:
        self.transitions.append((project_id, item_id, state_id))
        return object()

    def list_comments(self, project_id: str, item_id: str) -> list[PlaneComment]:
        return self.comments

    def post_comment(self, project_id: str, item_id: str, body: str) -> PlaneComment:
        comment = PlaneComment(id="comment-1", body=body)
        self.comments = [comment]
        return comment

    def update_comment(
        self, project_id: str, item_id: str, comment_id: str, body: str
    ) -> PlaneComment:
        comment = PlaneComment(id=comment_id, body=body)
        self.comments = [comment]
        return comment


class _GitHub:
    def pull_request_state(self, repo: str, number: int) -> GitHubPullRequestState:
        return GitHubPullRequestState("merged", "approved", "passed", "")


class _UnusedFactory:
    pass


def test_github_event_tracks_pr_and_moves_merged_item_to_done(tmp_path: Path) -> None:
    store = _store(tmp_path / "factory.db")
    assert store.claim_factory_job("acme", "project-1", "wi-1", repo="acme/api")
    key = factory_job_external_key("acme", "project-1", "wi-1")
    store.mark_factory_job_published(
        key,
        session_id="session-1",
        branch="simplecode/eng-1",
        pr_url="https://github.com/acme/api/pull/7",
        pr_repo="acme/api",
        pr_number=7,
        enqueue_review_sync=False,
    )
    store.update_factory_job(key, "completed")
    assert store.enqueue_factory_task(
        "factory.github.sync",
        "github:delivery-1",
        repo="acme/api",
        metadata={"repo": "acme/api", "pr_number": 7},
    )
    plane = _Plane()
    worker = EventFactoryWorker(
        _config(tmp_path),
        plane,
        store,
        factory=_UnusedFactory(),  # type: ignore[arg-type]
        github=_GitHub(),
        worker_id="worker-1",
    )

    assert worker.run_once()
    job = store.load_factory_job(key)
    assert (job.pr_state, job.review_state, job.ci_state) == (
        "merged",
        "approved",
        "passed",
    )
    assert plane.transitions == [("project-1", "wi-1", "state:Done")]
    assert store.list_factory_tasks()[0].status == "completed"
    store.close()
