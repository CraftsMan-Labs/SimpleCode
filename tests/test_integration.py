"""Integration tests — full pipeline flow with mocked subprocess calls."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from simplecode.pipeline import Pipeline, PipelineConfig
from simplecode.runner import AgentRunner
from simplecode.runtime import RUNTIMES


def _mock_subprocess_run(returncode: int = 0, stdout: str = "mock agent output"):
    """Create a mock for subprocess.run that returns predictable output."""
    mock_result = MagicMock()
    mock_result.returncode = returncode
    mock_result.stdout = stdout
    mock_result.stderr = ""
    return MagicMock(return_value=mock_result)


class TestAgentRunnerIntegration:
    """Test AgentRunner with mocked CLI calls."""

    def test_runs_all_prompts_in_sequence(self, tmp_path: Path) -> None:
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "1.md").write_text("first prompt")
        (prompts / "2.md").write_text("second prompt")
        (prompts / "3.md").write_text("third prompt")

        output_dir = tmp_path / "output"

        runner = AgentRunner(
            agent_id="test-agent",
            runtime_config=RUNTIMES["claude"],
            output_dir=output_dir,
            timeout=60,
            session_id="test-session",
            permissions="read-only",
        )

        call_count = 0

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.returncode = 0
            result.stdout = f"output from prompt {call_count}"
            result.stderr = ""
            return result

        with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
            result = runner.run(
                prompts_dir=prompts,
                working_dir=tmp_path,
            )

        assert result.prompt_count == 3
        assert result.success is True
        assert call_count == 3

        assert (output_dir / "test-agent" / "1-output.md").exists()
        assert (output_dir / "test-agent" / "2-output.md").exists()
        assert (output_dir / "test-agent" / "3-output.md").exists()
        assert (output_dir / "test-agent" / "output.md").exists()

        final = (output_dir / "test-agent" / "output.md").read_text()
        assert "output from prompt 3" in final

    def test_first_prompt_includes_context(self, tmp_path: Path) -> None:
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "1.md").write_text("analyse this")

        context_file = tmp_path / "prior-output.md"
        context_file.write_text("previous agent said hello")

        output_dir = tmp_path / "output"

        runner = AgentRunner(
            agent_id="ctx-agent",
            runtime_config=RUNTIMES["claude"],
            output_dir=output_dir,
            timeout=60,
            session_id="s1",
            permissions="read-only",
        )

        captured_prompts = []

        def mock_run(*args, **kwargs):
            if kwargs.get("input"):
                captured_prompts.append(kwargs["input"])
            result = MagicMock()
            result.returncode = 0
            result.stdout = "ok"
            result.stderr = ""
            return result

        with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
            runner.run(
                prompts_dir=prompts,
                context_files=[str(context_file)],
                working_dir=tmp_path,
            )

        assert len(captured_prompts) == 1
        assert "previous agent said hello" in captured_prompts[0]
        assert "Context from previous agents" in captured_prompts[0]

    def test_resume_with_feedback(self, tmp_path: Path) -> None:
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "1.md").write_text("start")

        output_dir = tmp_path / "output"

        runner = AgentRunner(
            agent_id="fb-agent",
            runtime_config=RUNTIMES["claude"],
            output_dir=output_dir,
            timeout=60,
            session_id="s1",
            permissions="write",
        )

        call_index = 0

        def mock_run(*args, **kwargs):
            nonlocal call_index
            call_index += 1
            result = MagicMock()
            result.returncode = 0
            result.stdout = f"response {call_index}"
            result.stderr = ""
            return result

        with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
            runner.run(prompts_dir=prompts, working_dir=tmp_path)
            feedback_response = runner.resume_with_feedback("please add tests")

        assert call_index == 2
        assert feedback_response == "response 2"

    def test_model_override(self, tmp_path: Path) -> None:
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "1.md").write_text("go")

        output_dir = tmp_path / "output"

        runner = AgentRunner(
            agent_id="model-agent",
            runtime_config=RUNTIMES["claude"],
            output_dir=output_dir,
            timeout=60,
            session_id="s1",
            permissions="write",
            model="opus",
        )

        captured_cmd = []

        def mock_run(cmd, *args, **kwargs):
            captured_cmd.extend(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "done"
            result.stderr = ""
            return result

        with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
            runner.run(prompts_dir=prompts, working_dir=tmp_path)

        assert "--model" in captured_cmd
        assert "opus" in captured_cmd


class TestPipelineIntegration:
    """Test full pipeline flow with mocked CLI."""

    def test_two_agent_pipeline(self, pipeline_dir: Path, tmp_path: Path) -> None:
        config = PipelineConfig.load(pipeline_dir / "pipeline.yaml")
        pipeline = Pipeline(config, pipeline_dir)

        output_dir = tmp_path / "output"

        call_count = 0

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.returncode = 0
            result.stdout = f"agent output {call_count}"
            result.stderr = ""
            return result

        with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
            result = pipeline.run(
                runtime="claude",
                working_dir=str(tmp_path),
                output_dir=str(output_dir),
                timeout=60,
            )

        assert result.agents_completed == 2
        assert result.total_agents == 2
        assert result.gate_rejected_at is None

        assert (result.output_dir / "agent-a" / "output.md").exists()
        assert (result.output_dir / "agent-b" / "output.md").exists()
        assert (result.output_dir / "pipeline.json").exists()

        metadata = json.loads((result.output_dir / "pipeline.json").read_text())
        assert metadata["status"] == "complete"
        assert metadata["agents_completed"] == 2

    def test_context_flows_between_agents(self, pipeline_dir: Path, tmp_path: Path) -> None:
        config = PipelineConfig.load(pipeline_dir / "pipeline.yaml")
        pipeline = Pipeline(config, pipeline_dir)

        output_dir = tmp_path / "output"

        prompts_seen = []

        def mock_run(*args, **kwargs):
            if kwargs.get("input"):
                prompts_seen.append(kwargs["input"])
            result = MagicMock()
            result.returncode = 0
            result.stdout = "unique-marker-from-agent-a"
            result.stderr = ""
            return result

        with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
            pipeline.run(
                runtime="claude",
                working_dir=str(tmp_path),
                output_dir=str(output_dir),
                timeout=60,
            )

        agent_b_first_prompt = prompts_seen[2]
        assert "unique-marker-from-agent-a" in agent_b_first_prompt

    def test_input_files_injected(self, pipeline_dir: Path, tmp_path: Path) -> None:
        task_file = tmp_path / "TASK.md"
        task_file.write_text("Build a login page")

        config = PipelineConfig.load(pipeline_dir / "pipeline.yaml")
        pipeline = Pipeline(config, pipeline_dir)
        output_dir = tmp_path / "output"

        prompts_seen = []

        def mock_run(*args, **kwargs):
            if kwargs.get("input"):
                prompts_seen.append(kwargs["input"])
            result = MagicMock()
            result.returncode = 0
            result.stdout = "done"
            result.stderr = ""
            return result

        with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
            pipeline.run(
                runtime="claude",
                working_dir=str(tmp_path),
                input_files=[str(task_file)],
                output_dir=str(output_dir),
                timeout=60,
            )

        assert "Build a login page" in prompts_seen[0]

    def test_setup_script_runs(self, setup_pipeline_dir: Path, tmp_path: Path) -> None:
        config = PipelineConfig.load(setup_pipeline_dir / "pipeline.yaml")
        pipeline = Pipeline(config, setup_pipeline_dir)
        output_dir = tmp_path / "output"

        setup_called = []

        def mock_run(cmd, *args, **kwargs):
            if "setup.sh" in str(cmd):
                setup_called.append(cmd)
                result = MagicMock()
                result.returncode = 0
                result.stdout = "setup done"
                result.stderr = ""
                return result
            result = MagicMock()
            result.returncode = 0
            result.stdout = "agent done"
            result.stderr = ""
            return result

        with (
            patch("simplecode.runtime.subprocess.run", side_effect=mock_run),
            patch("simplecode.pipeline.subprocess.run", side_effect=mock_run),
        ):
            pipeline.run(
                runtime="claude",
                working_dir=str(tmp_path),
                output_dir=str(output_dir),
                timeout=60,
            )

        assert len(setup_called) == 1

    def test_setup_failure_aborts_pipeline(self, setup_pipeline_dir: Path, tmp_path: Path) -> None:
        config = PipelineConfig.load(setup_pipeline_dir / "pipeline.yaml")
        pipeline = Pipeline(config, setup_pipeline_dir)

        def mock_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "permission denied"
            return result

        from simplecode.errors import SetupError

        with patch("simplecode.pipeline.subprocess.run", side_effect=mock_run):
            with pytest.raises(SetupError, match="permission denied"):
                pipeline.run(
                    runtime="claude",
                    working_dir=str(tmp_path),
                    output_dir=str(tmp_path / "output"),
                )

    def test_per_agent_model_override(self, tmp_path: Path) -> None:
        prompts_a = tmp_path / "prompts" / "a"
        prompts_a.mkdir(parents=True)
        (prompts_a / "1.md").write_text("go")

        prompts_b = tmp_path / "prompts" / "b"
        prompts_b.mkdir(parents=True)
        (prompts_b / "1.md").write_text("go")

        yaml_content = """\
