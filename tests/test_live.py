"""Live tests — actually invoke CLI runtimes against a real repo.

Run with:
    uv run pytest tests/test_live.py -m live -v

These tests require 'claude' (or another runtime) to be installed and
authenticated. They run real agent sessions, so they cost API tokens
and take time.

Control which runtime to test via the SIMPLECODE_TEST_RUNTIME env var
(default: claude).
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import pytest

from simplecode.pipeline import Pipeline, PipelineConfig
from simplecode.runner import AgentRunner
from simplecode.runtime import RUNTIMES

RUNTIME = os.environ.get("SIMPLECODE_TEST_RUNTIME", "claude")
TIMEOUT = int(os.environ.get("SIMPLECODE_TEST_TIMEOUT", "120"))

_skip_reason = None
if RUNTIME not in RUNTIMES:
    _skip_reason = f"Unknown runtime: {RUNTIME}"
else:
    binary = RUNTIMES[RUNTIME].cmd_new[0]
    if not shutil.which(binary):
        _skip_reason = f"Runtime binary '{binary}' not found on PATH"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(_skip_reason is not None, reason=_skip_reason or ""),
]


@pytest.fixture()
def target_repo(tmp_path: Path) -> Path:
    """Create a tiny target repo for agents to work on."""
    repo = tmp_path / "target"
    repo.mkdir()
    (repo / "hello.py").write_text('def greet(name: str) -> str:\n    return f"Hello, {name}!"\n')
    (repo / "README.md").write_text("# Test Project\n\nA tiny project for testing.\n")
    return repo


class TestLiveSingleAgent:
    """Test a single AgentRunner against the real CLI."""

    def test_single_prompt(self, tmp_path: Path, target_repo: Path) -> None:
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "1.md").write_text(
            "List all files in this project. "
            "Reply with ONLY the filenames, one per line. Nothing else."
        )

        output_dir = tmp_path / "output"
        runner = AgentRunner(
            agent_id="single",
            runtime_config=RUNTIMES[RUNTIME],
            output_dir=output_dir,
            timeout=TIMEOUT,
            session_id="live-test-single",
            permissions="read-only",
        )

        result = runner.run(
            prompts_dir=prompts,
            working_dir=target_repo,
        )

        assert result.success is True
        assert result.prompt_count == 1
        assert (output_dir / "single" / "output.md").exists()
        assert (output_dir / "single" / "1-output.md").exists()

        output_text = (output_dir / "single" / "1-output.md").read_text()
        assert len(output_text) > 0

    def test_multi_prompt_same_session(self, tmp_path: Path, target_repo: Path) -> None:
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "1.md").write_text(
            "Read the file hello.py and tell me the function name. "
            "Reply with ONLY the function name."
        )
        (prompts / "2.md").write_text(
            "What was the function name from your previous answer? "
            "Reply with ONLY the function name again to confirm session continuity."
        )

        output_dir = tmp_path / "output"
        runner = AgentRunner(
            agent_id="multi",
            runtime_config=RUNTIMES[RUNTIME],
            output_dir=output_dir,
            timeout=TIMEOUT,
            session_id="live-test-multi",
            permissions="read-only",
        )

        result = runner.run(
            prompts_dir=prompts,
            working_dir=target_repo,
        )

        assert result.success is True
        assert result.prompt_count == 2

        out1 = (output_dir / "multi" / "1-output.md").read_text()
        out2 = (output_dir / "multi" / "2-output.md").read_text()
        assert "greet" in out1.lower()
        assert "greet" in out2.lower()

    def test_resume_with_feedback(self, tmp_path: Path, target_repo: Path) -> None:
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "1.md").write_text(
            "Read hello.py. Reply with ONLY the return type of the greet function."
        )

        output_dir = tmp_path / "output"
        runner = AgentRunner(
            agent_id="feedback",
            runtime_config=RUNTIMES[RUNTIME],
            output_dir=output_dir,
            timeout=TIMEOUT,
            session_id="live-test-feedback",
            permissions="read-only",
        )

        result = runner.run(
            prompts_dir=prompts,
            working_dir=target_repo,
        )

        assert result.success is True

        feedback_response = runner.resume_with_feedback(
            "What was the parameter name of the greet function? Reply with ONLY the name.",
            working_dir=target_repo,
        )

        assert "name" in feedback_response.lower()


class TestLivePipeline:
    """Test full pipeline execution against the real CLI."""

    def test_two_agent_pipeline(self, tmp_path: Path, target_repo: Path) -> None:
        pipe_dir = tmp_path / "pipeline"
        pipe_dir.mkdir()

        pa = pipe_dir / "prompts" / "scanner"
        pa.mkdir(parents=True)
        (pa / "1.md").write_text(
            "List all files in this project. Reply with ONLY filenames, one per line."
        )

        pb = pipe_dir / "prompts" / "analyser"
        pb.mkdir(parents=True)
        (pb / "1.md").write_text(
            "Based on the context from the previous agent, "
            "which files are Python files? Reply with ONLY those filenames."
        )

        yaml_content = """\
name: live-test
description: Two-agent live test

agents:
  - id: scanner
    prompts: prompts/scanner/
    permissions: read-only

  - id: analyser
    prompts: prompts/analyser/
    permissions: read-only
