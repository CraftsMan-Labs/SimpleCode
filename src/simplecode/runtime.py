"""Runtime abstraction for CLI agent backends (claude, cursor, codex, opencode).

Each runtime defines how to start a new session and how to resume an existing one,
plus permission flags and input modes.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from simplecode.errors import AgentError, RuntimeNotFoundError

logger = logging.getLogger(__name__)

PERMISSION_LEVELS = ("read-only", "write", "full")

PERMISSION_FLAGS: dict[str, dict[str, list[str]]] = {
    "claude": {
        "read-only": ["--permission-mode", "plan"],
        "write": ["--permission-mode", "acceptEdits"],
        "full": ["--permission-mode", "bypassPermissions"],
    },
    "cursor": {
        "read-only": ["--mode", "ask", "--trust"],
        "write": ["--force", "--trust"],
        "full": ["--force", "--trust", "--approve-mcps"],
    },
    "codex": {
        "read-only": ["--sandbox", "read-only", "--ask-for-approval", "never"],
        "write": ["--sandbox", "workspace-write", "--ask-for-approval", "never"],
        "full": ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
    },
    "opencode": {
        "read-only": [],
        "write": ["--dangerously-skip-permissions"],
        "full": ["--dangerously-skip-permissions"],
    },
}

OnOutput = Callable[[str, str], None]  # (stream_name, chunk)


@dataclass(frozen=True)
class RuntimeConfig:
    """How to invoke a specific CLI agent backend."""

    name: str
    cmd_new: tuple[str, ...]
    cmd_resume: tuple[str, ...]
    session_flag_new: str | None = None
    session_flag_resume: str | None = None
    input_mode: str = "stdin"  # "stdin" or "arg"
    model_flag: str = "--model"
    resume_needs_session_id: bool = True

    def build_new_cmd(
        self,
        session_id: str,
        permissions: str = "write",
        model: str | None = None,
    ) -> list[str]:
        cmd = list(self.cmd_new)
        if model:
            cmd.extend([self.model_flag, model])
        cmd.extend(PERMISSION_FLAGS.get(self.name, {}).get(permissions, []))
        if self.session_flag_new:
            cmd.extend([self.session_flag_new, session_id])
        return cmd

    def build_resume_cmd(
        self,
        session_id: str,
        permissions: str = "write",
        model: str | None = None,
    ) -> list[str]:
        cmd = list(self.cmd_resume)
        if model:
            cmd.extend([self.model_flag, model])
        cmd.extend(PERMISSION_FLAGS.get(self.name, {}).get(permissions, []))
        if self.session_flag_resume and self.resume_needs_session_id:
            cmd.extend([self.session_flag_resume, session_id])
        return cmd


RUNTIMES: dict[str, RuntimeConfig] = {
    "claude": RuntimeConfig(
        name="claude",
        cmd_new=("claude", "-p", "--output-format", "text"),
        cmd_resume=("claude", "-p", "--output-format", "text"),
        session_flag_new="--session-id",
        session_flag_resume="--resume",
    ),
    "cursor": RuntimeConfig(
        name="cursor",
        cmd_new=("agent", "-p", "--output-format", "text"),
        cmd_resume=("agent", "-p", "--output-format", "text"),
        session_flag_new=None,
        session_flag_resume="--resume",
    ),
    "codex": RuntimeConfig(
        name="codex",
        cmd_new=("codex", "exec"),
        cmd_resume=("codex", "exec", "resume", "--last"),
        session_flag_new=None,
        session_flag_resume=None,
        input_mode="arg",
        model_flag="-m",
        resume_needs_session_id=False,
    ),
    "opencode": RuntimeConfig(
        name="opencode",
        cmd_new=("opencode", "run"),
        cmd_resume=("opencode", "run"),
        session_flag_new=None,
        session_flag_resume="--session",
        input_mode="arg",
        model_flag="-m",
    ),
}

SUPPORTED_RUNTIMES = sorted(RUNTIMES.keys())


def make_session_id() -> str:
    """Generate a short unique session ID."""
    return uuid.uuid4().hex[:8]


def make_cli_session_id(session_id: str, agent_id: str, runtime: str) -> str:
    """Deterministic CLI-level session ID from pipeline session + agent ID."""
    seed = f"{session_id}-{agent_id}"
    if runtime == "claude":
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
    return seed


def _stream_until_done(
    proc: subprocess.Popen[bytes],
    *,
    idle_timeout: int,
    on_output: OnOutput | None,
    runtime_name: str,
    full_cmd: list[str],
) -> tuple[str, str, int]:
    """Read pipes on background threads; kill on idle silence."""
    assert proc.stdout is not None
    assert proc.stderr is not None

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    lock = threading.Lock()
    last_activity = time.monotonic()

    def _reader(stream: object, name: str, sink: list[str]) -> None:
        nonlocal last_activity
        try:
            while True:
                raw = stream.read(4096)  # type: ignore[attr-defined]
                if not raw:
                    break
                chunk = raw.decode("utf-8", errors="replace")
                with lock:
                    sink.append(chunk)
                    last_activity = time.monotonic()
                if on_output:
                    try:
                        on_output(name, chunk)
                    except Exception:
                        logger.debug("on_output callback failed", exc_info=True)
        finally:
            try:
                stream.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    t_out = threading.Thread(
        target=_reader, args=(proc.stdout, "stdout", stdout_chunks), daemon=True
    )
    t_err = threading.Thread(
        target=_reader, args=(proc.stderr, "stderr", stderr_chunks), daemon=True
    )
    t_out.start()
    t_err.start()

    timed_out = False
    while True:
        if proc.poll() is not None and not t_out.is_alive() and not t_err.is_alive():
            break
        with lock:
            silent_for = time.monotonic() - last_activity
        if proc.poll() is None and silent_for >= idle_timeout:
            timed_out = True
            _kill_process(proc)
            break
        time.sleep(0.05)

    t_out.join(timeout=2)
    t_err.join(timeout=2)
    exit_code = proc.poll()
    if exit_code is None:
        _kill_process(proc)
        exit_code = proc.wait()

    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)

    if timed_out:
        raise AgentError(
            runtime=runtime_name,
            cmd=full_cmd,
            exit_code=-1,
            stderr=f"Idle timeout after {idle_timeout}s with no output",
        )

    return stdout, stderr, exit_code


def _kill_process(proc: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def exec_cli(
    cmd: list[str],
    prompt: str,
    working_dir: str | Path,
    input_mode: str = "stdin",
    timeout: int = 300,
    env_extra: dict[str, str] | None = None,
    cmd_prefix: tuple[str, ...] | list[str] | None = None,
    on_output: OnOutput | None = None,
) -> str:
    """Execute a CLI agent command and return its stdout.

    Streams stdout/stderr live. *timeout* is an **idle** silence window —
    any output resets the deadline. Silent processes are terminated then killed.

    *cmd_prefix* (e.g. ``docker exec -i …``) is prepended to *cmd*. When set,
    host ``cwd`` is omitted (container ``-w`` owns the working directory).

    Raises RuntimeNotFoundError if the binary is missing,
    AgentError if the command exits non-zero or times out.
    """
    env = {**os.environ, **(env_extra or {})}
    prefix = list(cmd_prefix) if cmd_prefix else []

    if input_mode == "arg":
        full_cmd = [*prefix, *cmd, prompt]
        stdin_bytes = None
    else:
        full_cmd = [*prefix, *cmd]
        stdin_bytes = prompt.encode("utf-8")

    logger.info("exec: %s", " ".join(full_cmd[:8]))
    logger.debug("prompt length: %d chars", len(prompt))

    cwd = None if prefix else str(working_dir)
    runtime_name = cmd[0] if cmd else full_cmd[0]

    try:
        proc = subprocess.Popen(
            full_cmd,
            stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            bufsize=0,
        )
    except FileNotFoundError as e:
        raise RuntimeNotFoundError(full_cmd[0]) from e

    if stdin_bytes is not None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin_bytes)
            proc.stdin.close()
        except BrokenPipeError:
            pass

    try:
        stdout, stderr, exit_code = _stream_until_done(
            proc,
            idle_timeout=timeout,
            on_output=on_output,
            runtime_name=runtime_name,
            full_cmd=full_cmd,
        )
    except AgentError:
        raise
    except Exception:
        _kill_process(proc)
        raise

    if exit_code != 0:
        raise AgentError(
            runtime=runtime_name,
            cmd=full_cmd,
            exit_code=exit_code,
            stderr=stderr or stdout or "(no output)",
        )

    return stdout or stderr or "(no output)"