name: model-test
agents:
  - id: a
    prompts: prompts/a/
    model: opus
  - id: b
    prompts: prompts/b/
    model: haiku
"""
        (tmp_path / "pipeline.yaml").write_text(yaml_content)

        config = PipelineConfig.load(tmp_path / "pipeline.yaml")
        pipeline = Pipeline(config, tmp_path)

        commands_seen = []

        def mock_run(cmd, *args, **kwargs):
            commands_seen.append(list(cmd) if isinstance(cmd, (list, tuple)) else cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "done"
            result.stderr = ""
            return result

        with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
            pipeline.run(
                runtime="claude",
                working_dir=str(tmp_path),
                output_dir=str(tmp_path / "output"),
            )

        cmd_a = commands_seen[0]
        cmd_b = commands_seen[1]
        assert "opus" in cmd_a
        assert "haiku" in cmd_b

    def test_run_inherits_cli_timeout_and_permissions(self, tmp_path: Path) -> None:
        prompts = tmp_path / "prompts" / "a"
        prompts.mkdir(parents=True)
        (prompts / "1.md").write_text("go")
        (tmp_path / "pipeline.yaml").write_text(
            "name: inherit-test\nagents:\n  - id: a\n    prompts: prompts/a/\n"
        )

        config = PipelineConfig.load(tmp_path / "pipeline.yaml")
        pipeline = Pipeline(config, tmp_path)
        commands_seen: list[list[str]] = []

        def mock_run(cmd, *args, **kwargs):
            commands_seen.append(list(cmd))
            result = MagicMock()
            result.returncode = 0
            result.stdout = "done"
            result.stderr = ""
            return result

        with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
            pipeline.run(
                runtime="claude",
                working_dir=str(tmp_path),
                output_dir=str(tmp_path / "output"),
                timeout=86400,
                permissions="full",
            )

        assert "--permission-mode" in commands_seen[0]
        assert "bypassPermissions" in commands_seen[0]

    def test_yaml_timeout_overrides_run_default(self, tmp_path: Path) -> None:
        prompts = tmp_path / "prompts" / "a"
        prompts.mkdir(parents=True)
        (prompts / "1.md").write_text("go")
        (tmp_path / "pipeline.yaml").write_text(
            "name: override-test\nagents:\n  - id: a\n    prompts: prompts/a/\n"
            "    timeout: 120\n    permissions: read-only\n"
        )

        config = PipelineConfig.load(tmp_path / "pipeline.yaml")
        pipeline = Pipeline(config, tmp_path)
        commands_seen: list[list[str]] = []

        def mock_run(cmd, *args, **kwargs):
            commands_seen.append(list(cmd))
            result = MagicMock()
            result.returncode = 0
            result.stdout = "done"
            result.stderr = ""
            return result

        with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
            pipeline.run(
                runtime="claude",
                working_dir=str(tmp_path),
                output_dir=str(tmp_path / "output"),
                timeout=86400,
                permissions="full",
            )

        assert "plan" in commands_seen[0]
        assert "bypassPermissions" not in commands_seen[0]


class TestGateIntegration:
    """Test gate behavior within pipelines."""

    def test_file_gate_with_feedback_then_pns(
        self, gated_pipeline_dir: Path, tmp_path: Path
    ) -> None:
        config = PipelineConfig.load(gated_pipeline_dir / "pipeline.yaml")
        pipeline = Pipeline(config, gated_pipeline_dir)

        gate_dir = tmp_path / "gates"
        gate_dir.mkdir()
        output_dir = tmp_path / "output"

        (gate_dir / "planner.feedback").write_text("Add more detail to the plan")

        call_count = 0

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.returncode = 0
            result.stdout = f"response {call_count}"
            result.stderr = ""
            return result

        import threading

        def write_pns_after_delay():
            """After the first feedback is consumed, write PNS."""
            import time

            for _ in range(20):
                time.sleep(0.5)
                if (
                    not (gate_dir / "planner.feedback").exists()
                    and (gate_dir / "planner.gate").exists()
                ):
                    (gate_dir / "planner.feedback").write_text("PNS")
                    return

        t = threading.Thread(target=write_pns_after_delay, daemon=True)
        t.start()

        with (
            patch("simplecode.runtime.subprocess.run", side_effect=mock_run),
            patch("simplecode.gate.FILE_POLL_INTERVAL_SECONDS", 0.2),
        ):
            result = pipeline.run(
                runtime="claude",
                working_dir=str(tmp_path),
                output_dir=str(output_dir),
                gate_mode="file",
                gate_dir=gate_dir,
            )

        t.join(timeout=15)

        assert result.agents_completed == 2
        assert result.gate_rejected_at is None

        gate_artifacts = list((result.output_dir / "planner").glob("gate-*"))
        assert len(gate_artifacts) >= 2

    def test_file_gate_rejection(self, gated_pipeline_dir: Path, tmp_path: Path) -> None:
        config = PipelineConfig.load(gated_pipeline_dir / "pipeline.yaml")
        pipeline = Pipeline(config, gated_pipeline_dir)

        gate_dir = tmp_path / "gates"
        gate_dir.mkdir()

        (gate_dir / "planner.rejected").write_text("")

        def mock_run(*args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = "agent output"
            result.stderr = ""
            return result

        with (
            patch("simplecode.runtime.subprocess.run", side_effect=mock_run),
            patch("simplecode.gate.FILE_POLL_INTERVAL_SECONDS", 0.1),
        ):
            result = pipeline.run(
                runtime="claude",
                working_dir=str(tmp_path),
                output_dir=str(tmp_path / "output"),
                gate_mode="file",
                gate_dir=gate_dir,
            )

        assert result.gate_rejected_at == "planner"
        assert result.agents_completed == 0


class TestCLIIntegration:
    """Test CLI commands end-to-end."""

    def test_init_creates_valid_pipeline(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from simplecode.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["init", str(tmp_path / "test-pipe"), "--agents", "4"])

        assert result.exit_code == 0
        assert (tmp_path / "test-pipe" / "pipeline.yaml").exists()
        assert (tmp_path / "test-pipe" / "setup.sh").exists()
        assert (tmp_path / "test-pipe" / "prompts" / "planner" / "1.md").exists()
        assert (tmp_path / "test-pipe" / "prompts" / "executor" / "1.md").exists()
        assert (tmp_path / "test-pipe" / "prompts" / "reviewer" / "1.md").exists()
        assert (tmp_path / "test-pipe" / "prompts" / "review-tasks" / "1.md").exists()

        config = PipelineConfig.load(tmp_path / "test-pipe" / "pipeline.yaml")
        assert len(config.agents) == 4

    def test_list_shows_agents(self, pipeline_dir: Path) -> None:
        from click.testing import CliRunner

        from simplecode.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["list", "-f", str(pipeline_dir / "pipeline.yaml")])

        assert result.exit_code == 0
        assert "agent-a" in result.output
        assert "agent-b" in result.output
        assert "2 prompts" in result.output
        assert "1 prompts" in result.output

    def test_run_with_mock(self, pipeline_dir: Path, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from simplecode.cli import main

        def mock_run(*args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = "ok"
            result.stderr = ""
            return result

        runner = CliRunner()
        with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
            result = runner.invoke(
                main,
                [
                    "run",
                    "-f",
                    str(pipeline_dir / "pipeline.yaml"),
                    "-r",
                    "claude",
                    "-w",
                    str(tmp_path),
                    "-o",
                    str(tmp_path / "output"),
                    "-t",
                    "60",
                ],
            )

        assert result.exit_code == 0
        assert "Pipeline complete" in result.output
        assert "2/2" in result.output

    def test_run_missing_yaml_is_clean_error(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from simplecode.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["run", "-f", str(tmp_path / "missing.yaml")])
        assert result.exit_code == 1
        assert "Error:" in result.output
        assert "Traceback" not in result.output

    def test_list_missing_prompts_is_clean_error(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from simplecode.cli import main

        yaml_file = tmp_path / "pipeline.yaml"
        yaml_file.write_text("name: broken\nagents:\n  - id: a\n    prompts: prompts/missing/\n")
        runner = CliRunner()
        result = runner.invoke(main, ["list", "-f", str(yaml_file)])
        assert result.exit_code == 1
        assert "Error:" in result.output
        assert "prompts directory not found" in result.output.lower()

    def test_run_resolves_input_from_cwd(
        self, pipeline_dir: Path, tmp_path: Path, monkeypatch
    ) -> None:
        from click.testing import CliRunner

        from simplecode.cli import main

        issue = tmp_path / "ISSUE.md"
        issue.write_text("do the thing")
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(tmp_path)

        captured: list[str] = []

        def mock_run(*args, **kwargs):
            if kwargs.get("input"):
                captured.append(kwargs["input"])
            result = MagicMock()
            result.returncode = 0
            result.stdout = "ok"
            result.stderr = ""
            return result

        runner = CliRunner()
        with patch("simplecode.runtime.subprocess.run", side_effect=mock_run):
            result = runner.invoke(
                main,
                [
                    "run",
                    "-f",
                    str(pipeline_dir / "pipeline.yaml"),
                    "-r",
                    "claude",
                    "-w",
                    str(work),
                    "-o",
                    str(tmp_path / "output"),
                    "-i",
                    "ISSUE.md",
                    "-t",
                    "60",
                ],
            )

        assert result.exit_code == 0, result.output
        assert captured
        assert "do the thing" in captured[0]