"""
        (pipe_dir / "pipeline.yaml").write_text(yaml_content)

        config = PipelineConfig.load(pipe_dir / "pipeline.yaml")
        pipeline = Pipeline(config, pipe_dir)

        output_dir = tmp_path / "output"
        result = pipeline.run(
            runtime=RUNTIME,
            working_dir=str(target_repo),
            output_dir=str(output_dir),
            timeout=TIMEOUT,
        )

        assert result.agents_completed == 2
        assert result.gate_rejected_at is None
        assert (result.output_dir / "scanner" / "output.md").exists()
        assert (result.output_dir / "analyser" / "output.md").exists()
        assert (result.output_dir / "pipeline.json").exists()

        analyser_output = (result.output_dir / "analyser" / "output.md").read_text()
        assert "hello" in analyser_output.lower()

    def test_pipeline_with_input_file(self, tmp_path: Path, target_repo: Path) -> None:
        task_file = target_repo / "TASK.md"
        task_file.write_text("Focus only on Python files in this project.")

        pipe_dir = tmp_path / "pipeline"
        pipe_dir.mkdir()

        pa = pipe_dir / "prompts" / "agent"
        pa.mkdir(parents=True)
        (pa / "1.md").write_text(
            "Based on the task in the context, list the relevant files. Reply with ONLY filenames."
        )

        yaml_content = """\
name: input-test
agents:
  - id: agent
    prompts: prompts/agent/
    permissions: read-only
"""
        (pipe_dir / "pipeline.yaml").write_text(yaml_content)

        config = PipelineConfig.load(pipe_dir / "pipeline.yaml")
        pipeline = Pipeline(config, pipe_dir)

        result = pipeline.run(
            runtime=RUNTIME,
            working_dir=str(target_repo),
            input_files=[str(task_file)],
            output_dir=str(tmp_path / "output"),
            timeout=TIMEOUT,
        )

        assert result.agents_completed == 1
        output = (result.output_dir / "agent" / "output.md").read_text()
        assert "hello" in output.lower()

    def test_file_gate_pns(self, tmp_path: Path, target_repo: Path) -> None:
        pipe_dir = tmp_path / "pipeline"
        pipe_dir.mkdir()

        pa = pipe_dir / "prompts" / "gated"
        pa.mkdir(parents=True)
        (pa / "1.md").write_text("List all files. Reply with ONLY filenames.")

        pb = pipe_dir / "prompts" / "after-gate"
        pb.mkdir(parents=True)
        (pb / "1.md").write_text("Say 'gate passed' and nothing else.")

        yaml_content = """\
name: gate-test
agents:
  - id: gated
    prompts: prompts/gated/
    gate: human
    permissions: read-only
  - id: after-gate
    prompts: prompts/after-gate/
    permissions: read-only
"""
        (pipe_dir / "pipeline.yaml").write_text(yaml_content)

        gate_dir = tmp_path / "gates"
        gate_dir.mkdir()

        def auto_approve():
            """Write PNS to gate file once it appears."""
            import time

            for _ in range(60):
                time.sleep(1)
                gate_file = gate_dir / "gated.gate"
                feedback_file = gate_dir / "gated.feedback"
                if gate_file.exists() and not feedback_file.exists():
                    feedback_file.write_text("PNS")
                    return

        t = threading.Thread(target=auto_approve, daemon=True)
        t.start()

        config = PipelineConfig.load(pipe_dir / "pipeline.yaml")
        pipeline = Pipeline(config, pipe_dir)

        result = pipeline.run(
            runtime=RUNTIME,
            working_dir=str(target_repo),
            output_dir=str(tmp_path / "output"),
            timeout=TIMEOUT,
            gate_mode="file",
            gate_dir=gate_dir,
        )

        t.join(timeout=15)

        assert result.agents_completed == 2
        assert result.gate_rejected_at is None


class TestLiveSetup:
    """Test setup.sh execution in live pipelines."""

    def test_setup_runs_before_agents(self, tmp_path: Path, target_repo: Path) -> None:
        pipe_dir = tmp_path / "pipeline"
        pipe_dir.mkdir()

        marker_file = tmp_path / "setup-ran.txt"

        setup_sh = pipe_dir / "setup.sh"
        setup_sh.write_text(
            f"#!/usr/bin/env bash\nset -euo pipefail\necho 'setup complete' > {marker_file}\n"
        )
        setup_sh.chmod(0o755)

        pa = pipe_dir / "prompts" / "agent"
        pa.mkdir(parents=True)
        (pa / "1.md").write_text("Say 'hello'. Nothing else.")

        yaml_content = """\
name: setup-test
setup: setup.sh
agents:
  - id: agent
    prompts: prompts/agent/
    permissions: read-only
"""
        (pipe_dir / "pipeline.yaml").write_text(yaml_content)

        config = PipelineConfig.load(pipe_dir / "pipeline.yaml")
        pipeline = Pipeline(config, pipe_dir)

        result = pipeline.run(
            runtime=RUNTIME,
            working_dir=str(target_repo),
            output_dir=str(tmp_path / "output"),
            timeout=TIMEOUT,
        )

        assert result.agents_completed == 1
        assert marker_file.exists()
        assert "setup complete" in marker_file.read_text()
