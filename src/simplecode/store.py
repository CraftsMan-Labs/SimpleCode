"""Session store for checkpoint/resume — supports PostgreSQL and SQLite."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from simplecode.errors import CheckpointNotFoundError, StoreError

logger = logging.getLogger(__name__)

SQLITE_DEFAULT_PATH = Path.home() / ".simplecode" / "simplecode.db"

_PG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    pipeline_name       TEXT NOT NULL,
    pipeline_yaml       TEXT NOT NULL,
    runtime             TEXT NOT NULL,
    model               TEXT,
    permissions         TEXT NOT NULL DEFAULT 'write',
    working_dir         TEXT NOT NULL,
    output_dir          TEXT NOT NULL,
    input_files         JSONB NOT NULL DEFAULT '[]',
    timeout             INTEGER NOT NULL DEFAULT 300,
    heartbeat_interval  REAL NOT NULL DEFAULT 0,

    status              TEXT NOT NULL DEFAULT 'running',
    gated_agent_id      TEXT,
    gated_agent_index   INTEGER,
    agents_completed    INTEGER NOT NULL DEFAULT 0,
    total_agents        INTEGER NOT NULL,
    context_files       JSONB NOT NULL DEFAULT '[]',
    last_output         TEXT,
    gate_round          INTEGER NOT NULL DEFAULT 0,
    gate_rejected_at    TEXT,

    branch_strategy     TEXT,
    branch              TEXT,
    worktree_path       TEXT,
    docker_image        TEXT,
    host_repo           TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_outputs (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    agent_id    TEXT NOT NULL,
    prompt_num  INTEGER NOT NULL,
    output_text TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, agent_id, prompt_num)
);

CREATE TABLE IF NOT EXISTS factory_jobs (
    external_key     TEXT PRIMARY KEY,
    workspace_slug   TEXT NOT NULL,
    project_id       TEXT NOT NULL,
    project_key      TEXT,
    work_item_id     TEXT NOT NULL,
    work_item_ref    TEXT,
    title            TEXT,
    status           TEXT NOT NULL DEFAULT 'claimed',
    repo             TEXT,
    branch           TEXT,
    session_id       TEXT,
    pr_url           TEXT,
    last_error       TEXT,
    current_stage    TEXT,
    current_agent    TEXT,
    current_prompt   INTEGER,
    plane_progress_comment_id TEXT,
    attempt          INTEGER NOT NULL DEFAULT 1,
    cancel_requested_at TIMESTAMPTZ,
    pr_repo          TEXT,
    pr_number        INTEGER,
    pr_state         TEXT,
    ci_state         TEXT,
    review_state     TEXT,
    completed_at     TIMESTAMPTZ,
    last_event_at    TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS factory_jobs_plane_key
    ON factory_jobs (workspace_slug, project_id, work_item_id);

CREATE TABLE IF NOT EXISTS factory_events (
    event_id       BIGSERIAL PRIMARY KEY,
    external_key   TEXT NOT NULL REFERENCES factory_jobs(external_key) ON DELETE CASCADE,
    session_id     TEXT,
    event_type     TEXT NOT NULL,
    stage          TEXT,
    status         TEXT,
    message        TEXT,
    metadata       JSONB NOT NULL DEFAULT '{}',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS factory_events_job_event
    ON factory_events (external_key, event_id);

CREATE TABLE IF NOT EXISTS factory_deliveries (
    source             TEXT NOT NULL,
    source_event_id    TEXT NOT NULL,
    delivery_id        TEXT,
    event_type         TEXT NOT NULL,
    subject_id         TEXT,
    project_id         TEXT,
    repo               TEXT,
    payload_sha256     TEXT NOT NULL,
    metadata           JSONB NOT NULL DEFAULT '{}',
    disposition        TEXT NOT NULL DEFAULT 'accepted',
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, source_event_id)
);

CREATE TABLE IF NOT EXISTS factory_tasks (
    task_id            TEXT PRIMARY KEY,
    task_type          TEXT NOT NULL,
    idempotency_key    TEXT NOT NULL UNIQUE,
    external_key       TEXT,
    repo               TEXT,
    metadata           JSONB NOT NULL DEFAULT '{}',
    status             TEXT NOT NULL DEFAULT 'pending',
    attempt_count      INTEGER NOT NULL DEFAULT 0,
    available_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner        TEXT,
    lease_expires_at   TIMESTAMPTZ,
    last_error         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS factory_tasks_ready
    ON factory_tasks (status, available_at, created_at);

CREATE TABLE IF NOT EXISTS factory_repo_leases (
    repo               TEXT PRIMARY KEY,
    worker_id          TEXT NOT NULL,
    task_id            TEXT NOT NULL,
    lease_expires_at   TIMESTAMPTZ NOT NULL,
    heartbeat_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS factory_workspaces (
    external_key        TEXT NOT NULL REFERENCES factory_jobs(external_key) ON DELETE CASCADE,
    attempt             INTEGER NOT NULL,
    provider            TEXT NOT NULL,
    template            TEXT NOT NULL,
    workspace_name      TEXT NOT NULL,
    workspace_id        TEXT,
    status              TEXT NOT NULL DEFAULT 'provisioning',
    cleanup_status      TEXT NOT NULL DEFAULT 'not_requested',
    last_error          TEXT,
    provisioned_at      TIMESTAMPTZ,
    execution_started_at TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    cleanup_requested_at TIMESTAMPTZ,
    cleanup_completed_at TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (external_key, attempt),
    UNIQUE (provider, workspace_name)
);

CREATE INDEX IF NOT EXISTS factory_workspaces_cleanup
    ON factory_workspaces (cleanup_status, updated_at);
"""

_SQLITE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    pipeline_name       TEXT NOT NULL,
    pipeline_yaml       TEXT NOT NULL,
    runtime             TEXT NOT NULL,
    model               TEXT,
    permissions         TEXT NOT NULL DEFAULT 'write',
    working_dir         TEXT NOT NULL,
    output_dir          TEXT NOT NULL,
    input_files         TEXT NOT NULL DEFAULT '[]',
    timeout             INTEGER NOT NULL DEFAULT 300,
    heartbeat_interval  REAL NOT NULL DEFAULT 0,

    status              TEXT NOT NULL DEFAULT 'running',
    gated_agent_id      TEXT,
    gated_agent_index   INTEGER,
    agents_completed    INTEGER NOT NULL DEFAULT 0,
    total_agents        INTEGER NOT NULL,
    context_files       TEXT NOT NULL DEFAULT '[]',
    last_output         TEXT,
    gate_round          INTEGER NOT NULL DEFAULT 0,
    gate_rejected_at    TEXT,

    branch_strategy     TEXT,
    branch              TEXT,
    worktree_path       TEXT,
    docker_image        TEXT,
    host_repo           TEXT,

    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_outputs (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    agent_id    TEXT NOT NULL,
    prompt_num  INTEGER NOT NULL,
    output_text TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (session_id, agent_id, prompt_num)
);

