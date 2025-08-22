"""Tests for progress reporting and heartbeat."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from simplecode.progress import CLIProgressCallback, Heartbeat, ProgressCallback


class TestHeartbeat:
    def test_fires_after_interval(self) -> None:
        messages: list[str] = []
        hb = Heartbeat(
            message_fn=lambda: "working",
            interval=0.1,
            print_fn=messages.append,
        )
        with hb:
            time.sleep(0.35)

        assert len(messages) >= 2
        assert "working" in messages[0]
        assert "elapsed" in messages[0]

    def test_stops_on_exit(self) -> None:
        messages: list[str] = []
        hb = Heartbeat(
            message_fn=lambda: "working",
            interval=0.1,
            print_fn=messages.append,
        )
        with hb:
            time.sleep(0.15)

        count_at_exit = len(messages)
        time.sleep(0.25)
        assert len(messages) == count_at_exit

    def test_no_fire_before_interval(self) -> None:
        messages: list[str] = []
        hb = Heartbeat(
            message_fn=lambda: "working",
            interval=10.0,
            print_fn=messages.append,
        )
        with hb:
            time.sleep(0.05)

        assert len(messages) == 0


class TestCLIProgressCallback:
    def test_implements_protocol(self) -> None:
        cb = CLIProgressCallback()
        assert isinstance(cb, ProgressCallback)

    @patch("simplecode.progress.click")
    def test_pipeline_start(self, mock_click: MagicMock) -> None:
        cb = CLIProgressCallback()
        cb.on_pipeline_start("my-pipe", 3)
        mock_click.echo.assert_called_once()
        msg = mock_click.echo.call_args[0][0]
        assert "my-pipe" in msg
        assert "3" in msg

    @patch("simplecode.progress.click")
    def test_agent_lifecycle(self, mock_click: MagicMock) -> None:
        cb = CLIProgressCallback()
        cb.on_agent_start(1, "planner", 3, 2)
        cb.on_prompt_start("planner", 1, 2)
        cb.on_prompt_done("planner", 1, 2)
        cb.on_agent_done(1, "planner", 3)

        messages = [c[0][0] for c in mock_click.echo.call_args_list]
        assert any("planner" in m and "1/3" in m for m in messages)
        assert any("Prompt 1/2..." in m for m in messages)
        assert any("Prompt 1/2 done" in m for m in messages)
        assert any("complete" in m for m in messages)


class TestProgressIntegration:
    def test_pipeline_calls_progress(self, tmp_path: Path) -> None:
        prompts_dir = tmp_path / "prompts" / "agent-a"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "1.md").write_text("do something")

        pipeline_yaml = tmp_path / "pipeline.yaml"
        pipeline_yaml.write_text(
            "name: test\n"
            "agents:\n"
            "  - id: agent-a\n"
            "    prompts: prompts/agent-a/\n"
        )

        from simplecode.pipeline import Pipeline, PipelineConfig

        config = PipelineConfig.load(pipeline_yaml)
        pipeline = Pipeline(config, tmp_path)

        progress = MagicMock()

        with patch("simplecode.runner.exec_cli", return_value="done"):
            pipeline.run(
                runtime="claude",
                working_dir=str(tmp_path),
                output_dir=str(tmp_path / "output"),
                progress=progress,
            )

        progress.on_pipeline_start.assert_called_once_with("test", 1)
        progress.on_agent_start.assert_called_once_with(1, "agent-a", 1, 1)
        progress.on_prompt_start.assert_called_once_with("agent-a", 1, 1)
        progress.on_prompt_done.assert_called_once_with("agent-a", 1, 1)
        progress.on_agent_done.assert_called_once_with(1, "agent-a", 1)

    def test_pipeline_works_without_progress(self, tmp_path: Path) -> None:
        prompts_dir = tmp_path / "prompts" / "agent-a"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "1.md").write_text("do something")

        pipeline_yaml = tmp_path / "pipeline.yaml"
        pipeline_yaml.write_text(
            "name: test\n"
            "agents:\n"
            "  - id: agent-a\n"
            "    prompts: prompts/agent-a/\n"
        )

        from simplecode.pipeline import Pipeline, PipelineConfig

        config = PipelineConfig.load(pipeline_yaml)
        pipeline = Pipeline(config, tmp_path)

        with patch("simplecode.runner.exec_cli", return_value="done"):
            result = pipeline.run(
                runtime="claude",
                working_dir=str(tmp_path),
                output_dir=str(tmp_path / "output"),
            )

        assert result.agents_completed == 1
