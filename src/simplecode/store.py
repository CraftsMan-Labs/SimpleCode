"""Session store for checkpoint/resume — supports PostgreSQL and SQLite."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_json_col(value: object) -> list[str]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return []


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

    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


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
        timeout=int(row["timeout"]),  # type: ignore[arg-type]
        heartbeat_interval=float(row["heartbeat_interval"]),  # type: ignore[arg-type]
        status=str(row["status"]),
        gated_agent_id=row["gated_agent_id"] if row["gated_agent_id"] is None else str(row["gated_agent_id"]),
        gated_agent_index=row["gated_agent_index"] if row["gated_agent_index"] is None else int(row["gated_agent_index"]),  # type: ignore[arg-type]
        agents_completed=int(row["agents_completed"]),  # type: ignore[arg-type]
        total_agents=int(row["total_agents"]),  # type: ignore[arg-type]
        context_files=_parse_json_col(row["context_files"]),
        last_output=row["last_output"] if row["last_output"] is None else str(row["last_output"]),
        gate_round=int(row["gate_round"]),  # type: ignore[arg-type]
        gate_rejected_at=row["gate_rejected_at"] if row["gate_rejected_at"] is None else str(row["gate_rejected_at"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


class SessionStore:
    """Session store supporting PostgreSQL and SQLite backends."""

    def __init__(self, database_url: str) -> None:
        self._is_pg = _is_postgres_url(database_url)
        self._url = database_url

        if self._is_pg:
            try:
                import psycopg2

                self._pg_conn = psycopg2.connect(database_url)
                self._pg_conn.autocommit = True
            except Exception as e:
                raise StoreError(f"Failed to connect to PostgreSQL: {e}") from e
            self._sqlite_conn = None
        else:
            db_path = self._resolve_sqlite_path(database_url)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._sqlite_conn = sqlite3.connect(str(db_path))
            self._sqlite_conn.row_factory = sqlite3.Row
            self._sqlite_conn.execute("PRAGMA journal_mode=WAL")
            self._pg_conn = None

    @property
    def backend(self) -> str:
        return "postgresql" if self._is_pg else "sqlite"

    @staticmethod
    def _resolve_sqlite_path(url: str) -> Path:
        if url.startswith("sqlite:///"):
            return Path(url[len("sqlite:///"):])
        if url.startswith("sqlite://"):
            return Path(url[len("sqlite://"):])
        return Path(url)

    def close(self) -> None:
        if self._pg_conn:
            self._pg_conn.close()
        if self._sqlite_conn:
            self._sqlite_conn.close()

    def ensure_schema(self) -> None:
        try:
            if self._is_pg:
                import psycopg2

                assert self._pg_conn is not None
                with self._pg_conn.cursor() as cur:
                    cur.execute(_PG_SCHEMA_SQL)
            else:
                assert self._sqlite_conn is not None
                self._sqlite_conn.executescript(_SQLITE_SCHEMA_SQL)
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

    def _fetchone(self, sql: str, params: tuple[object, ...] = ()) -> dict[str, object] | None:
        try:
            if self._is_pg:
                import psycopg2.extras

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
                import psycopg2.extras

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

    def _now_expr(self) -> str:
        return "now()" if self._is_pg else "?"

    def _now_params(self) -> tuple[str, ...]:
        return () if self._is_pg else (_now_iso(),)

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
    ) -> None:
        now = _now_iso()
        if self._is_pg:
            self._execute(
                """
                INSERT INTO sessions (
                    session_id, pipeline_name, pipeline_yaml, runtime, model,
                    permissions, working_dir, output_dir, input_files,
                    timeout, heartbeat_interval, status, total_agents
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'running', %s)
                """,
                (
                    session_id, pipeline_name, pipeline_yaml, runtime, model,
                    permissions, working_dir, output_dir, json.dumps(input_files or []),
                    timeout, heartbeat_interval, total_agents,
                ),
            )
        else:
            self._execute(
                """
                INSERT INTO sessions (
                    session_id, pipeline_name, pipeline_yaml, runtime, model,
                    permissions, working_dir, output_dir, input_files,
                    timeout, heartbeat_interval, status, total_agents,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    session_id, pipeline_name, pipeline_yaml, runtime, model,
                    permissions, working_dir, output_dir, json.dumps(input_files or []),
                    timeout, heartbeat_interval, total_agents, now, now,
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
            gated_agent_id, gated_agent_index, agents_completed,
            json.dumps(context_files), last_output,
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

    def update_after_feedback(
        self, session_id: str, last_output: str, gate_round: int
    ) -> None:
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
                INSERT INTO agent_outputs (session_id, agent_id, prompt_num, output_text, created_at)
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
