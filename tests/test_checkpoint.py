"""Tests for checkpoint/resume via gate_mode=checkpoint."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from simplecode.gate import Gate, GateResult
from simplecode.pipeline import Pipeline, PipelineConfig
from simplecode.store import SessionStore


def test_gate_checkpoint_returns_immediately(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    gate = Gate(output_dir=output_dir)
    result = gate.run(
        agent_id="test-agent",
        last_output="Agent output here",
        resume_fn=lambda f: "response",
        gate_mode="checkpoint",
    )

    assert result == GateResult.CHECKPOINTED


def test_pipeline_checkpoints_at_gate(gated_pipeline_dir: Path, tmp_path: Path) -> None:
    config = PipelineConfig.load(gated_pipeline_dir / "pipeline.yaml")
    pipeline = Pipeline(config, gated_pipeline_dir)

    output_dir = tmp_path / "output"

    def mock_run(*args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "planner output"
        result.stderr = ""
        return result

    with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
        result = pipeline.run(
            runtime="claude",
            working_dir=str(tmp_path),
            output_dir=str(output_dir),
            gate_mode="checkpoint",
            timeout=60,
        )

    assert result.status == "checkpointed"
    assert result.checkpoint_agent == "planner"
    assert result.agents_completed == 0

    metadata = json.loads((result.output_dir / "pipeline.json").read_text())
    assert metadata["status"] == "checkpointed"
    assert metadata["checkpoint_agent"] == "planner"


def test_pipeline_no_gate_completes_normally(pipeline_dir: Path, tmp_path: Path) -> None:
    """Pipeline without gates should complete normally even with checkpoint gate_mode."""
    config = PipelineConfig.load(pipeline_dir / "pipeline.yaml")
    pipeline = Pipeline(config, pipeline_dir)

    output_dir = tmp_path / "output"

    def mock_run(*args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "agent output"
        result.stderr = ""
        return result

    with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
        result = pipeline.run(
            runtime="claude",
            working_dir=str(tmp_path),
            output_dir=str(output_dir),
            gate_mode="checkpoint",
            timeout=60,
        )

    assert result.status == "complete"
    assert result.agents_completed == 2


def test_resume_gate_uses_session_working_dir(
    gated_pipeline_dir: Path, tmp_path: Path
) -> None:
    config = PipelineConfig.load(gated_pipeline_dir / "pipeline.yaml")
    pipeline = Pipeline(
        config, gated_pipeline_dir, pipeline_yaml_path=gated_pipeline_dir / "pipeline.yaml"
    )
    store = SessionStore(str(tmp_path / "sessions.db"))
    store.ensure_schema()
    work = tmp_path / "work"
    work.mkdir()
    output_dir = tmp_path / "output"

    def mock_run(*args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "planner output"
        result.stderr = ""
        return result

    with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
        first = pipeline.run(
            runtime="claude",
            working_dir=str(work),
            output_dir=str(output_dir),
            gate_mode="checkpoint",
            timeout=60,
            store=store,
        )

    captured: list[str] = []

    def mock_feedback(*args, **kwargs):
        captured.append(str(kwargs.get("cwd", "")))
        result = MagicMock()
        result.returncode = 0
        result.stdout = "revised plan"
        result.stderr = ""
        return result

    def fake_gate_run(self, agent_id, last_output, resume_fn, gate_mode="stdin", gate_dir=None):
        resume_fn("add tests")
        return GateResult.PROCEED

    with (
        patch("simplecode.runtime.subprocess.run", side_effect=mock_feedback),
        patch("simplecode.pipeline.Gate.run", fake_gate_run),
    ):
        pipeline.resume(session_id=first.session_id, store=store)

    store.close()
    assert captured
    assert all(cwd == str(work) for cwd in captured)


class TestSQLiteStore:
    """Test SessionStore with SQLite backend."""

    def test_create_and_load_session(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        store = SessionStore(str(db_path))
        store.ensure_schema()

        store.create_session(
            session_id="s1",
            pipeline_name="test-pipe",
            pipeline_yaml="/tmp/pipeline.yaml",
            runtime="claude",
            working_dir="/tmp",
            output_dir="/tmp/output",
            total_agents=3,
            model="opus",
        )

        session = store.load_session("s1")
        assert session.session_id == "s1"
        assert session.pipeline_name == "test-pipe"
        assert session.status == "running"
        assert session.total_agents == 3
        assert session.model == "opus"
        store.close()

    def test_checkpoint_and_resume_flow(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        store = SessionStore(str(db_path))
        store.ensure_schema()

        store.create_session(
            session_id="s2",
            pipeline_name="test-pipe",
            pipeline_yaml="/tmp/pipeline.yaml",
            runtime="claude",
            working_dir="/tmp",
            output_dir="/tmp/output",
            total_agents=2,
        )

        store.checkpoint(
            session_id="s2",
            gated_agent_id="planner",
            gated_agent_index=0,
            agents_completed=0,
            context_files=["/tmp/output/s2/planner/output.md"],
            last_output="plan output",
        )

        session = store.load_session("s2")
        assert session.status == "waiting"
        assert session.gated_agent_id == "planner"
        assert session.gated_agent_index == 0
        assert session.last_output == "plan output"
        assert session.context_files == ["/tmp/output/s2/planner/output.md"]

        store.update_after_feedback("s2", "updated output", 1)
        session = store.load_session("s2")
        assert session.gate_round == 1
        assert session.last_output == "updated output"

        store.mark_resumed("s2")
        session = store.load_session("s2")
        assert session.status == "resumed"

        store.mark_completed("s2", 2)
        session = store.load_session("s2")
        assert session.status == "completed"
        assert session.agents_completed == 2

        store.close()

    def test_list_sessions_by_status(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        store = SessionStore(str(db_path))
        store.ensure_schema()

        store.create_session(
            session_id="a1",
            pipeline_name="pipe-a",
            pipeline_yaml="/tmp/a.yaml",
            runtime="claude",
            working_dir="/tmp",
            output_dir="/tmp/out",
            total_agents=1,
        )
        store.create_session(
            session_id="b2",
            pipeline_name="pipe-b",
            pipeline_yaml="/tmp/b.yaml",
            runtime="claude",
            working_dir="/tmp",
            output_dir="/tmp/out",
            total_agents=1,
        )
        store.checkpoint(
            session_id="a1",
            gated_agent_id="agent",
            gated_agent_index=0,
            agents_completed=0,
            context_files=[],
            last_output="output",
        )

        waiting = store.list_sessions(status="waiting")
        assert len(waiting) == 1
        assert waiting[0].session_id == "a1"

        running = store.list_sessions(status="running")
        assert len(running) == 1
        assert running[0].session_id == "b2"

        all_sessions = store.list_sessions()
        assert len(all_sessions) == 2

        store.close()

    def test_save_agent_output(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        store = SessionStore(str(db_path))
        store.ensure_schema()

        store.create_session(
            session_id="s3",
            pipeline_name="test",
            pipeline_yaml="/tmp/p.yaml",
            runtime="claude",
            working_dir="/tmp",
            output_dir="/tmp/out",
            total_agents=1,
        )

        store.save_agent_output("s3", "agent-a", 1, "first output")
        store.save_agent_output("s3", "agent-a", 1, "updated output")

        store.close()

    def test_checkpoint_not_found(self, tmp_path: Path) -> None:
        import pytest

        from simplecode.errors import CheckpointNotFoundError

        db_path = tmp_path / "test.db"
        store = SessionStore(str(db_path))
        store.ensure_schema()

        with pytest.raises(CheckpointNotFoundError):
            store.load_session("nonexistent")

        store.close()
