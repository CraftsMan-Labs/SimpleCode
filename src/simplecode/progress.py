"""Progress reporting for pipeline execution."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

import click


@runtime_checkable
class ProgressCallback(Protocol):
    """Protocol for receiving pipeline progress updates.

    Implement this to customise how progress is reported.
    The CLI ships a default implementation that prints to stdout.
    """

    def on_pipeline_start(self, name: str, total_agents: int) -> None: ...

    def on_agent_start(
        self, index: int, agent_id: str, total_agents: int, total_prompts: int
    ) -> None: ...

    def on_prompt_start(self, agent_id: str, prompt_index: int, total_prompts: int) -> None: ...

    def on_prompt_done(self, agent_id: str, prompt_index: int, total_prompts: int) -> None: ...

    def on_agent_done(self, index: int, agent_id: str, total_agents: int) -> None: ...

    def on_gate_start(self, agent_id: str) -> None: ...


class Heartbeat:
    """Context manager that prints a status line at a fixed interval."""

    def __init__(
        self,
        message_fn: Callable[[], str],
        interval: float = 30.0,
        print_fn: Callable[..., object] | None = None,
    ) -> None:
        self._message_fn = message_fn
        self._interval = interval
        self._print_fn = print_fn or click.echo
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_time = 0.0

    def __enter__(self) -> Heartbeat:
        self._start_time = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            elapsed = int(time.monotonic() - self._start_time)
            mins, secs = divmod(elapsed, 60)
            self._print_fn(f"  ⏳ Still running: {self._message_fn()} ({mins}m {secs}s elapsed)")


class CLIProgressCallback:
    """Default progress callback that prints status lines via click.echo."""

    def on_pipeline_start(self, name: str, total_agents: int) -> None:
        click.echo(f"\nRunning pipeline '{name}' ({total_agents} agents)\n")

    def on_agent_start(
        self, index: int, agent_id: str, total_agents: int, total_prompts: int
    ) -> None:
        click.echo(f"[{index}/{total_agents}] Agent '{agent_id}' ({total_prompts} prompts)...")

    def on_prompt_start(self, agent_id: str, prompt_index: int, total_prompts: int) -> None:
        click.echo(f"  Prompt {prompt_index}/{total_prompts}...")

    def on_prompt_done(self, agent_id: str, prompt_index: int, total_prompts: int) -> None:
        click.echo(f"  Prompt {prompt_index}/{total_prompts} done.")

    def on_agent_done(self, index: int, agent_id: str, total_agents: int) -> None:
        click.echo(f"[{index}/{total_agents}] Agent '{agent_id}' complete.\n")

    def on_gate_start(self, agent_id: str) -> None:
        pass
