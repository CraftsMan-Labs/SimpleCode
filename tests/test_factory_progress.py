"""Focused tests for durable factory progress and Plane comment reuse."""

from __future__ import annotations

from pathlib import Path

from simplecode.factory_progress import FactoryProgressTracker, factory_progress_marker
from simplecode.plane import PlaneComment, PlaneError, PlaneNotFoundError
from simplecode.store import SessionStore, factory_job_external_key


class FakeProgressPlane:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.updates: list[tuple[str, str]] = []
        self.comments: list[PlaneComment] = []
        self.update_error: Exception | None = None
        self.list_error: Exception | None = None

    def list_comments(self, project_id: str, work_item_id: str) -> list[PlaneComment]:
        if self.list_error is not None:
            raise self.list_error
        return list(self.comments)

    def post_comment(self, project_id: str, work_item_id: str, comment: str) -> PlaneComment:
        self.posts.append(comment)
        posted = PlaneComment(id=f"comment-{len(self.posts)}", body=comment, html=comment)
        self.comments.append(posted)
        return posted

    def update_comment(
        self,
        project_id: str,
        work_item_id: str,
        comment_id: str,
        comment: str,
    ) -> PlaneComment:
        if self.update_error is not None:
            raise self.update_error
        self.updates.append((comment_id, comment))
        return PlaneComment(id=comment_id, body=comment)


def _tracker(
    tmp_path: Path,
) -> tuple[FactoryProgressTracker, FakeProgressPlane, SessionStore, str]:
    store = SessionStore(str(tmp_path / "progress.db"))
    store.ensure_schema()
    assert store.claim_factory_job(
        "workspace",
        "project",
        "item",
        project_key="ENG",
        title="Safe title",
        repo="acme/api",
        branch="simplecode/eng-1-safe",
    )
    key = factory_job_external_key("workspace", "project", "item")
    plane = FakeProgressPlane()
    return FactoryProgressTracker(store, plane, key, "project", "item"), plane, store, key


def test_tracker_reuses_one_comment_and_clears_stale_progress(tmp_path: Path) -> None:
    tracker, plane, store, key = _tracker(tmp_path)

    tracker.milestone("started", "setup", message="Setup started")
    tracker.on_session("session-1")
    tracker.on_agent_start(1, "builder", 2, 3)
    tracker.on_prompt_start("builder", 1, 3)
    tracker.on_prompt_done("builder", 1, 3)
    tracker.on_agent_done(1, "builder", 2)

    job = store.load_factory_job(key)
    assert len(plane.posts) == 1
    assert plane.updates
    assert {comment_id for comment_id, _ in plane.updates} == {"comment-1"}
    assert job.plane_progress_comment_id == "comment-1"
    assert job.session_id == "session-1"
    assert job.current_stage == "builder"
    assert job.current_agent is None
    assert job.current_prompt is None
    assert [event.event_type for event in store.list_factory_events(key)] == [
        "started",
        "session_started",
        "agent_started",
        "prompt_started",
        "prompt_finished",
        "agent_finished",
    ]


def test_missing_plane_comment_is_replaced_once(tmp_path: Path) -> None:
    tracker, plane, store, key = _tracker(tmp_path)
    tracker.milestone("started", "setup")
    plane.update_error = PlaneNotFoundError("gone", status=404)

    tracker.milestone("next", "pipeline")

    assert len(plane.posts) == 2
    assert store.load_factory_job(key).plane_progress_comment_id == "comment-2"


def test_tracker_adopts_marked_comment_after_post_persistence_crash(tmp_path: Path) -> None:
    tracker, plane, store, key = _tracker(tmp_path)
    marker = factory_progress_marker(key)
    plane.comments.append(
        PlaneComment(
            id="existing-progress",
            body=f"Factory marker: {marker}",
            html=f"<p><code>{marker}</code></p>",
        )
    )

    tracker.milestone("recovered", "recovery")

    assert plane.posts == []
    assert plane.updates[0][0] == "existing-progress"
    assert store.load_factory_job(key).plane_progress_comment_id == "existing-progress"
    assert key not in marker


def test_tracker_ignores_unrelated_comments_before_posting(tmp_path: Path) -> None:
    tracker, plane, store, key = _tracker(tmp_path)
    plane.comments.extend(
        [
            PlaneComment(id="human", body="Factory Progress is useful"),
            PlaneComment(
                id="other-job",
                html=(f"<p><code>{factory_progress_marker('workspace:project:other')}</code></p>"),
            ),
        ]
    )

    tracker.milestone("started", "setup")

    assert len(plane.posts) == 1
    assert store.load_factory_job(key).plane_progress_comment_id == "comment-1"


def test_progress_comment_escapes_dynamic_fields(tmp_path: Path) -> None:
    tracker, plane, store, key = _tracker(tmp_path)
    store.update_factory_job(
        key,
        "failed",
        branch="<script>alert('branch')</script>",
        last_error='<img src=x onerror="alert(1)"> & failed',
    )

    tracker.milestone("failed", "<b>unsafe stage</b>")

    body = plane.posts[0]
    assert factory_progress_marker(key) in body
    assert "<script>" not in body
    assert "<img" not in body
    assert "<b>unsafe" not in body
    assert "&lt;script&gt;" in body
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in body
    assert "&lt;b&gt;unsafe stage&lt;/b&gt;" in body


def test_comment_discovery_failure_does_not_risk_duplicate(tmp_path: Path) -> None:
    tracker, plane, store, key = _tracker(tmp_path)
    plane.list_error = PlaneError("comment listing unavailable")

    tracker.milestone("started", "setup")

    assert plane.posts == []
    assert store.load_factory_job(key).plane_progress_comment_id is None
    assert [event.event_type for event in store.list_factory_events(key)] == ["started"]


def test_plane_comment_update_failure_keeps_event_without_posting_duplicate(
    tmp_path: Path,
) -> None:
    tracker, plane, store, key = _tracker(tmp_path)
    tracker.milestone("started", "setup")
    plane.update_error = PlaneError("comment service unavailable")

    tracker.on_agent_start(1, "builder", 1, 1)

    assert [event.event_type for event in store.list_factory_events(key)] == [
        "started",
        "agent_started",
    ]
    job = store.load_factory_job(key)
    assert job.current_agent == "builder"
    assert job.plane_progress_comment_id == "comment-1"
    assert len(plane.posts) == 1
    assert plane.updates == []
