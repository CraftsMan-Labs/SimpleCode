"""Runtime abstraction for CLI agent backends (claude, cursor, codex, opencode).

Each runtime defines how to start a new session and how to resume an existing one,
plus permission flags and input modes.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
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


def exec_cli(
    cmd: list[str],
    prompt: str,
    working_dir: str | Path,
    input_mode: str = "stdin",
    timeout: int = 300,
    env_extra: dict[str, str] | None = None,
) -> str:
    """Execute a CLI agent command and return its stdout.

    Raises RuntimeNotFoundError if the binary is missing,
    AgentError if the command exits non-zero or times out.
    """
    env = {**os.environ, **(env_extra or {})}

    if input_mode == "arg":
        full_cmd = [*cmd, prompt]
        stdin_text = None
    else:
        full_cmd = cmd
        stdin_text = prompt

    logger.info("exec: %s", " ".join(full_cmd[:6]))
    logger.debug("prompt length: %d chars", len(prompt))

    try:
        result = subprocess.run(
            full_cmd,
            input=stdin_text,
            cwd=str(working_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        raise RuntimeNotFoundError(full_cmd[0])
    except subprocess.TimeoutExpired:
        raise AgentError(
            runtime=full_cmd[0],
            cmd=full_cmd,
            exit_code=-1,
            stderr=f"Timed out after {timeout}s",
        )

    output = result.stdout or result.stderr or "(no output)"
    if result.returncode != 0:
        logger.warning("Non-zero exit %d from %s", result.returncode, full_cmd[0])

    return output
