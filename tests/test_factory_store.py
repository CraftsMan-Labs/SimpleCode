"""Tests for durable factory-job persistence."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from simplecode.errors import StoreError
from simplecode.store import SessionStore, factory_job_external_key


def _open_store(db_path: Path) -> SessionStore:
    store = SessionStore(str(db_path))
    store.ensure_schema()
    return store


class TestFactoryJobStore:
    def test_first_claim(self, tmp_path: Path) -> None:
        store = _open_store(tmp_path / "test.db")
        claimed = store.claim_factory_job(
            "acme",
            "proj-1",
            "wi-99",
            work_item_ref="ACME-99",
            repo="acme/app",
            branch="simplecode/proj-1-99-fix",
        )
        assert claimed is True

        job = store.load_factory_job(factory_job_external_key("acme", "proj-1", "wi-99"))
        assert job.external_key == "acme:proj-1:wi-99"
        assert job.workspace_slug == "acme"
        assert job.project_id == "proj-1"
        assert job.work_item_id == "wi-99"
        assert job.work_item_ref == "ACME-99"
        assert job.status == "claimed"
        assert job.repo == "acme/app"
        assert job.branch == "simplecode/proj-1-99-fix"
        assert job.project_key is None
        assert job.title is None
        assert job.current_stage is None
        assert job.last_event_at is None
        assert job.session_id is None
        assert job.pr_url is None
        assert job.last_error is None
        store.close()

    def test_duplicate_claim(self, tmp_path: Path) -> None:
        store = _open_store(tmp_path / "test.db")
        first = store.claim_factory_job(
            "acme", "proj-1", "wi-99", repo="acme/app", work_item_ref="ACME-99"
        )
        second = store.claim_factory_job(
            "acme", "proj-1", "wi-99", repo="other/repo", work_item_ref="CHANGED"
        )
        assert first is True
        assert second is False

        job = store.load_factory_job(factory_job_external_key("acme", "proj-1", "wi-99"))
        assert job.repo == "acme/app"
        assert job.work_item_ref == "ACME-99"
        assert job.status == "claimed"
        store.close()

    def test_persistence(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        store = _open_store(db_path)
        assert store.claim_factory_job(
            "acme",
            "proj-1",
            "wi-99",
            work_item_ref="ACME-99",
            repo="acme/app",
            branch="feat/one",
            session_id="sess-1",
        )
        store.update_factory_job(
            factory_job_external_key("acme", "proj-1", "wi-99"),
            "running",
            pr_url="https://github.com/acme/app/pull/4",
        )
        store.close()

        reopened = _open_store(db_path)
        job = reopened.load_factory_job(factory_job_external_key("acme", "proj-1", "wi-99"))
        assert job.status == "running"
        assert job.repo == "acme/app"
        assert job.branch == "feat/one"
        assert job.session_id == "sess-1"
        assert job.pr_url == "https://github.com/acme/app/pull/4"
        assert job.work_item_ref == "ACME-99"
        reopened.close()

    def test_metadata_updates(self, tmp_path: Path) -> None:
        store = _open_store(tmp_path / "test.db")
        assert store.claim_factory_job("acme", "proj-1", "wi-99", repo="acme/app")
        key = factory_job_external_key("acme", "proj-1", "wi-99")

        store.update_factory_job(key, "running", session_id="sess-1", branch="feat/one")
        job = store.load_factory_job(key)
        assert job.status == "running"
        assert job.repo == "acme/app"
        assert job.session_id == "sess-1"
        assert job.branch == "feat/one"

        store.update_factory_job(key, "failed", last_error="push rejected")
        job = store.load_factory_job(key)
        assert job.status == "failed"
        assert job.last_error == "push rejected"
        assert job.repo == "acme/app"
        assert job.session_id == "sess-1"
        assert job.branch == "feat/one"

        store.update_factory_job(key, "running")
        job = store.load_factory_job(key)
        assert job.status == "running"
        assert job.last_error == "push rejected"
        assert job.repo == "acme/app"
        assert job.session_id == "sess-1"
        assert job.pr_url is None

        store.update_factory_job(key, "completed", pr_url="https://github.com/acme/app/pull/8")
        job = store.load_factory_job(key)
        assert job.status == "completed"
        assert job.pr_url == "https://github.com/acme/app/pull/8"
        assert job.repo == "acme/app"
        assert job.last_error == "push rejected"
        store.close()

    def test_optional_updates_distinguish_omitted_from_explicit_none(
        self, tmp_path: Path
    ) -> None:
        store = _open_store(tmp_path / "test.db")
        assert store.claim_factory_job("acme", "proj-1", "wi-99")
        key = factory_job_external_key("acme", "proj-1", "wi-99")
        store.update_factory_job(
            key,
            "running",
            current_stage="build",
            current_agent="builder",
            current_prompt=2,
        )

        store.update_factory_job(key, "running", current_stage="qa")
        job = store.load_factory_job(key)
        assert job.current_stage == "qa"
        assert job.current_agent == "builder"
        assert job.current_prompt == 2

        store.update_factory_job(
            key,
            "completed",
            current_agent=None,
            current_prompt=None,
        )
        job = store.load_factory_job(key)
        assert job.status == "completed"
        assert job.current_stage == "qa"
        assert job.current_agent is None
        assert job.current_prompt is None
        store.close()

    def test_active_recovery(self, tmp_path: Path) -> None:
        store = _open_store(tmp_path / "test.db")
        assert store.claim_factory_job("acme", "proj-1", "wi-1")
        assert store.claim_factory_job("acme", "proj-1", "wi-2")
        assert store.claim_factory_job("acme", "proj-1", "wi-3")
        assert store.claim_factory_job("acme", "proj-1", "wi-4")

        store.update_factory_job(factory_job_external_key("acme", "proj-1", "wi-2"), "running")
        store.update_factory_job(factory_job_external_key("acme", "proj-1", "wi-3"), "completed")
        store.update_factory_job(
            factory_job_external_key("acme", "proj-1", "wi-4"),
            "failed",
            last_error="qa blocked",
        )

        active = store.list_active_factory_jobs()
        assert [job.work_item_id for job in active] == ["wi-1", "wi-2"]
        assert {job.status for job in active} == {"claimed", "running"}
        store.close()

    def test_schema_upgrade_from_existing_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
        conn.execute(
            """
            CREATE TABLE factory_jobs (
                external_key TEXT PRIMARY KEY,
                workspace_slug TEXT NOT NULL,
                project_id TEXT NOT NULL,
                work_item_id TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

        store = _open_store(db_path)
        assert store.claim_factory_job(
            "acme", "proj-1", "wi-99", repo="acme/app", work_item_ref="ACME-99"
        )
        key = factory_job_external_key("acme", "proj-1", "wi-99")
        store.update_factory_job(
            key,
            "running",
            session_id="sess-1",
            branch="feat/one",
            pr_url="https://github.com/acme/app/pull/1",
        )
        job = store.load_factory_job(key)
        assert job.repo == "acme/app"
        assert job.work_item_ref == "ACME-99"
        assert job.session_id == "sess-1"
        assert job.branch == "feat/one"
        assert job.pr_url == "https://github.com/acme/app/pull/1"
        assert job.project_key is None
        assert job.title is None

        assert store._sqlite_conn is not None
        factory_columns = {
            row[1] for row in store._sqlite_conn.execute("PRAGMA table_info(factory_jobs)")
        }
        assert {
            "project_key",
            "title",
            "current_stage",
            "current_agent",
            "current_prompt",
            "plane_progress_comment_id",
            "last_event_at",
        } <= factory_columns
        event_columns = {
            row[1] for row in store._sqlite_conn.execute("PRAGMA table_info(factory_events)")
        }
        assert {
            "event_id",
            "external_key",
            "session_id",
            "event_type",
            "stage",
            "status",
            "message",
            "metadata",
            "created_at",
        } == event_columns
        store.close()

    def test_dashboard_job_reads_filter_and_order(self, tmp_path: Path) -> None:
        store = _open_store(tmp_path / "test.db")
        assert store.claim_factory_job(
            "acme",
            "proj-1",
            "wi-1",
            project_key="ENG",
            work_item_ref="ENG-1",
            title="First job",
        )
        assert store.claim_factory_job(
            "acme",
            "proj-2",
            "wi-2",
            project_key="OPS",
            work_item_ref="OPS-2",
            title="Second job",
        )
        first_key = factory_job_external_key("acme", "proj-1", "wi-1")
        second_key = factory_job_external_key("acme", "proj-2", "wi-2")
        store.update_factory_job(
            first_key,
            "running",
            current_stage="build",
            current_agent="builder",
            current_prompt=2,
            plane_progress_comment_id="comment-1",
        )
        store.update_factory_job(second_key, "completed", current_stage="plane_review")

        assert store._sqlite_conn is not None
        store._sqlite_conn.execute(
            "UPDATE factory_jobs SET last_event_at = ? WHERE external_key = ?",
            ("2026-10-09T10:00:00+00:00", first_key),
        )
        store._sqlite_conn.execute(
            "UPDATE factory_jobs SET last_event_at = ? WHERE external_key = ?",
            ("2026-10-09T11:00:00+00:00", second_key),
        )
        store._sqlite_conn.commit()

        jobs = store.list_factory_jobs()
        assert [job.external_key for job in jobs] == [second_key, first_key]
        running = store.list_factory_jobs(status="running", project_key="ENG", limit=1)
        assert [job.external_key for job in running] == [first_key]
        assert running[0].title == "First job"
        assert running[0].current_agent == "builder"
        assert running[0].current_prompt == 2
        assert running[0].plane_progress_comment_id == "comment-1"
        assert store.list_factory_jobs(status="missing") == []
        assert store.list_factory_jobs(limit=0) == []
        store.close()

    def test_factory_event_reads_order_filter_and_metadata(self, tmp_path: Path) -> None:
        store = _open_store(tmp_path / "test.db")
        assert store.claim_factory_job("acme", "proj-1", "wi-1")
        assert store.claim_factory_job("acme", "proj-1", "wi-2")
        key = factory_job_external_key("acme", "proj-1", "wi-1")
        other_key = factory_job_external_key("acme", "proj-1", "wi-2")

        first = store.append_factory_event(
            key,
            "agent_started",
            session_id="session-1",
            stage="build",
            status="running",
            message="Builder started",
            metadata={"prompt_count": 2, "safe": True},
        )
        store.append_factory_event(other_key, "claimed", stage="claim")
        second = store.append_factory_event(
            key,
            "prompt_started",
            session_id="session-1",
            stage="build",
            metadata={"prompt": 1},
        )

        events = store.list_factory_events(key)
        assert [event.event_id for event in events] == [first.event_id, second.event_id]
        assert events[0].metadata == {"prompt_count": 2, "safe": True}
        assert events[0].created_at.tzinfo is not None
        remaining = store.list_factory_events(key, after_id=first.event_id)
        assert [event.event_id for event in remaining] == [second.event_id]
        assert store.list_factory_events(key, after_id=second.event_id) == []
        assert store.list_factory_events(key, limit=0) == []
        assert store.load_factory_job(key).last_event_at == second.created_at
        store.close()

    def test_factory_event_newest_window_returns_ascending(self, tmp_path: Path) -> None:
        store = _open_store(tmp_path / "test.db")
        assert store.claim_factory_job("acme", "proj-1", "wi-1")
        key = factory_job_external_key("acme", "proj-1", "wi-1")
        event_ids = [
            store.append_factory_event(key, "progress", metadata={"sequence": sequence}).event_id
            for sequence in range(5)
        ]

        oldest_window = store.list_factory_events(key, limit=3)
        newest_window = store.list_factory_events(key, limit=3, newest=True)

        assert [event.event_id for event in oldest_window] == event_ids[:3]
        assert [event.event_id for event in newest_window] == event_ids[-3:]
        assert [event.event_id for event in newest_window] == sorted(
            event.event_id for event in newest_window
        )
        store.close()

    def test_postgres_dashboard_query_uses_postgres_placeholders(self) -> None:
        store = SessionStore.__new__(SessionStore)
        store._is_pg = True
        calls: list[tuple[str, tuple[object, ...]]] = []

        def fetchall(sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
            calls.append((sql, params))
            return []

        store._fetchall = fetchall  # type: ignore[method-assign]

        assert store.list_factory_jobs(status="running", workspace_slug="acme", limit=5) == []
        assert store.list_factory_events("acme:proj:wi", after_id=10, limit=20) == []
        assert store.list_factory_events(
            "acme:proj:wi", after_id=10, limit=20, newest=True
        ) == []
        assert len(calls) == 3
        assert "status = %s" in calls[0][0]
        assert "workspace_slug = %s" in calls[0][0]
        assert "LIMIT %s" in calls[0][0]
        assert calls[0][1] == ("running", "acme", 5)
        assert "external_key = %s AND event_id > %s" in calls[1][0]
        assert "ORDER BY event_id ASC LIMIT %s" in calls[1][0]
        assert calls[1][1] == ("acme:proj:wi", 10, 20)
        assert "event_id > %s ORDER BY event_id DESC LIMIT %s" in calls[2][0]
        assert "AS recent_events ORDER BY event_id ASC" in calls[2][0]
        assert calls[2][1] == ("acme:proj:wi", 10, 20)

    def test_postgres_factory_event_append_is_one_atomic_statement(self) -> None:
        store = SessionStore.__new__(SessionStore)
        store._is_pg = True
        calls: list[tuple[str, tuple[object, ...]]] = []

        def fetchone(
            sql: str, params: tuple[object, ...] = ()
        ) -> dict[str, object] | None:
            calls.append((sql, params))
            return {
                "event_id": 7,
                "external_key": "acme:proj:wi",
                "session_id": "session-1",
                "event_type": "prompt_started",
                "stage": "build",
                "status": "running",
                "message": "Prompt started",
                "metadata": {"prompt": 2},
                "created_at": "2026-10-09T12:00:00+00:00",
            }

        store._fetchone = fetchone  # type: ignore[method-assign]
        event = store.append_factory_event(
            "acme:proj:wi",
            "prompt_started",
            session_id="session-1",
            stage="build",
            status="running",
            message="Prompt started",
            metadata={"prompt": 2},
        )

        assert event.event_id == 7
        assert len(calls) == 1
        sql, params = calls[0]
        assert "WITH inserted AS" in sql
        assert "INSERT INTO factory_events" in sql
        assert "updated AS" in sql
        assert "UPDATE factory_jobs AS jobs" in sql
        assert "SET last_event_at = inserted.created_at" in sql
        assert "SELECT inserted.*" in sql
        assert "JOIN updated USING (external_key)" in sql
        assert sql.count("%s") == 7
        assert params == (
            "acme:proj:wi",
            "session-1",
            "prompt_started",
            "build",
            "running",
            "Prompt started",
            '{"prompt":2}',
        )

    def test_load_missing_job(self, tmp_path: Path) -> None:
        store = _open_store(tmp_path / "test.db")
        with pytest.raises(StoreError, match="Factory job not found"):
            store.load_factory_job("missing:key")
        store.close()

    def test_release_only_unstarted_expected_status(self, tmp_path: Path) -> None:
        store = _open_store(tmp_path / "test.db")
        assert store.claim_factory_job("acme", "proj-1", "wi-1")
        key = factory_job_external_key("acme", "proj-1", "wi-1")

        assert store.release_factory_job(key, expected_status="running") is False
        assert store.release_factory_job(key, expected_status="claimed") is True
        with pytest.raises(StoreError, match="Factory job not found"):
            store.load_factory_job(key)
        store.close()

    def test_sqlite_factory_lock_serializes_connections(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        first = _open_store(db_path)
        second = SessionStore(f"{tmp_path}/./test.db")
        second.ensure_schema()
        attempting = threading.Event()
        acquired = threading.Event()

        def contend() -> None:
            attempting.set()
            with second.factory_lock():
                acquired.set()

        thread = threading.Thread(target=contend)
        with first.factory_lock():
            thread.start()
            assert attempting.wait(1)
            assert not acquired.wait(0.05)

        assert acquired.wait(1)
        thread.join(timeout=1)
        assert not thread.is_alive()
        first.close()
        second.close()
