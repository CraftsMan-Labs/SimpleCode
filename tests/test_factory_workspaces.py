"""Tests for durable factory workspace persistence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from simplecode.errors import StoreError
from simplecode.factory import FactoryError, cleanup_coder_workspace_attempt
from simplecode.factory_config import (
    CoderWorkspaceConfig,
    FactoryConfig,
    FactoryProjectMapping,
    FactoryStateNames,
    FactoryWorkspaceConfig,
    PlaneConnectionConfig,
)
from simplecode.store import SessionStore, factory_job_external_key


def _open_store(db_path: Path) -> SessionStore:
    store = SessionStore(str(db_path))
    store.ensure_schema()
    return store


def _claim_job(store: SessionStore, work_item_id: str = "wi-1") -> str:
    assert store.claim_factory_job("acme", "proj-1", work_item_id)
    return factory_job_external_key("acme", "proj-1", work_item_id)


def _coder_config(tmp_path: Path) -> FactoryConfig:
    return FactoryConfig(
        plane=PlaneConnectionConfig("http://plane", "acme", "PLANE_API_KEY"),
        poll_interval=30,
        states=FactoryStateNames(
            ready="Ready",
            in_progress="In Progress",
            in_review="In Review",
            needs_attention="Needs Attention",
        ),
        pipeline=tmp_path / "pipeline",
        projects=(FactoryProjectMapping("ENG", "acme/api"),),
        workspace=FactoryWorkspaceConfig(
            provider="coder",
            coder=CoderWorkspaceConfig(
                url="https://coder.example.test",
                token_env="CODER_SESSION_TOKEN",
                template="simplecode-factory",
            ),
        ),
    )


class _CoderClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.deleted: list[str] = []

    def delete(self, workspace: str) -> bool:
        self.deleted.append(workspace)
        if self.error is not None:
            raise self.error
        return True


def test_records_workspace_identity_idempotently(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "factory.db")
    key = _claim_job(store)

    created = store.record_factory_workspace(
        key,
        1,
        workspace_name="sc-job-a1",
        provider="coder",
        template="simplecode-factory",
    )
    recorded = store.record_factory_workspace(
        key,
        1,
        workspace_name="sc-job-a1",
        provider="coder",
        template="simplecode-factory",
        workspace_id="workspace-123",
    )

    assert created.status == "provisioning"
    assert created.cleanup_status == "not_requested"
    assert recorded.workspace_id == "workspace-123"
    assert len(store.list_factory_workspaces()) == 1
    store.close()


def test_operator_cleanup_updates_durable_status(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "factory.db")
    key = _claim_job(store)
    store.record_factory_workspace(
        key,
        1,
        workspace_name="sc-job-a1",
        provider="coder",
        template="simplecode-factory",
    )
    client = _CoderClient()

    cleanup_coder_workspace_attempt(
        _coder_config(tmp_path), store, key, 1, client=client  # type: ignore[arg-type]
    )

    assert client.deleted == ["sc-job-a1"]
    assert store.load_factory_workspace(key, 1).cleanup_status == "completed"
    store.close()


def test_operator_cleanup_sanitizes_and_records_failure(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "factory.db")
    key = _claim_job(store)
    store.record_factory_workspace(
        key,
        1,
        workspace_name="sc-job-a1",
        provider="coder",
        template="simplecode-factory",
    )
    client = _CoderClient(RuntimeError("token=cleanup-secret unavailable"))

    with pytest.raises(FactoryError, match="REDACTED"):
        cleanup_coder_workspace_attempt(
            _coder_config(tmp_path), store, key, 1, client=client  # type: ignore[arg-type]
        )

    row = store.load_factory_workspace(key, 1)
    assert row.cleanup_status == "failed"
    assert row.last_error is not None
    assert "cleanup-secret" not in row.last_error
    store.close()


def test_rejects_workspace_identity_conflicts(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "factory.db")
    key = _claim_job(store)
    store.record_factory_workspace(
        key,
        1,
        workspace_name="sc-job-a1",
        provider="coder",
        template="simplecode-factory",
        workspace_id="workspace-123",
    )

    with pytest.raises(StoreError, match="identity conflict"):
        store.record_factory_workspace(
            key,
            1,
            workspace_name="different-name",
            provider="coder",
            template="simplecode-factory",
        )
    with pytest.raises(StoreError, match="identity conflict"):
        store.record_factory_workspace(
            key,
            1,
            workspace_name="sc-job-a1",
            provider="coder",
            template="simplecode-factory",
            workspace_id="different-id",
        )
    store.close()


def test_updates_lifecycle_and_milestone_timestamps(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "factory.db")
    key = _claim_job(store)
    store.record_factory_workspace(
        key,
        1,
        workspace_name="sc-job-a1",
        provider="coder",
        template="simplecode-factory",
    )

    ready = store.update_factory_workspace_lifecycle(key, 1, "ready", workspace_id="workspace-123")
    running = store.update_factory_workspace_lifecycle(key, 1, "running")
    failed = store.update_factory_workspace_lifecycle(
        key, 1, "failed", last_error="remote command failed"
    )

    assert ready.provisioned_at is not None
    assert ready.workspace_id == "workspace-123"
    assert running.execution_started_at is not None
    assert failed.finished_at is not None
    assert failed.last_error == "remote command failed"
    assert store.load_factory_workspace(key, 1) == failed
    with pytest.raises(StoreError, match="identity conflict"):
        store.update_factory_workspace_lifecycle(
            key, 1, "failed", workspace_id="different-workspace"
        )
    assert store.load_factory_workspace(key, 1).workspace_id == "workspace-123"
    with pytest.raises(StoreError, match="not found"):
        store.update_factory_workspace_lifecycle(key, 2, "ready")
    store.close()


def test_lists_filters_and_cleanup_candidates(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "factory.db")
    first_key = _claim_job(store, "wi-1")
    second_key = _claim_job(store, "wi-2")
    for key, name in ((first_key, "sc-one-a1"), (second_key, "sc-two-a1")):
        store.record_factory_workspace(
            key,
            1,
            workspace_name=name,
            provider="coder",
            template="simplecode-factory",
        )

    assert store.mark_factory_workspace_cleanup_pending(first_key, 1)
    assert store.mark_factory_workspace_cleanup_pending(second_key, 1)
    assert store.mark_factory_workspace_cleanup_failed(second_key, 1, "delete timed out")

    candidates = store.find_factory_workspace_cleanup_candidates()
    assert {row.external_key for row in candidates} == {first_key, second_key}
    assert store.list_factory_workspaces(cleanup_status="failed")[0].last_error == (
        "delete timed out"
    )
    assert store.list_factory_workspaces(external_key=first_key, limit=1)[0].workspace_name == (
        "sc-one-a1"
    )
    assert store.list_factory_workspaces(provider="other") == []
    assert store.list_factory_workspaces(limit=0) == []

    assert store.mark_factory_workspace_cleanup_complete(first_key, 1)
    completed = store.load_factory_workspace(first_key, 1)
    assert completed.cleanup_status == "completed"
    assert completed.cleanup_requested_at is not None
    assert completed.cleanup_completed_at is not None
    assert [row.external_key for row in store.find_factory_workspace_cleanup_candidates()] == [
        second_key
    ]
    assert not store.mark_factory_workspace_cleanup_pending("missing", 1)
    store.close()


def test_schema_upgrade_adds_workspace_table_and_job_delete_cascades(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
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
    key = _claim_job(store)
    store.record_factory_workspace(
        key,
        1,
        workspace_name="sc-job-a1",
        provider="coder",
        template="simplecode-factory",
    )
    assert store.release_factory_job(key, expected_status="claimed")
    assert store.list_factory_workspaces() == []
    store.close()