CREATE TABLE IF NOT EXISTS factory_jobs (
    external_key     TEXT PRIMARY KEY,
    workspace_slug   TEXT NOT NULL,
    project_id       TEXT NOT NULL,
    project_key      TEXT,
    work_item_id     TEXT NOT NULL,
    work_item_ref    TEXT,
    title            TEXT,
    status           TEXT NOT NULL DEFAULT 'claimed',
    repo             TEXT,
    branch           TEXT,
    session_id       TEXT,
    pr_url           TEXT,
    last_error       TEXT,
    current_stage    TEXT,
    current_agent    TEXT,
    current_prompt   INTEGER,
    plane_progress_comment_id TEXT,
    attempt          INTEGER NOT NULL DEFAULT 1,
    cancel_requested_at TEXT,
    pr_repo          TEXT,
    pr_number        INTEGER,
    pr_state         TEXT,
    ci_state         TEXT,
    review_state     TEXT,
    completed_at     TEXT,
    last_event_at    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS factory_jobs_plane_key
    ON factory_jobs (workspace_slug, project_id, work_item_id);

CREATE TABLE IF NOT EXISTS factory_events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    external_key   TEXT NOT NULL REFERENCES factory_jobs(external_key) ON DELETE CASCADE,
    session_id     TEXT,
    event_type     TEXT NOT NULL,
    stage          TEXT,
    status         TEXT,
    message        TEXT,
    metadata       TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS factory_events_job_event
    ON factory_events (external_key, event_id);

CREATE TABLE IF NOT EXISTS factory_deliveries (
    source             TEXT NOT NULL,
    source_event_id    TEXT NOT NULL,
    delivery_id        TEXT,
    event_type         TEXT NOT NULL,
    subject_id         TEXT,
    project_id         TEXT,
    repo               TEXT,
    payload_sha256     TEXT NOT NULL,
    metadata           TEXT NOT NULL DEFAULT '{}',
    disposition        TEXT NOT NULL DEFAULT 'accepted',
    received_at        TEXT NOT NULL,
    PRIMARY KEY (source, source_event_id)
);

CREATE TABLE IF NOT EXISTS factory_tasks (
    task_id            TEXT PRIMARY KEY,
    task_type          TEXT NOT NULL,
    idempotency_key    TEXT NOT NULL UNIQUE,
    external_key       TEXT,
    repo               TEXT,
    metadata           TEXT NOT NULL DEFAULT '{}',
    status             TEXT NOT NULL DEFAULT 'pending',
    attempt_count      INTEGER NOT NULL DEFAULT 0,
    available_at       TEXT NOT NULL,
    lease_owner        TEXT,
    lease_expires_at   TEXT,
    last_error         TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    completed_at       TEXT
);

CREATE INDEX IF NOT EXISTS factory_tasks_ready
    ON factory_tasks (status, available_at, created_at);

CREATE TABLE IF NOT EXISTS factory_repo_leases (
    repo               TEXT PRIMARY KEY,
    worker_id          TEXT NOT NULL,
    task_id            TEXT NOT NULL,
    lease_expires_at   TEXT NOT NULL,
    heartbeat_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS factory_workspaces (
    external_key        TEXT NOT NULL REFERENCES factory_jobs(external_key) ON DELETE CASCADE,
    attempt             INTEGER NOT NULL,
    provider            TEXT NOT NULL,
    template            TEXT NOT NULL,
    workspace_name      TEXT NOT NULL,
    workspace_id        TEXT,
    status              TEXT NOT NULL DEFAULT 'provisioning',
    cleanup_status      TEXT NOT NULL DEFAULT 'not_requested',
    last_error          TEXT,
    provisioned_at      TEXT,
    execution_started_at TEXT,
    finished_at         TEXT,
    cleanup_requested_at TEXT,
    cleanup_completed_at TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (external_key, attempt),
    UNIQUE (provider, workspace_name)
);

CREATE INDEX IF NOT EXISTS factory_workspaces_cleanup
    ON factory_workspaces (cleanup_status, updated_at);
"""

FACTORY_JOB_TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")
_FACTORY_ADVISORY_LOCK_ID = 0x53494D504C45434F


class _UnsetType:
    """Marker for an optional update argument that was not supplied."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _UnsetType()


_FACTORY_JOB_COLUMNS: tuple[str, ...] = (
    "external_key",
    "workspace_slug",
    "project_id",
    "project_key",
    "work_item_id",
    "work_item_ref",
    "title",
    "status",
    "repo",
    "branch",
    "session_id",
    "pr_url",
    "last_error",
    "current_stage",
    "current_agent",
    "current_prompt",
    "plane_progress_comment_id",
    "attempt",
    "cancel_requested_at",
    "pr_repo",
    "pr_number",
    "pr_state",
    "ci_state",
    "review_state",
    "completed_at",
    "last_event_at",
    "created_at",
    "updated_at",
)

_FACTORY_JOB_OPTIONAL_COLUMNS: tuple[str, ...] = (
    "work_item_ref",
    "project_key",
    "title",
    "repo",
    "branch",
    "session_id",
    "pr_url",
    "last_error",
    "current_stage",
    "current_agent",
    "current_prompt",
    "plane_progress_comment_id",
    "cancel_requested_at",
    "pr_repo",
    "pr_number",
    "pr_state",
    "ci_state",
    "review_state",
    "completed_at",
)


def factory_job_external_key(workspace_slug: str, project_id: str, work_item_id: str) -> str:
    """Stable claim key for a Plane workspace/project/work-item."""
    return f"{workspace_slug}:{project_id}:{work_item_id}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_json_col(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


def _parse_json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return {str(key): item for key, item in parsed.items()}
    return {}


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"Expected int-compatible value, got {type(value).__name__}")


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise TypeError(f"Expected float-compatible value, got {type(value).__name__}")


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return datetime.now(UTC)


@dataclass
class SessionRow:
    """Mirror of a row in the sessions table."""

    session_id: str
    pipeline_name: str
    pipeline_yaml: str
    runtime: str
    model: str | None
    permissions: str
    working_dir: str
    output_dir: str
    input_files: list[str]
    timeout: int
    heartbeat_interval: float

    status: str
    gated_agent_id: str | None
    gated_agent_index: int | None
    agents_completed: int
    total_agents: int
    context_files: list[str]
    last_output: str | None
    gate_round: int
    gate_rejected_at: str | None

    branch_strategy: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    docker_image: str | None = None
    host_repo: str | None = None

    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class FactoryJobRow:
    """Mirror of a row in the factory_jobs table."""

    external_key: str
    workspace_slug: str
    project_id: str
    work_item_id: str
    work_item_ref: str | None
    status: str
    repo: str | None
    branch: str | None
    session_id: str | None
    pr_url: str | None
    last_error: str | None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    project_key: str | None = None
    title: str | None = None
    current_stage: str | None = None
    current_agent: str | None = None
    current_prompt: int | None = None
    plane_progress_comment_id: str | None = None
    attempt: int = 1
    cancel_requested_at: datetime | None = None
    pr_repo: str | None = None
    pr_number: int | None = None
    pr_state: str | None = None
    ci_state: str | None = None
    review_state: str | None = None
    completed_at: datetime | None = None
    last_event_at: datetime | None = None


@dataclass
class FactoryEventRow:
    """One durable, structured factory progress event."""

    event_id: int
    external_key: str
    session_id: str | None
    event_type: str
    stage: str | None
    status: str | None
    message: str | None
    metadata: dict[str, object]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class FactoryTaskRow:
    """One durable unit of factory work."""

    task_id: str
    task_type: str
    idempotency_key: str
    external_key: str | None
    repo: str | None
    metadata: dict[str, object]
    status: str
    attempt_count: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class FactoryDeliveryRow:
    """Minimal retained metadata for an inbound webhook delivery."""

    source: str
    source_event_id: str
    delivery_id: str | None
    event_type: str
    subject_id: str | None
    project_id: str | None
    repo: str | None
    payload_sha256: str
    metadata: dict[str, object]
    disposition: str
    received_at: datetime


@dataclass(frozen=True)
class FactoryWorkspaceRow:
    """One provisioned workspace for a factory job attempt."""

    external_key: str
    attempt: int
    provider: str
    template: str
    workspace_name: str
    workspace_id: str | None
    status: str
    cleanup_status: str
    last_error: str | None
    provisioned_at: datetime | None
    execution_started_at: datetime | None
    finished_at: datetime | None
    cleanup_requested_at: datetime | None
    cleanup_completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _is_postgres_url(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://"))


def _row_to_session(row: dict[str, object]) -> SessionRow:
    return SessionRow(
        session_id=str(row["session_id"]),
        pipeline_name=str(row["pipeline_name"]),
        pipeline_yaml=str(row["pipeline_yaml"]),
        runtime=str(row["runtime"]),
        model=row["model"] if row["model"] is None else str(row["model"]),
        permissions=str(row["permissions"]),
        working_dir=str(row["working_dir"]),
        output_dir=str(row["output_dir"]),
        input_files=_parse_json_col(row["input_files"]),
        timeout=_as_int(row["timeout"]),
        heartbeat_interval=_as_float(row["heartbeat_interval"]),
        status=str(row["status"]),
        gated_agent_id=(None if row["gated_agent_id"] is None else str(row["gated_agent_id"])),
        gated_agent_index=(
            None if row["gated_agent_index"] is None else _as_int(row["gated_agent_index"])
        ),
        agents_completed=_as_int(row["agents_completed"]),
        total_agents=_as_int(row["total_agents"]),
        context_files=_parse_json_col(row["context_files"]),
        last_output=row["last_output"] if row["last_output"] is None else str(row["last_output"]),
        gate_round=_as_int(row["gate_round"]),
        gate_rejected_at=(
            None if row["gate_rejected_at"] is None else str(row["gate_rejected_at"])
        ),
        branch_strategy=(
            None if row.get("branch_strategy") is None else str(row["branch_strategy"])
        ),
        branch=None if row.get("branch") is None else str(row["branch"]),
        worktree_path=(None if row.get("worktree_path") is None else str(row["worktree_path"])),
        docker_image=(None if row.get("docker_image") is None else str(row["docker_image"])),
        host_repo=None if row.get("host_repo") is None else str(row["host_repo"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _row_to_factory_job(row: dict[str, object]) -> FactoryJobRow:
    return FactoryJobRow(
        external_key=str(row["external_key"]),
        workspace_slug=str(row["workspace_slug"]),
        project_id=str(row["project_id"]),
        project_key=_optional_str(row.get("project_key")),
        work_item_id=str(row["work_item_id"]),
        work_item_ref=_optional_str(row["work_item_ref"]),
        title=_optional_str(row.get("title")),
        status=str(row["status"]),
        repo=_optional_str(row["repo"]),
        branch=_optional_str(row["branch"]),
        session_id=_optional_str(row["session_id"]),
        pr_url=_optional_str(row["pr_url"]),
        last_error=_optional_str(row["last_error"]),
        current_stage=_optional_str(row.get("current_stage")),
        current_agent=_optional_str(row.get("current_agent")),
        current_prompt=(
            None if row.get("current_prompt") is None else _as_int(row["current_prompt"])
        ),
        plane_progress_comment_id=_optional_str(row.get("plane_progress_comment_id")),
        attempt=_as_int(row.get("attempt", 1)),
        cancel_requested_at=(
            None
            if row.get("cancel_requested_at") is None
            else _parse_datetime(row["cancel_requested_at"])
        ),
        pr_repo=_optional_str(row.get("pr_repo")),
        pr_number=None if row.get("pr_number") is None else _as_int(row["pr_number"]),
        pr_state=_optional_str(row.get("pr_state")),
        ci_state=_optional_str(row.get("ci_state")),
        review_state=_optional_str(row.get("review_state")),
        completed_at=(
            None if row.get("completed_at") is None else _parse_datetime(row["completed_at"])
        ),
        last_event_at=(
            None if row.get("last_event_at") is None else _parse_datetime(row["last_event_at"])
        ),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _row_to_factory_event(row: dict[str, object]) -> FactoryEventRow:
    return FactoryEventRow(
        event_id=_as_int(row["event_id"]),
        external_key=str(row["external_key"]),
        session_id=_optional_str(row["session_id"]),
        event_type=str(row["event_type"]),
        stage=_optional_str(row["stage"]),
        status=_optional_str(row["status"]),
        message=_optional_str(row["message"]),
        metadata=_parse_json_object(row["metadata"]),
        created_at=_parse_datetime(row["created_at"]),
    )


def _row_to_factory_task(row: dict[str, object]) -> FactoryTaskRow:
    return FactoryTaskRow(
        task_id=str(row["task_id"]),
        task_type=str(row["task_type"]),
        idempotency_key=str(row["idempotency_key"]),
        external_key=_optional_str(row.get("external_key")),
        repo=_optional_str(row.get("repo")),
        metadata=_parse_json_object(row["metadata"]),
        status=str(row["status"]),
        attempt_count=_as_int(row["attempt_count"]),
        available_at=_parse_datetime(row["available_at"]),
        lease_owner=_optional_str(row.get("lease_owner")),
        lease_expires_at=(
            None
            if row.get("lease_expires_at") is None
            else _parse_datetime(row["lease_expires_at"])
        ),
        last_error=_optional_str(row.get("last_error")),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        completed_at=(
            None if row.get("completed_at") is None else _parse_datetime(row["completed_at"])
        ),
    )


def _row_to_factory_delivery(row: dict[str, object]) -> FactoryDeliveryRow:
    return FactoryDeliveryRow(
        source=str(row["source"]),
        source_event_id=str(row["source_event_id"]),
        delivery_id=_optional_str(row.get("delivery_id")),
        event_type=str(row["event_type"]),
        subject_id=_optional_str(row.get("subject_id")),
        project_id=_optional_str(row.get("project_id")),
        repo=_optional_str(row.get("repo")),
        payload_sha256=str(row["payload_sha256"]),
        metadata=_parse_json_object(row["metadata"]),
        disposition=str(row["disposition"]),
        received_at=_parse_datetime(row["received_at"]),
    )


def _row_to_factory_workspace(row: dict[str, object]) -> FactoryWorkspaceRow:
    def optional_datetime(column: str) -> datetime | None:
        value = row.get(column)
        return None if value is None else _parse_datetime(value)

    return FactoryWorkspaceRow(
        external_key=str(row["external_key"]),
        attempt=_as_int(row["attempt"]),
        provider=str(row["provider"]),
        template=str(row["template"]),
        workspace_name=str(row["workspace_name"]),
        workspace_id=_optional_str(row.get("workspace_id")),
        status=str(row["status"]),
        cleanup_status=str(row["cleanup_status"]),
        last_error=_optional_str(row.get("last_error")),
        provisioned_at=optional_datetime("provisioned_at"),
        execution_started_at=optional_datetime("execution_started_at"),
        finished_at=optional_datetime("finished_at"),
        cleanup_requested_at=optional_datetime("cleanup_requested_at"),
        cleanup_completed_at=optional_datetime("cleanup_completed_at"),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


class SessionStore:
    """Session store supporting PostgreSQL and SQLite backends."""

    def __init__(self, database_url: str) -> None:
        self._is_pg = _is_postgres_url(database_url)
        self._url = database_url
        self._sqlite_path: Path | None = None

        if self._is_pg:
            try:
                import psycopg2  # type: ignore[import-untyped]

                self._pg_conn = psycopg2.connect(database_url)
                self._pg_conn.autocommit = True
            except Exception as e:
                raise StoreError(f"Failed to connect to PostgreSQL: {e}") from e
            self._sqlite_conn = None
        else:
            db_path = self._resolve_sqlite_path(database_url).expanduser().resolve()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._sqlite_path = db_path
            self._sqlite_conn = sqlite3.connect(str(db_path))
            self._sqlite_conn.row_factory = sqlite3.Row
            self._sqlite_conn.execute("PRAGMA foreign_keys=ON")
            self._sqlite_conn.execute("PRAGMA journal_mode=WAL")
            self._pg_conn = None

    @property
    def backend(self) -> str:
        return "postgresql" if self._is_pg else "sqlite"

    @property
    def database_url(self) -> str:
        return self._url

    @staticmethod
    def _resolve_sqlite_path(url: str) -> Path:
        if url.startswith("sqlite:///"):
            return Path(url[len("sqlite:///") :])
        if url.startswith("sqlite://"):
            return Path(url[len("sqlite://") :])
        return Path(url)

    def close(self) -> None:
        if self._pg_conn:
            self._pg_conn.close()
        if self._sqlite_conn:
            self._sqlite_conn.close()

    @contextmanager
    def factory_lock(self) -> Iterator[None]:
        """Serialize factory recovery and execution across processes."""
        if self._is_pg:
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (_FACTORY_ADVISORY_LOCK_ID,))
            try:
                yield
            finally:
                with self._pg_conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (_FACTORY_ADVISORY_LOCK_ID,))
            return

        import fcntl

        assert self._sqlite_path is not None
        lock_path = self._sqlite_path.with_name(self._sqlite_path.name + ".factory.lock")
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def ensure_schema(self) -> None:
        try:
            if self._is_pg:
                assert self._pg_conn is not None
                with self._pg_conn.cursor() as cur:
                    cur.execute(_PG_SCHEMA_SQL)
                    for col, typ in (
                        ("branch_strategy", "TEXT"),
                        ("branch", "TEXT"),
                        ("worktree_path", "TEXT"),
                        ("docker_image", "TEXT"),
                        ("host_repo", "TEXT"),
                    ):
                        cur.execute(f"ALTER TABLE sessions ADD COLUMN IF NOT EXISTS {col} {typ}")
                    for col, typ in (
                        ("external_key", "TEXT"),
                        ("workspace_slug", "TEXT"),
                        ("project_id", "TEXT"),
                        ("project_key", "TEXT"),
                        ("work_item_id", "TEXT"),
                        ("work_item_ref", "TEXT"),
                        ("title", "TEXT"),
                        ("status", "TEXT"),
                        ("repo", "TEXT"),
                        ("branch", "TEXT"),
                        ("session_id", "TEXT"),
                        ("pr_url", "TEXT"),
                        ("last_error", "TEXT"),
                        ("current_stage", "TEXT"),
                        ("current_agent", "TEXT"),
                        ("current_prompt", "INTEGER"),
                        ("plane_progress_comment_id", "TEXT"),
                        ("attempt", "INTEGER NOT NULL DEFAULT 1"),
                        ("cancel_requested_at", "TIMESTAMPTZ"),
                        ("pr_repo", "TEXT"),
                        ("pr_number", "INTEGER"),
                        ("pr_state", "TEXT"),
                        ("ci_state", "TEXT"),
                        ("review_state", "TEXT"),
                        ("completed_at", "TIMESTAMPTZ"),
                        ("last_event_at", "TIMESTAMPTZ"),
                        ("created_at", "TIMESTAMPTZ"),
                        ("updated_at", "TIMESTAMPTZ"),
                    ):
                        cur.execute(
                            f"ALTER TABLE factory_jobs ADD COLUMN IF NOT EXISTS {col} {typ}"
                        )
                    cur.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS factory_jobs_plane_key "
                        "ON factory_jobs (workspace_slug, project_id, work_item_id)"
                    )
            else:
                assert self._sqlite_conn is not None
                self._sqlite_conn.executescript(_SQLITE_SCHEMA_SQL)
                existing = {
                    row[1] for row in self._sqlite_conn.execute("PRAGMA table_info(sessions)")
                }
                for col in (
                    "branch_strategy",
                    "branch",
                    "worktree_path",
                    "docker_image",
                    "host_repo",
                ):
                    if col not in existing:
                        self._sqlite_conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
                factory_existing = {
                    row[1] for row in self._sqlite_conn.execute("PRAGMA table_info(factory_jobs)")
                }
                factory_column_types = {
                    "current_prompt": "INTEGER",
                    "attempt": "INTEGER NOT NULL DEFAULT 1",
                    "pr_number": "INTEGER",
                }
                for col in _FACTORY_JOB_COLUMNS:
                    if col not in factory_existing:
                        column_type = factory_column_types.get(col, "TEXT")
                        self._sqlite_conn.execute(
                            f"ALTER TABLE factory_jobs ADD COLUMN {col} {column_type}"
                        )
                self._sqlite_conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS factory_jobs_plane_key "
                    "ON factory_jobs (workspace_slug, project_id, work_item_id)"
                )
                self._sqlite_conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS factory_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        external_key TEXT NOT NULL
                            REFERENCES factory_jobs(external_key) ON DELETE CASCADE,
                        session_id TEXT,
                        event_type TEXT NOT NULL,
                        stage TEXT,
                        status TEXT,
                        message TEXT,
                        metadata TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS factory_events_job_event
                        ON factory_events (external_key, event_id);
                    """
                )
                self._sqlite_conn.commit()
        except Exception as e:
            raise StoreError(f"Failed to create schema: {e}") from e

    def _execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        try:
            if self._is_pg:
                assert self._pg_conn is not None
                with self._pg_conn.cursor() as cur:
                    cur.execute(sql, params)
            else:
                assert self._sqlite_conn is not None
                self._sqlite_conn.execute(sql, params)
                self._sqlite_conn.commit()
        except Exception as e:
            raise StoreError(f"Database query failed: {e}") from e

    def _execute_rowcount(self, sql: str, params: tuple[object, ...] = ()) -> int:
        try:
            if self._is_pg:
                assert self._pg_conn is not None
                with self._pg_conn.cursor() as cur:
                    cur.execute(sql, params)
                    return int(cur.rowcount)
            else:
                assert self._sqlite_conn is not None
                cur = self._sqlite_conn.execute(sql, params)
                self._sqlite_conn.commit()
                return int(cur.rowcount)
        except Exception as e:
            raise StoreError(f"Database query failed: {e}") from e

    def _fetchone(self, sql: str, params: tuple[object, ...] = ()) -> dict[str, object] | None:
        try:
            if self._is_pg:
                import psycopg2.extras  # type: ignore[import-untyped]

                assert self._pg_conn is not None
                with self._pg_conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute(sql, params)
                    row = cur.fetchone()
                    return dict(row) if row else None
            else:
                assert self._sqlite_conn is not None
                cur = self._sqlite_conn.execute(sql, params)
                row = cur.fetchone()
                return dict(row) if row else None
        except Exception as e:
            raise StoreError(f"Database query failed: {e}") from e

    def _fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        try:
            if self._is_pg:
                import psycopg2.extras  # type: ignore[import-untyped]

                assert self._pg_conn is not None
                with self._pg_conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute(sql, params)
                    return [dict(r) for r in cur.fetchall()]
            else:
                assert self._sqlite_conn is not None
                cur = self._sqlite_conn.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            raise StoreError(f"Database query failed: {e}") from e

    def create_session(
        self,
        session_id: str,
        pipeline_name: str,
        pipeline_yaml: str,
        runtime: str,
        working_dir: str,
        output_dir: str,
        total_agents: int,
        model: str | None = None,
        permissions: str = "write",
        input_files: list[str] | None = None,
        timeout: int = 300,
        heartbeat_interval: float = 0,
        branch_strategy: str | None = None,
        branch: str | None = None,
        worktree_path: str | None = None,
        docker_image: str | None = None,
        host_repo: str | None = None,
    ) -> None:
        now = _now_iso()
        if self._is_pg:
            self._execute(
                """
                INSERT INTO sessions (
                    session_id, pipeline_name, pipeline_yaml, runtime, model,
                    permissions, working_dir, output_dir, input_files,
                    timeout, heartbeat_interval, status, total_agents,
                    branch_strategy, branch, worktree_path, docker_image, host_repo
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'running', %s,
                    %s, %s, %s, %s, %s
                )
                """,
                (
                    session_id,
                    pipeline_name,
                    pipeline_yaml,
                    runtime,
                    model,
                    permissions,
                    working_dir,
                    output_dir,
                    json.dumps(input_files or []),
                    timeout,
                    heartbeat_interval,
                    total_agents,
                    branch_strategy,
                    branch,
                    worktree_path,
                    docker_image,
                    host_repo,
                ),
            )
        else:
            self._execute(
                """
                INSERT INTO sessions (
                    session_id, pipeline_name, pipeline_yaml, runtime, model,
                    permissions, working_dir, output_dir, input_files,
                    timeout, heartbeat_interval, status, total_agents,
                    branch_strategy, branch, worktree_path, docker_image, host_repo,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    session_id,
                    pipeline_name,
                    pipeline_yaml,
                    runtime,
                    model,
                    permissions,
                    working_dir,
                    output_dir,
                    json.dumps(input_files or []),
                    timeout,
                    heartbeat_interval,
                    total_agents,
                    branch_strategy,
                    branch,
                    worktree_path,
                    docker_image,
                    host_repo,
                    now,
                    now,
                ),
            )

    def checkpoint(
        self,
        session_id: str,
        gated_agent_id: str,
        gated_agent_index: int,
        agents_completed: int,
        context_files: list[str],
        last_output: str,
    ) -> None:
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        params: list[object] = [
            gated_agent_id,
            gated_agent_index,
            agents_completed,
            json.dumps(context_files),
            last_output,
        ]
        if not self._is_pg:
            params.append(_now_iso())
        params.append(session_id)
        self._execute(
            f"""
            UPDATE sessions SET
                status = 'waiting',
                gated_agent_id = {ph},
                gated_agent_index = {ph},
                agents_completed = {ph},
                context_files = {ph},
                last_output = {ph},
                gate_round = 0,
                updated_at = {now_expr}
            WHERE session_id = {ph}
            """,
            tuple(params),
        )

    def load_session(self, session_id: str) -> SessionRow:
        ph = "%s" if self._is_pg else "?"
        row = self._fetchone(f"SELECT * FROM sessions WHERE session_id = {ph}", (session_id,))
        if row is None:
            raise CheckpointNotFoundError(session_id)
        return _row_to_session(row)

    def update_after_feedback(self, session_id: str, last_output: str, gate_round: int) -> None:
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        params: list[object] = [last_output, gate_round]
        if not self._is_pg:
            params.append(_now_iso())
        params.append(session_id)
        self._execute(
            f"""
            UPDATE sessions SET
                last_output = {ph},
                gate_round = {ph},
                updated_at = {now_expr}
            WHERE session_id = {ph}
            """,
            tuple(params),
        )

    def mark_resumed(self, session_id: str) -> None:
        self._update_status(session_id, "resumed")

    def update_progress(self, session_id: str, agents_completed: int) -> None:
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        params: list[object] = [agents_completed]
        if not self._is_pg:
            params.append(_now_iso())
        params.append(session_id)
        self._execute(
            f"""
            UPDATE sessions SET
                agents_completed = {ph},
                updated_at = {now_expr}
            WHERE session_id = {ph}
            """,
            tuple(params),
        )

    def mark_completed(self, session_id: str, agents_completed: int) -> None:
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        params: list[object] = [agents_completed]
        if not self._is_pg:
            params.append(_now_iso())
        params.append(session_id)
        self._execute(
            f"""
            UPDATE sessions SET
                status = 'completed',
                agents_completed = {ph},
                updated_at = {now_expr}
            WHERE session_id = {ph}
            """,
            tuple(params),
        )

    def mark_rejected(self, session_id: str, gate_rejected_at: str) -> None:
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        params: list[object] = [gate_rejected_at]
        if not self._is_pg:
            params.append(_now_iso())
        params.append(session_id)
        self._execute(
            f"""
            UPDATE sessions SET
                status = 'rejected',
                gate_rejected_at = {ph},
                updated_at = {now_expr}
            WHERE session_id = {ph}
            """,
            tuple(params),
        )

    def save_agent_output(
        self, session_id: str, agent_id: str, prompt_num: int, output_text: str
    ) -> None:
        now = _now_iso()
        if self._is_pg:
            self._execute(
                """
                INSERT INTO agent_outputs (session_id, agent_id, prompt_num, output_text)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (session_id, agent_id, prompt_num)
                DO UPDATE SET output_text = EXCLUDED.output_text
                """,
                (session_id, agent_id, prompt_num, output_text),
            )
        else:
            self._execute(
                """
                INSERT INTO agent_outputs
                    (session_id, agent_id, prompt_num, output_text, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (session_id, agent_id, prompt_num)
                DO UPDATE SET output_text = EXCLUDED.output_text
                """,
                (session_id, agent_id, prompt_num, output_text, now),
            )

    def list_sessions(self, status: str | None = None) -> list[SessionRow]:
        ph = "%s" if self._is_pg else "?"
        if status:
            rows = self._fetchall(
                f"SELECT * FROM sessions WHERE status = {ph} ORDER BY updated_at DESC",
                (status,),
            )
        else:
            rows = self._fetchall("SELECT * FROM sessions ORDER BY updated_at DESC")
        return [_row_to_session(r) for r in rows]

    def _update_status(self, session_id: str, status: str) -> None:
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        params: list[object] = [status]
        if not self._is_pg:
            params.append(_now_iso())
        params.append(session_id)
        self._execute(
            f"UPDATE sessions SET status = {ph}, updated_at = {now_expr} WHERE session_id = {ph}",
            tuple(params),
        )

    def claim_factory_job(
        self,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        *,
        project_key: str | None = None,
        work_item_ref: str | None = None,
        title: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """Atomically claim a Plane work item. Returns False if already claimed."""
        external_key = factory_job_external_key(workspace_slug, project_id, work_item_id)
        if self._is_pg:
            inserted = self._execute_rowcount(
                """
                INSERT INTO factory_jobs (
                    external_key, workspace_slug, project_id, project_key, work_item_id,
                    work_item_ref, title, status, repo, branch, session_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, 'claimed', %s, %s, %s
                )
                ON CONFLICT (workspace_slug, project_id, work_item_id) DO NOTHING
                """,
                (
                    external_key,
                    workspace_slug,
                    project_id,
                    project_key,
                    work_item_id,
                    work_item_ref,
                    title,
                    repo,
                    branch,
                    session_id,
                ),
            )
        else:
            now = _now_iso()
            inserted = self._execute_rowcount(
                """
                INSERT INTO factory_jobs (
                    external_key, workspace_slug, project_id, project_key, work_item_id,
                    work_item_ref, title, status, repo, branch, session_id,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?, ?
                )
                ON CONFLICT (workspace_slug, project_id, work_item_id) DO NOTHING
                """,
                (
                    external_key,
                    workspace_slug,
                    project_id,
                    project_key,
                    work_item_id,
                    work_item_ref,
                    title,
                    repo,
                    branch,
                    session_id,
                    now,
                    now,
                ),
            )
        return inserted > 0

    def load_factory_job(self, external_key: str) -> FactoryJobRow:
        ph = "%s" if self._is_pg else "?"
        row = self._fetchone(
            f"SELECT * FROM factory_jobs WHERE external_key = {ph}",
            (external_key,),
        )
        if row is None:
            raise StoreError(f"Factory job not found: {external_key}")
        return _row_to_factory_job(row)

    def update_factory_job(
        self,
        external_key: str,
        status: str,
        *,
        project_key: str | None | _UnsetType = UNSET,
        work_item_ref: str | None | _UnsetType = UNSET,
        title: str | None | _UnsetType = UNSET,
        repo: str | None | _UnsetType = UNSET,
        branch: str | None | _UnsetType = UNSET,
        session_id: str | None | _UnsetType = UNSET,
        pr_url: str | None | _UnsetType = UNSET,
        last_error: str | None | _UnsetType = UNSET,
        current_stage: str | None | _UnsetType = UNSET,
        current_agent: str | None | _UnsetType = UNSET,
        current_prompt: int | None | _UnsetType = UNSET,
        plane_progress_comment_id: str | None | _UnsetType = UNSET,
        cancel_requested_at: datetime | None | _UnsetType = UNSET,
        pr_repo: str | None | _UnsetType = UNSET,
        pr_number: int | None | _UnsetType = UNSET,
        pr_state: str | None | _UnsetType = UNSET,
        ci_state: str | None | _UnsetType = UNSET,
        review_state: str | None | _UnsetType = UNSET,
        completed_at: datetime | None | _UnsetType = UNSET,
    ) -> None:
        """Update status and only optional fields that were explicitly supplied."""
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        assignments = [f"status = {ph}", f"updated_at = {now_expr}"]
        params: list[object] = [status]
        if not self._is_pg:
            params.append(_now_iso())

        optional = {
            "project_key": project_key,
            "work_item_ref": work_item_ref,
            "title": title,
            "repo": repo,
            "branch": branch,
            "session_id": session_id,
            "pr_url": pr_url,
            "last_error": last_error,
            "current_stage": current_stage,
            "current_agent": current_agent,
            "current_prompt": current_prompt,
            "plane_progress_comment_id": plane_progress_comment_id,
            "cancel_requested_at": cancel_requested_at,
            "pr_repo": pr_repo,
            "pr_number": pr_number,
            "pr_state": pr_state,
            "ci_state": ci_state,
            "review_state": review_state,
            "completed_at": completed_at,
        }
        for column in _FACTORY_JOB_OPTIONAL_COLUMNS:
            value = optional[column]
            if value is not UNSET:
                assignments.append(f"{column} = {ph}")
                params.append(
                    value.isoformat() if not self._is_pg and isinstance(value, datetime) else value
                )

        params.append(external_key)
        updated = self._execute_rowcount(
            "UPDATE factory_jobs SET " + ", ".join(assignments) + f" WHERE external_key = {ph}",
            tuple(params),
        )
        if updated == 0:
            raise StoreError(f"Factory job not found: {external_key}")

    def release_factory_job(self, external_key: str, *, expected_status: str) -> bool:
        """Delete an unstarted claim only when it is still in ``expected_status``."""
        ph = "%s" if self._is_pg else "?"
        deleted = self._execute_rowcount(
            f"DELETE FROM factory_jobs WHERE external_key = {ph} AND status = {ph}",
            (external_key, expected_status),
        )
        return deleted > 0

    def list_active_factory_jobs(self) -> list[FactoryJobRow]:
        """Jobs that are not in a terminal state, oldest first for restart recovery."""
        ph = "%s" if self._is_pg else "?"
        placeholders = ", ".join(ph for _ in FACTORY_JOB_TERMINAL_STATUSES)
        rows = self._fetchall(
            "SELECT * FROM factory_jobs "
            f"WHERE status NOT IN ({placeholders}) "
            "ORDER BY created_at ASC",
            FACTORY_JOB_TERMINAL_STATUSES,
        )
        return [_row_to_factory_job(r) for r in rows]

    def list_factory_jobs(
        self,
        *,
        status: str | None = None,
        workspace_slug: str | None = None,
        project_key: str | None = None,
        limit: int = 100,
    ) -> list[FactoryJobRow]:
        """List dashboard jobs newest-first, with optional exact-match filters."""
        if limit < 1:
            return []
        ph = "%s" if self._is_pg else "?"
        predicates: list[str] = []
        params: list[object] = []
        for column, value in (
            ("status", status),
            ("workspace_slug", workspace_slug),
            ("project_key", project_key),
        ):
            if value is not None:
                predicates.append(f"{column} = {ph}")
                params.append(value)
        where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
        params.append(limit)
        rows = self._fetchall(
            "SELECT * FROM factory_jobs"
            + where
            + f" ORDER BY COALESCE(last_event_at, updated_at) DESC, external_key ASC LIMIT {ph}",
            tuple(params),
        )
        return [_row_to_factory_job(row) for row in rows]

    def append_factory_event(
        self,
        external_key: str,
        event_type: str,
        *,
        session_id: str | None = None,
        stage: str | None = None,
        status: str | None = None,
        message: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> FactoryEventRow:
        """Append a structured event and advance the job's dashboard activity time."""
        encoded_metadata = json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True)
        if self._is_pg:
            row = self._fetchone(
                """
                WITH inserted AS (
                    INSERT INTO factory_events (
                        external_key, session_id, event_type, stage, status, message, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                ),
                updated AS (
                    UPDATE factory_jobs AS jobs
                    SET last_event_at = inserted.created_at
                    FROM inserted
                    WHERE jobs.external_key = inserted.external_key
                    RETURNING jobs.external_key
                )
                SELECT inserted.*
                FROM inserted
                JOIN updated USING (external_key)
                """,
                (
                    external_key,
                    session_id,
                    event_type,
                    stage,
                    status,
                    message,
                    encoded_metadata,
                ),
            )
            assert row is not None
        else:
            assert self._sqlite_conn is not None
            created_at = _now_iso()
            try:
                with self._sqlite_conn:
                    cur = self._sqlite_conn.execute(
                        """
                        INSERT INTO factory_events (
                            external_key, session_id, event_type, stage, status,
                            message, metadata, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            external_key,
                            session_id,
                            event_type,
                            stage,
                            status,
                            message,
                            encoded_metadata,
                            created_at,
                        ),
                    )
                    if cur.lastrowid is None:
                        raise StoreError("Factory event insert did not return an event ID")
                    event_id = cur.lastrowid
                    self._sqlite_conn.execute(
                        "UPDATE factory_jobs SET last_event_at = ? WHERE external_key = ?",
                        (created_at, external_key),
                    )
            except Exception as e:
                raise StoreError(f"Database query failed: {e}") from e
            row = self._fetchone(
                "SELECT * FROM factory_events WHERE event_id = ?",
                (event_id,),
            )
            assert row is not None
        return _row_to_factory_event(row)

    def list_factory_events(
        self,
        external_key: str,
        *,
        after_id: int | None = None,
        limit: int = 200,
        newest: bool = False,
    ) -> list[FactoryEventRow]:
        """List events ascending; ``newest`` selects the latest window first."""
        if limit < 1:
            return []
        ph = "%s" if self._is_pg else "?"
        params: list[object] = [external_key]
        after = ""
        if after_id is not None:
            after = f" AND event_id > {ph}"
            params.append(after_id)
        params.append(limit)
        filtered = f"SELECT * FROM factory_events WHERE external_key = {ph}" + after
        if newest:
            sql = (
                f"SELECT * FROM ({filtered} ORDER BY event_id DESC LIMIT {ph}) "
                "AS recent_events ORDER BY event_id ASC"
            )
        else:
            sql = filtered + f" ORDER BY event_id ASC LIMIT {ph}"
        rows = self._fetchall(sql, tuple(params))
        return [_row_to_factory_event(row) for row in rows]

    def record_factory_delivery(
        self,
        *,
        source: str,
        source_event_id: str,
        delivery_id: str | None,
        event_type: str,
        payload_sha256: str,
        task_type: str,
        subject_id: str | None = None,
        project_id: str | None = None,
        repo: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> bool:
        """Atomically retain minimal delivery metadata and enqueue its first task."""
        encoded = json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True)
        task_id = uuid.uuid4().hex
        idempotency_key = f"delivery:{source}:{source_event_id}"
        if self._is_pg:
            row = self._fetchone(
                """
                WITH accepted AS (
                    INSERT INTO factory_deliveries (
                        source, source_event_id, delivery_id, event_type, subject_id,
                        project_id, repo, payload_sha256, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source, source_event_id) DO NOTHING
                    RETURNING source_event_id
                ), queued AS (
                    INSERT INTO factory_tasks (
                        task_id, task_type, idempotency_key, repo, metadata
                    )
                    SELECT %s, %s, %s, %s, %s FROM accepted
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING task_id
                )
                SELECT task_id FROM queued
                """,
                (
                    source,
                    source_event_id,
                    delivery_id,
                    event_type,
                    subject_id,
                    project_id,
                    repo,
                    payload_sha256,
                    encoded,
                    task_id,
                    task_type,
                    idempotency_key,
                    repo,
                    encoded,
                ),
            )
            return row is not None

        assert self._sqlite_conn is not None
        now = _now_iso()
        try:
            with self._sqlite_conn:
                cur = self._sqlite_conn.execute(
                    """
                    INSERT INTO factory_deliveries (
                        source, source_event_id, delivery_id, event_type, subject_id,
                        project_id, repo, payload_sha256, metadata, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source, source_event_id) DO NOTHING
                    """,
                    (
                        source,
                        source_event_id,
                        delivery_id,
                        event_type,
                        subject_id,
                        project_id,
                        repo,
                        payload_sha256,
                        encoded,
                        now,
                    ),
                )
                if cur.rowcount == 0:
                    return False
                self._sqlite_conn.execute(
                    """
                    INSERT INTO factory_tasks (
                        task_id, task_type, idempotency_key, repo, metadata,
                        available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, task_type, idempotency_key, repo, encoded, now, now, now),
                )
        except Exception as exc:
            raise StoreError(f"Database query failed: {exc}") from exc
        return True

    def enqueue_factory_task(
        self,
        task_type: str,
        idempotency_key: str,
        *,
        external_key: str | None = None,
        repo: str | None = None,
        metadata: dict[str, object] | None = None,
        available_at: datetime | None = None,
    ) -> bool:
        """Enqueue one idempotent internal factory task."""
        task_id = uuid.uuid4().hex
        encoded = json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True)
        available = available_at or datetime.now(UTC)
        if self._is_pg:
            inserted = self._execute_rowcount(
                """
                INSERT INTO factory_tasks (
                    task_id, task_type, idempotency_key, external_key, repo,
                    metadata, available_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (task_id, task_type, idempotency_key, external_key, repo, encoded, available),
            )
        else:
            stamp = _now_iso()
            inserted = self._execute_rowcount(
                """
                INSERT INTO factory_tasks (
                    task_id, task_type, idempotency_key, external_key, repo,
                    metadata, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (
                    task_id,
                    task_type,
                    idempotency_key,
                    external_key,
                    repo,
                    encoded,
                    available.isoformat(),
                    stamp,
                    stamp,
                ),
            )
        return inserted > 0

    def claim_factory_task(self, worker_id: str, *, lease_seconds: int) -> FactoryTaskRow | None:
        """Claim the oldest ready task, including work abandoned after lease expiry."""
        if self._is_pg:
            row = self._fetchone(
                """
                WITH picked AS (
                    SELECT task_id
                    FROM factory_tasks
                    WHERE (
                        status = 'pending' AND available_at <= now()
                    ) OR (
                        status = 'processing' AND lease_expires_at < now()
                    )
                    ORDER BY available_at ASC, created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE factory_tasks AS tasks
                SET status = 'processing',
                    attempt_count = attempt_count + 1,
                    lease_owner = %s,
                    lease_expires_at = now() + (%s * interval '1 second'),
                    updated_at = now()
                FROM picked
                WHERE tasks.task_id = picked.task_id
                RETURNING tasks.*
                """,
                (worker_id, lease_seconds),
            )
            return None if row is None else _row_to_factory_task(row)

        assert self._sqlite_conn is not None
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=lease_seconds)
        try:
            self._sqlite_conn.execute("BEGIN IMMEDIATE")
            row = self._sqlite_conn.execute(
                """
                SELECT * FROM factory_tasks
                WHERE (status = 'pending' AND available_at <= ?)
                   OR (status = 'processing' AND lease_expires_at < ?)
                ORDER BY available_at ASC, created_at ASC
                LIMIT 1
                """,
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                self._sqlite_conn.commit()
                return None
            self._sqlite_conn.execute(
                """
                UPDATE factory_tasks
                SET status = 'processing', attempt_count = attempt_count + 1,
                    lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (worker_id, expires.isoformat(), now.isoformat(), row["task_id"]),
            )
            self._sqlite_conn.commit()
            claimed = self._fetchone(
                "SELECT * FROM factory_tasks WHERE task_id = ?", (row["task_id"],)
            )
        except Exception as exc:
            self._sqlite_conn.rollback()
            raise StoreError(f"Database query failed: {exc}") from exc
        assert claimed is not None
        return _row_to_factory_task(claimed)

    def renew_factory_task(self, task_id: str, worker_id: str, *, lease_seconds: int) -> bool:
        ph = "%s" if self._is_pg else "?"
        if self._is_pg:
            sql = (
                "UPDATE factory_tasks SET lease_expires_at = now() + "
                f"({ph} * interval '1 second'), updated_at = now() "
                f"WHERE task_id = {ph} AND lease_owner = {ph} AND status = 'processing'"
            )
            params: tuple[object, ...] = (lease_seconds, task_id, worker_id)
        else:
            now = datetime.now(UTC)
            sql = (
                "UPDATE factory_tasks SET lease_expires_at = ?, updated_at = ? "
                "WHERE task_id = ? AND lease_owner = ? AND status = 'processing'"
            )
            params = (
                (now + timedelta(seconds=lease_seconds)).isoformat(),
                now.isoformat(),
                task_id,
                worker_id,
            )
        return self._execute_rowcount(sql, params) > 0

    def complete_factory_task(self, task_id: str, worker_id: str) -> bool:
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        params: list[object] = []
        if not self._is_pg:
            stamp = _now_iso()
            params.extend((stamp, stamp))
        params.extend((task_id, worker_id))
        return (
            self._execute_rowcount(
                f"""
                UPDATE factory_tasks
                SET status = 'completed', lease_owner = NULL, lease_expires_at = NULL,
                    completed_at = {now_expr}, updated_at = {now_expr}, last_error = NULL
                WHERE task_id = {ph} AND lease_owner = {ph} AND status = 'processing'
                """,
                tuple(params),
            )
            > 0
        )

    def fail_factory_task(
        self,
        task_id: str,
        worker_id: str,
        error: str,
        *,
        max_attempts: int,
        delay_seconds: float,
    ) -> str:
        """Retry or dead-letter a claimed task; return the resulting status."""
        task = self.load_factory_task(task_id)
        status = "dead" if task.attempt_count >= max_attempts else "pending"
        available = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        params: list[object] = [status, error[:400]]
        if self._is_pg:
            params.append(available)
        else:
            params.extend((available.isoformat(), _now_iso()))
        params.extend((task_id, worker_id))
        updated = self._execute_rowcount(
            f"""
            UPDATE factory_tasks
            SET status = {ph}, last_error = {ph}, available_at = {ph},
                lease_owner = NULL, lease_expires_at = NULL, updated_at = {now_expr}
            WHERE task_id = {ph} AND lease_owner = {ph} AND status = 'processing'
            """,
            tuple(params),
        )
        if updated == 0:
            raise StoreError(f"Factory task lease lost: {task_id}")
        return status

    def defer_factory_task(self, task_id: str, worker_id: str, *, delay_seconds: float) -> bool:
        """Release a task without consuming an attempt, typically for repo contention."""
        available = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        params: list[object] = [available if self._is_pg else available.isoformat()]
        if not self._is_pg:
            params.append(_now_iso())
        params.extend((task_id, worker_id))
        return (
            self._execute_rowcount(
                f"""
                UPDATE factory_tasks
                SET status = 'pending', available_at = {ph},
                    attempt_count = CASE WHEN attempt_count > 0 THEN attempt_count - 1 ELSE 0 END,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = {now_expr}
                WHERE task_id = {ph} AND lease_owner = {ph} AND status = 'processing'
                """,
                tuple(params),
            )
            > 0
        )

    def load_factory_task(self, task_id: str) -> FactoryTaskRow:
        ph = "%s" if self._is_pg else "?"
        row = self._fetchone(f"SELECT * FROM factory_tasks WHERE task_id = {ph}", (task_id,))
        if row is None:
            raise StoreError(f"Factory task not found: {task_id}")
        return _row_to_factory_task(row)

    def load_factory_delivery(self, source: str, source_event_id: str) -> FactoryDeliveryRow:
        ph = "%s" if self._is_pg else "?"
        row = self._fetchone(
            f"SELECT * FROM factory_deliveries WHERE source = {ph} AND source_event_id = {ph}",
            (source, source_event_id),
        )
        if row is None:
            raise StoreError(f"Factory delivery not found: {source}:{source_event_id}")
        return _row_to_factory_delivery(row)

    def replay_factory_delivery(self, source: str, source_event_id: str) -> bool:
        delivery = self.load_factory_delivery(source, source_event_id)
        replay_id = uuid.uuid4().hex
        task_type = "factory.delivery.run" if source == "plane" else "factory.github.sync"
        return self.enqueue_factory_task(
            task_type,
            f"replay:{source}:{source_event_id}:{replay_id}",
            repo=delivery.repo,
            metadata=delivery.metadata,
        )

    def list_factory_tasks(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[FactoryTaskRow]:
        if limit < 1:
            return []
        ph = "%s" if self._is_pg else "?"
        params: list[object] = []
        where = ""
        if status is not None:
            where = f" WHERE status = {ph}"
            params.append(status)
        params.append(limit)
        rows = self._fetchall(
            "SELECT * FROM factory_tasks" + where + f" ORDER BY created_at DESC LIMIT {ph}",
            tuple(params),
        )
        return [_row_to_factory_task(row) for row in rows]

    def factory_task_summary(self) -> dict[str, object]:
        rows = self._fetchall("SELECT status, COUNT(*) AS count FROM factory_tasks GROUP BY status")
        counts = {str(row["status"]): _as_int(row["count"]) for row in rows}
        row = self._fetchone(
            "SELECT MIN(available_at) AS oldest FROM factory_tasks WHERE status = 'pending'"
        )
        lease_row = self._fetchone("SELECT COUNT(*) AS count FROM factory_repo_leases")
        return {
            "counts": counts,
            "oldest_pending_at": (
                None if row is None or row.get("oldest") is None else str(row["oldest"])
            ),
            "repo_leases": 0 if lease_row is None else _as_int(lease_row["count"]),
        }

    def acquire_repo_lease(
        self,
        repo: str,
        worker_id: str,
        task_id: str,
        *,
        lease_seconds: int,
    ) -> bool:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=lease_seconds)
        if self._is_pg:
            return (
                self._execute_rowcount(
                    """
                    INSERT INTO factory_repo_leases (
                        repo, worker_id, task_id, lease_expires_at
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (repo) DO UPDATE SET
                        worker_id = EXCLUDED.worker_id,
                        task_id = EXCLUDED.task_id,
                        lease_expires_at = EXCLUDED.lease_expires_at,
                        heartbeat_at = now()
                    WHERE factory_repo_leases.lease_expires_at < now()
                       OR (factory_repo_leases.worker_id = EXCLUDED.worker_id
                           AND factory_repo_leases.task_id = EXCLUDED.task_id)
                    """,
                    (repo, worker_id, task_id, expires),
                )
                > 0
            )
        return (
            self._execute_rowcount(
                """
                INSERT INTO factory_repo_leases (
                    repo, worker_id, task_id, lease_expires_at, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (repo) DO UPDATE SET
                    worker_id = excluded.worker_id,
                    task_id = excluded.task_id,
                    lease_expires_at = excluded.lease_expires_at,
                    heartbeat_at = excluded.heartbeat_at
                WHERE factory_repo_leases.lease_expires_at < excluded.heartbeat_at
                   OR (factory_repo_leases.worker_id = excluded.worker_id
                       AND factory_repo_leases.task_id = excluded.task_id)
                """,
                (repo, worker_id, task_id, expires.isoformat(), now.isoformat()),
            )
            > 0
        )

    def renew_repo_lease(
        self, repo: str, worker_id: str, task_id: str, *, lease_seconds: int
    ) -> bool:
        now = datetime.now(UTC)
        if self._is_pg:
            sql = (
                "UPDATE factory_repo_leases SET lease_expires_at = %s, heartbeat_at = now() "
                "WHERE repo = %s AND worker_id = %s AND task_id = %s"
            )
            params: tuple[object, ...] = (
                now + timedelta(seconds=lease_seconds),
                repo,
                worker_id,
                task_id,
            )
        else:
            sql = (
                "UPDATE factory_repo_leases SET lease_expires_at = ?, heartbeat_at = ? "
                "WHERE repo = ? AND worker_id = ? AND task_id = ?"
            )
            params = (
                (now + timedelta(seconds=lease_seconds)).isoformat(),
                now.isoformat(),
                repo,
                worker_id,
                task_id,
            )
        return self._execute_rowcount(sql, params) > 0

    def release_repo_lease(self, repo: str, worker_id: str, task_id: str) -> None:
        ph = "%s" if self._is_pg else "?"
        self._execute(
            "DELETE FROM factory_repo_leases "
            f"WHERE repo = {ph} AND worker_id = {ph} AND task_id = {ph}",
            (repo, worker_id, task_id),
        )

    def find_factory_job_by_pr(self, repo: str, pr_number: int) -> FactoryJobRow | None:
        ph = "%s" if self._is_pg else "?"
        row = self._fetchone(
            f"SELECT * FROM factory_jobs WHERE pr_repo = {ph} AND pr_number = {ph}",
            (repo, pr_number),
        )
        return None if row is None else _row_to_factory_job(row)

    def mark_factory_job_published(
        self,
        external_key: str,
        *,
        session_id: str,
        branch: str,
        pr_url: str,
        pr_repo: str,
        pr_number: int,
        enqueue_review_sync: bool,
    ) -> None:
        """Persist publication and its follow-up task atomically."""
        if self._is_pg:
            task_id = uuid.uuid4().hex
            row = self._fetchone(
                """
                WITH published AS (
                    UPDATE factory_jobs
                    SET status = 'published', session_id = %s, branch = %s,
                        pr_url = %s, pr_repo = %s, pr_number = %s,
                        pr_state = 'draft', updated_at = now()
                    WHERE external_key = %s
                    RETURNING external_key, repo
                ), queued AS (
                    INSERT INTO factory_tasks (
                        task_id, task_type, idempotency_key, external_key, repo
                    )
                    SELECT %s, 'factory.plane.review.sync', %s, external_key, repo
                    FROM published WHERE %s
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING task_id
                )
                SELECT external_key FROM published
                """,
                (
                    session_id,
                    branch,
                    pr_url,
                    pr_repo,
                    pr_number,
                    external_key,
                    task_id,
                    f"plane-review:{external_key}",
                    enqueue_review_sync,
                ),
            )
            if row is None:
                raise StoreError(f"Factory job not found: {external_key}")
            return

        assert self._sqlite_conn is not None
        now = _now_iso()
        try:
            with self._sqlite_conn:
                cur = self._sqlite_conn.execute(
                    """
                    UPDATE factory_jobs
                    SET status = 'published', session_id = ?, branch = ?, pr_url = ?,
                        pr_repo = ?, pr_number = ?, pr_state = 'draft', updated_at = ?
                    WHERE external_key = ?
                    """,
                    (
                        session_id,
                        branch,
                        pr_url,
                        pr_repo,
                        pr_number,
                        now,
                        external_key,
                    ),
                )
                if cur.rowcount == 0:
                    raise StoreError(f"Factory job not found: {external_key}")
                if enqueue_review_sync:
                    self._sqlite_conn.execute(
                        """
                        INSERT INTO factory_tasks (
                            task_id, task_type, idempotency_key, external_key, repo,
                            available_at, created_at, updated_at
                        )
                        SELECT ?, 'factory.plane.review.sync', ?, external_key, repo, ?, ?, ?
                        FROM factory_jobs WHERE external_key = ?
                        ON CONFLICT (idempotency_key) DO NOTHING
                        """,
                        (
                            uuid.uuid4().hex,
                            f"plane-review:{external_key}",
                            now,
                            now,
                            now,
                            external_key,
                        ),
                    )
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError(f"Database query failed: {exc}") from exc

    def request_factory_job_cancel(self, external_key: str) -> bool:
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        params: list[object] = []
        if not self._is_pg:
            params.append(_now_iso())
        params.append(external_key)
        return (
            self._execute_rowcount(
                f"UPDATE factory_jobs SET cancel_requested_at = {now_expr} "
                f"WHERE external_key = {ph} AND status NOT IN ('completed', 'failed', 'cancelled')",
                tuple(params),
            )
            > 0
        )

    def is_factory_job_cancel_requested(self, external_key: str) -> bool:
        ph = "%s" if self._is_pg else "?"
        row = self._fetchone(
            f"SELECT cancel_requested_at FROM factory_jobs WHERE external_key = {ph}",
            (external_key,),
        )
        return row is not None and row.get("cancel_requested_at") is not None

    def retry_factory_job(self, external_key: str) -> bool:
        """Reset a failed/cancelled unpublished job for an explicit new attempt."""
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        params: list[object] = []
        if not self._is_pg:
            params.append(_now_iso())
        params.append(external_key)
        return (
            self._execute_rowcount(
                f"""
                UPDATE factory_jobs
                SET status = 'claimed', attempt = attempt + 1,
                    cancel_requested_at = NULL, session_id = NULL, branch = NULL,
                    last_error = NULL, current_stage = 'retry', current_agent = NULL,
                    current_prompt = NULL, updated_at = {now_expr}
                WHERE external_key = {ph}
                  AND status IN ('failed', 'cancelled')
                  AND pr_url IS NULL
                """,
                tuple(params),
            )
            > 0
        )

    def record_factory_workspace(
        self,
        external_key: str,
        attempt: int,
        *,
        workspace_name: str,
        provider: str,
        template: str,
        workspace_id: str | None = None,
    ) -> FactoryWorkspaceRow:
        """Idempotently record the stable workspace identity for one attempt."""
        if attempt < 1:
            raise StoreError("Factory workspace attempt must be at least 1")

        if self._is_pg:
            self._execute(
                """
                INSERT INTO factory_workspaces (
                    external_key, attempt, provider, template, workspace_name, workspace_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (external_key, attempt) DO UPDATE SET
                    workspace_id = COALESCE(
                        factory_workspaces.workspace_id, EXCLUDED.workspace_id
                    ),
                    updated_at = now()
                WHERE factory_workspaces.provider = EXCLUDED.provider
                  AND factory_workspaces.template = EXCLUDED.template
                  AND factory_workspaces.workspace_name = EXCLUDED.workspace_name
                  AND (
                      factory_workspaces.workspace_id IS NULL
                      OR EXCLUDED.workspace_id IS NULL
                      OR factory_workspaces.workspace_id = EXCLUDED.workspace_id
                  )
                """,
                (external_key, attempt, provider, template, workspace_name, workspace_id),
            )
        else:
            now = _now_iso()
            self._execute(
                """
                INSERT INTO factory_workspaces (
                    external_key, attempt, provider, template, workspace_name, workspace_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (external_key, attempt) DO UPDATE SET
                    workspace_id = COALESCE(
                        factory_workspaces.workspace_id, excluded.workspace_id
                    ),
                    updated_at = excluded.updated_at
                WHERE factory_workspaces.provider = excluded.provider
                  AND factory_workspaces.template = excluded.template
                  AND factory_workspaces.workspace_name = excluded.workspace_name
                  AND (
                      factory_workspaces.workspace_id IS NULL
                      OR excluded.workspace_id IS NULL
                      OR factory_workspaces.workspace_id = excluded.workspace_id
                  )
                """,
                (
                    external_key,
                    attempt,
                    provider,
                    template,
                    workspace_name,
                    workspace_id,
                    now,
                    now,
                ),
            )

        workspace = self.load_factory_workspace(external_key, attempt)
        identity = (provider, template, workspace_name)
        recorded_identity = (
            workspace.provider,
            workspace.template,
            workspace.workspace_name,
        )
        id_matches = (
            workspace_id is None
            or workspace.workspace_id is None
            or workspace.workspace_id == workspace_id
        )
        if recorded_identity != identity or not id_matches:
            raise StoreError(
                f"Factory workspace identity conflict: {external_key} attempt {attempt}"
            )
        return workspace

    def update_factory_workspace_lifecycle(
        self,
        external_key: str,
        attempt: int,
        status: str,
        *,
        workspace_id: str | None | _UnsetType = UNSET,
        last_error: str | None | _UnsetType = UNSET,
    ) -> FactoryWorkspaceRow:
        """Advance workspace lifecycle state and its corresponding milestone timestamp."""
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        assignments = [f"status = {ph}", f"updated_at = {now_expr}"]
        params: list[object] = [status]
        if not self._is_pg:
            params.append(_now_iso())

        if workspace_id is not UNSET:
            assignments.append(f"workspace_id = COALESCE(workspace_id, {ph})")
            params.append(workspace_id)
        if last_error is not UNSET:
            assignments.append(f"last_error = {ph}")
            params.append(last_error)

        milestone_column: str | None = None
        if status in {"ready", "provisioned"}:
            milestone_column = "provisioned_at"
        elif status in {"running", "executing"}:
            milestone_column = "execution_started_at"
        elif status in {"completed", "failed", "cancelled"}:
            milestone_column = "finished_at"
        if milestone_column is not None:
            assignments.append(f"{milestone_column} = COALESCE({milestone_column}, {now_expr})")
            if not self._is_pg:
                params.append(_now_iso())

        params.extend((external_key, attempt))
        updated = self._execute_rowcount(
            "UPDATE factory_workspaces SET "
            + ", ".join(assignments)
            + f" WHERE external_key = {ph} AND attempt = {ph}",
            tuple(params),
        )
        if updated == 0:
            raise StoreError(f"Factory workspace not found: {external_key} attempt {attempt}")
        workspace = self.load_factory_workspace(external_key, attempt)
        if (
            workspace_id is not UNSET
            and workspace_id is not None
            and workspace.workspace_id != workspace_id
        ):
            raise StoreError(
                f"Factory workspace identity conflict: {external_key} attempt {attempt}"
            )
        return workspace

    def load_factory_workspace(self, external_key: str, attempt: int) -> FactoryWorkspaceRow:
        ph = "%s" if self._is_pg else "?"
        row = self._fetchone(
            f"SELECT * FROM factory_workspaces WHERE external_key = {ph} AND attempt = {ph}",
            (external_key, attempt),
        )
        if row is None:
            raise StoreError(f"Factory workspace not found: {external_key} attempt {attempt}")
        return _row_to_factory_workspace(row)

    def list_factory_workspaces(
        self,
        *,
        external_key: str | None = None,
        provider: str | None = None,
        status: str | None = None,
        cleanup_status: str | None = None,
        limit: int = 100,
    ) -> list[FactoryWorkspaceRow]:
        """List workspace attempts newest-first with optional exact-match filters."""
        if limit < 1:
            return []
        ph = "%s" if self._is_pg else "?"
        predicates: list[str] = []
        params: list[object] = []
        for column, value in (
            ("external_key", external_key),
            ("provider", provider),
            ("status", status),
            ("cleanup_status", cleanup_status),
        ):
            if value is not None:
                predicates.append(f"{column} = {ph}")
                params.append(value)
        where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
        params.append(limit)
        rows = self._fetchall(
            "SELECT * FROM factory_workspaces"
            + where
            + f" ORDER BY updated_at DESC, external_key ASC, attempt DESC LIMIT {ph}",
            tuple(params),
        )
        return [_row_to_factory_workspace(row) for row in rows]

    def find_factory_workspace_cleanup_candidates(
        self, *, limit: int = 100
    ) -> list[FactoryWorkspaceRow]:
        """Return cleanup work, including failed deletions eligible for retry."""
        if limit < 1:
            return []
        ph = "%s" if self._is_pg else "?"
        rows = self._fetchall(
            "SELECT * FROM factory_workspaces "
            "WHERE cleanup_status IN ('pending', 'failed') "
            f"ORDER BY cleanup_requested_at ASC, updated_at ASC LIMIT {ph}",
            (limit,),
        )
        return [_row_to_factory_workspace(row) for row in rows]

    def mark_factory_workspace_cleanup_pending(self, external_key: str, attempt: int) -> bool:
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        params: list[object] = []
        if not self._is_pg:
            now = _now_iso()
            params.extend((now, now))
        params.extend((external_key, attempt))
        return (
            self._execute_rowcount(
                "UPDATE factory_workspaces SET cleanup_status = 'pending', "
                f"cleanup_requested_at = COALESCE(cleanup_requested_at, {now_expr}), "
                f"cleanup_completed_at = NULL, updated_at = {now_expr} "
                f"WHERE external_key = {ph} AND attempt = {ph}",
                tuple(params),
            )
            > 0
        )

    def mark_factory_workspace_cleanup_complete(self, external_key: str, attempt: int) -> bool:
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        params: list[object] = []
        if not self._is_pg:
            now = _now_iso()
            params.extend((now, now))
        params.extend((external_key, attempt))
        return (
            self._execute_rowcount(
                "UPDATE factory_workspaces SET cleanup_status = 'completed', "
                f"cleanup_completed_at = {now_expr}, updated_at = {now_expr} "
                f"WHERE external_key = {ph} AND attempt = {ph}",
                tuple(params),
            )
            > 0
        )

    def mark_factory_workspace_cleanup_failed(
        self, external_key: str, attempt: int, last_error: str
    ) -> bool:
        ph = "%s" if self._is_pg else "?"
        now_expr = "now()" if self._is_pg else ph
        params: list[object] = [last_error]
        if not self._is_pg:
            params.append(_now_iso())
        params.extend((external_key, attempt))
        return (
            self._execute_rowcount(
                "UPDATE factory_workspaces SET cleanup_status = 'failed', "
                f"last_error = {ph}, updated_at = {now_expr} "
                f"WHERE external_key = {ph} AND attempt = {ph}",
                tuple(params),
            )
            > 0
        )
