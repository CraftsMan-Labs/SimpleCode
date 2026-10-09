"""Progress reporting for pipeline execution."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
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


class LifecycleProgressCallback(ProgressCallback, Protocol):
    """Extended optional lifecycle hooks used by durable progress sinks."""

    def on_session(self, session_id: str) -> None: ...

    def on_pipeline_done(self, name: str, total_agents: int, status: str) -> None: ...


def emit_progress(
    progress: object | None,
    method: str,
    *args: object,
) -> None:
    """Call a progress hook when implemented.

    Hook discovery keeps newly added lifecycle methods backwards compatible
    with callbacks written against older SimpleCode releases.
    """
    if progress is None:
        return
    callback = getattr(progress, method, None)
    if callable(callback):
        callback(*args)


class CompositeProgressCallback:
    """Fan events out in order, then re-raise the first callback failure."""

    def __init__(self, callbacks: Iterable[object | None]) -> None:
        self._callbacks = tuple(callback for callback in callbacks if callback is not None)

    def _emit(self, method: str, *args: object) -> None:
        first_error: Exception | None = None
        for callback in self._callbacks:
            try:
                emit_progress(callback, method, *args)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def on_session(self, session_id: str) -> None:
        self._emit("on_session", session_id)

    def on_pipeline_start(self, name: str, total_agents: int) -> None:
        self._emit("on_pipeline_start", name, total_agents)

    def on_pipeline_done(self, name: str, total_agents: int, status: str) -> None:
        self._emit("on_pipeline_done", name, total_agents, status)

    def on_agent_start(
        self, index: int, agent_id: str, total_agents: int, total_prompts: int
    ) -> None:
        self._emit("on_agent_start", index, agent_id, total_agents, total_prompts)

    def on_prompt_start(self, agent_id: str, prompt_index: int, total_prompts: int) -> None:
        self._emit("on_prompt_start", agent_id, prompt_index, total_prompts)

    def on_prompt_done(self, agent_id: str, prompt_index: int, total_prompts: int) -> None:
        self._emit("on_prompt_done", agent_id, prompt_index, total_prompts)

    def on_agent_done(self, index: int, agent_id: str, total_agents: int) -> None:
        self._emit("on_agent_done", index, agent_id, total_agents)

    def on_gate_start(self, agent_id: str) -> None:
        self._emit("on_gate_start", agent_id)

    def on_output(self, stream: str, chunk: str) -> None:
        self._emit("on_output", stream, chunk)


def compose_progress(*callbacks: object | None) -> LifecycleProgressCallback | None:
    """Compose optional current or legacy callbacks into one lifecycle sink."""
    active = tuple(callback for callback in callbacks if callback is not None)
    if not active:
        return None
    return CompositeProgressCallback(active)


def emit_output(progress: ProgressCallback | None, stream: str, chunk: str) -> None:
    """Forward a live output chunk if the callback implements ``on_output``."""
    emit_progress(progress, "on_output", stream, chunk)


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

    def on_session(self, session_id: str) -> None:
        pass

    def on_pipeline_start(self, name: str, total_agents: int) -> None:
        click.echo(f"\nRunning pipeline '{name}' ({total_agents} agents)\n")

    def on_pipeline_done(self, name: str, total_agents: int, status: str) -> None:
        pass

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

    def on_output(self, stream: str, chunk: str) -> None:
        # Live agent output stays quiet by default; verbose callers can wrap.
        pass
