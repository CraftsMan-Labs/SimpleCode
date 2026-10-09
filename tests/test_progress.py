"""Tests for progress reporting and heartbeat."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from simplecode.progress import (
    CLIProgressCallback,
    Heartbeat,
    ProgressCallback,
    compose_progress,
)


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


def test_composed_callbacks_preserve_registration_order() -> None:
    calls: list[str] = []

    class Sink:
        def __init__(self, name: str) -> None:
            self.name = name

        def on_session(self, session_id: str) -> None:
            calls.append(f"{self.name}:session:{session_id}")

        def on_agent_done(self, index: int, agent_id: str, total_agents: int) -> None:
            calls.append(f"{self.name}:agent:{agent_id}")

    progress = compose_progress(Sink("first"), None, Sink("second"))
    assert progress is not None
    progress.on_session("session-1")
    progress.on_agent_done(1, "builder", 1)

    assert calls == [
        "first:session:session-1",
        "second:session:session-1",
        "first:agent:builder",
        "second:agent:builder",
    ]


def test_composed_callbacks_call_all_sinks_and_raise_first_error() -> None:
    calls: list[str] = []
    first_error = RuntimeError("first failed")
    second_error = ValueError("second failed")

    class Sink:
        def __init__(self, name: str, error: Exception | None = None) -> None:
            self.name = name
            self.error = error

        def on_pipeline_done(self, name: str, total_agents: int, status: str) -> None:
            calls.append(self.name)
            if self.error is not None:
                raise self.error

    progress = compose_progress(
        Sink("first", first_error),
        Sink("second"),
        Sink("third", second_error),
        Sink("fourth"),
    )
    assert progress is not None

    with pytest.raises(RuntimeError) as caught:
        progress.on_pipeline_done("delivery", 2, "failed")

    assert caught.value is first_error
    assert calls == ["first", "second", "third", "fourth"]


def test_composed_callbacks_allow_partial_legacy_sinks() -> None:
    calls: list[str] = []

    class LegacySink:
        def on_pipeline_start(self, name: str, total_agents: int) -> None:
            calls.append(name)

    progress = compose_progress(LegacySink(), CLIProgressCallback())
    assert progress is not None
    progress.on_session("session-1")
    progress.on_pipeline_start("delivery", 2)
    progress.on_pipeline_done("delivery", 2, "complete")
    assert calls == ["delivery"]


class TestProgressIntegration:
    def test_pipeline_calls_progress(self, tmp_path: Path) -> None:
        prompts_dir = tmp_path / "prompts" / "agent-a"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "1.md").write_text("do something")

        pipeline_yaml = tmp_path / "pipeline.yaml"
        pipeline_yaml.write_text(
            "name: test\nagents:\n  - id: agent-a\n    prompts: prompts/agent-a/\n"
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
        progress.on_session.assert_called_once()
        progress.on_pipeline_done.assert_called_once_with("test", 1, "complete")
        progress.on_agent_start.assert_called_once_with(1, "agent-a", 1, 1)
        progress.on_prompt_start.assert_called_once_with("agent-a", 1, 1)
        progress.on_prompt_done.assert_called_once_with("agent-a", 1, 1)
        progress.on_agent_done.assert_called_once_with(1, "agent-a", 1)
        hook_names = {
            "on_session",
            "on_pipeline_start",
            "on_agent_start",
            "on_prompt_start",
            "on_prompt_done",
            "on_agent_done",
            "on_pipeline_done",
        }
        lifecycle = [item for item in progress.mock_calls if item[0] in hook_names]
        assert [item[0] for item in lifecycle] == [
            "on_session",
            "on_pipeline_start",
            "on_agent_start",
            "on_prompt_start",
            "on_prompt_done",
            "on_agent_done",
            "on_pipeline_done",
        ]

    def test_pipeline_works_without_progress(self, tmp_path: Path) -> None:
        prompts_dir = tmp_path / "prompts" / "agent-a"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "1.md").write_text("do something")

        pipeline_yaml = tmp_path / "pipeline.yaml"
        pipeline_yaml.write_text(
            "name: test\nagents:\n  - id: agent-a\n    prompts: prompts/agent-a/\n"
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

    def test_pipeline_emits_failed_finish_boundary(self, tmp_path: Path) -> None:
        prompts_dir = tmp_path / "prompts" / "agent-a"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "1.md").write_text("do something")
        pipeline_yaml = tmp_path / "pipeline.yaml"
        pipeline_yaml.write_text(
            "name: test\nagents:\n  - id: agent-a\n    prompts: prompts/agent-a/\n"
        )

        from simplecode.pipeline import Pipeline, PipelineConfig

        pipeline = Pipeline(PipelineConfig.load(pipeline_yaml), tmp_path)
        progress = MagicMock()
        with (
            patch("simplecode.runner.exec_cli", side_effect=RuntimeError("failed")),
            pytest.raises(RuntimeError, match="failed"),
        ):
            pipeline.run(
                working_dir=tmp_path,
                output_dir=tmp_path / "output",
                progress=progress,
            )

        progress.on_pipeline_done.assert_called_once_with("test", 1, "failed")

    def test_start_callback_failure_aborts_and_emits_failed_boundary(
        self,
        tmp_path: Path,
    ) -> None:
        pipeline_yaml = tmp_path / "pipeline.yaml"
        pipeline_yaml.write_text(
            "name: test\nagents:\n  - id: agent-a\n    prompts: prompts/agent-a/\n"
        )

        from simplecode.pipeline import Pipeline, PipelineConfig

        pipeline = Pipeline(PipelineConfig.load(pipeline_yaml), tmp_path)
        start_error = RuntimeError("start callback failed")

        class FailingStart:
            def __init__(self) -> None:
                self.done_statuses: list[str] = []

            def on_pipeline_start(self, name: str, total_agents: int) -> None:
                raise start_error

            def on_pipeline_done(self, name: str, total_agents: int, status: str) -> None:
                self.done_statuses.append(status)

        progress = FailingStart()
        with (
            patch.object(pipeline, "_run") as run_pipeline,
            pytest.raises(RuntimeError) as caught,
        ):
            pipeline.run(progress=progress)

        assert caught.value is start_error
        run_pipeline.assert_not_called()
        assert progress.done_statuses == ["failed"]

    def test_session_callback_failure_is_primary_and_emits_failed_boundary(
        self,
        tmp_path: Path,
    ) -> None:
        pipeline_yaml = tmp_path / "pipeline.yaml"
        pipeline_yaml.write_text(
            "name: test\nagents:\n  - id: agent-a\n    prompts: prompts/agent-a/\n"
        )

        from simplecode.pipeline import Pipeline, PipelineConfig

        pipeline = Pipeline(PipelineConfig.load(pipeline_yaml), tmp_path)
        session_error = RuntimeError("session callback failed")
        calls: list[str] = []

        class FailingSessionAndDone:
            def on_session(self, session_id: str) -> None:
                calls.append("session")
                raise session_error

            def on_pipeline_start(self, name: str, total_agents: int) -> None:
                calls.append("start")

            def on_pipeline_done(self, name: str, total_agents: int, status: str) -> None:
                calls.append(f"done:{status}")
                raise ValueError("failed boundary callback failed")

        with (
            patch.object(pipeline, "_run") as run_pipeline,
            pytest.raises(RuntimeError) as caught,
        ):
            pipeline.run(progress=FailingSessionAndDone())

        assert caught.value is session_error
        assert calls == ["session", "done:failed"]
        run_pipeline.assert_not_called()
        assert session_error.__notes__ == [
            "Suppressed on_pipeline_done callback error: ValueError"
        ]

    def test_success_callback_failure_is_fail_closed(self, tmp_path: Path) -> None:
        prompts_dir = tmp_path / "prompts" / "agent-a"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "1.md").write_text("do something")
        pipeline_yaml = tmp_path / "pipeline.yaml"
        pipeline_yaml.write_text(
            "name: test\nagents:\n  - id: agent-a\n    prompts: prompts/agent-a/\n"
        )

        from simplecode.pipeline import Pipeline, PipelineConfig

        pipeline = Pipeline(PipelineConfig.load(pipeline_yaml), tmp_path)
        done_error = RuntimeError("durable completion failed")

        class FailingDone:
            def on_pipeline_done(self, name: str, total_agents: int, status: str) -> None:
                assert status == "complete"
                raise done_error

        with (
            patch("simplecode.runner.exec_cli", return_value="done"),
            pytest.raises(RuntimeError) as caught,
        ):
            pipeline.run(
                working_dir=tmp_path,
                output_dir=tmp_path / "output",
                progress=FailingDone(),
            )

        assert caught.value is done_error

    def test_failed_callback_cannot_mask_primary_pipeline_error(self, tmp_path: Path) -> None:
        prompts_dir = tmp_path / "prompts" / "agent-a"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "1.md").write_text("do something")
        pipeline_yaml = tmp_path / "pipeline.yaml"
        pipeline_yaml.write_text(
            "name: test\nagents:\n  - id: agent-a\n    prompts: prompts/agent-a/\n"
        )

        from simplecode.pipeline import Pipeline, PipelineConfig

        pipeline = Pipeline(PipelineConfig.load(pipeline_yaml), tmp_path)
        primary_error = RuntimeError("pipeline failed")

        class FailingDone:
            def on_pipeline_done(self, name: str, total_agents: int, status: str) -> None:
                assert status == "failed"
                raise ValueError("completion callback failed")

        with (
            patch("simplecode.runner.exec_cli", side_effect=primary_error),
            pytest.raises(RuntimeError) as caught,
        ):
            pipeline.run(
                working_dir=tmp_path,
                output_dir=tmp_path / "output",
                progress=FailingDone(),
            )

        assert caught.value is primary_error
        assert primary_error.__notes__ == [
            "Suppressed on_pipeline_done callback error: ValueError"
        ]
