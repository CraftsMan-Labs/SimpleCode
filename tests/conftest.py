"""Shared fixtures for simplecode tests."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Capture before tests patch subprocess.Popen (shared module object).
_REAL_POPEN = subprocess.Popen


def make_popen_mock(
    handler: Callable[[list[str], str | None], tuple[int, str, str]],
) -> Callable[..., MagicMock | subprocess.Popen[Any]]:
    """Build a ``subprocess.Popen`` mock compatible with streaming ``exec_cli``.

    *handler(cmd, stdin_text) -> (returncode, stdout, stderr)*.
    ``stdin_text`` is whatever was written to the process stdin (decoded), or
    ``None`` when stdin is unused (arg input mode).

    Non-agent commands (``git``, ``docker``) are delegated to the real Popen so
    workspace setup still works while ``subprocess.Popen`` is patched.
    """

    def popen(
        cmd: list[str] | str, **kwargs: Any
    ) -> MagicMock | subprocess.Popen[Any]:
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)]
        if argv and argv[0] in {"git", "docker"}:
            return _REAL_POPEN(cmd, **kwargs)

        proc = MagicMock()
        proc.args = argv
        stdin_chunks: list[str] = []

        def write(data: bytes | str) -> int:
            if isinstance(data, bytes):
                stdin_chunks.append(data.decode("utf-8", errors="replace"))
            else:
                stdin_chunks.append(data)
            return len(data)

        proc.stdin = MagicMock()
        proc.stdin.write = write
        proc.stdin.close = MagicMock()

        state: dict[str, Any] = {"ready": False, "rc": 0, "out": b"", "err": b""}

        def _ensure() -> None:
            if state["ready"]:
                return
            stdin_text = "".join(stdin_chunks) if stdin_chunks else None
            rc, out, err = handler(argv, stdin_text)
            state["rc"] = rc
            state["out"] = out.encode("utf-8") if isinstance(out, str) else out
            state["err"] = err.encode("utf-8") if isinstance(err, str) else err
            state["ready"] = True

        out_sent = [False]
        err_sent = [False]

        def read_out(_n: int = -1) -> bytes:
            _ensure()
            if not out_sent[0]:
                out_sent[0] = True
                return state["out"]
            return b""

        def read_err(_n: int = -1) -> bytes:
            _ensure()
            if not err_sent[0]:
                err_sent[0] = True
                return state["err"]
            return b""

        proc.stdout = MagicMock()
        proc.stderr = MagicMock()
        proc.stdout.read = read_out
        proc.stderr.read = read_err
        proc.stdout.close = MagicMock()
        proc.stderr.close = MagicMock()
        proc.poll = lambda: (_ensure() or state["rc"])
        proc.wait = lambda timeout=None: (_ensure() or state["rc"])
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        # subprocess.run uses Popen as a context manager
        proc.__enter__ = lambda self: self
        proc.__exit__ = lambda *a: None
        proc.communicate = lambda *a, **k: (
            _ensure() or (state["out"], state["err"])
        )
        return proc

    return popen


@pytest.fixture()
def pipeline_dir(tmp_path: Path) -> Path:
    """Create a minimal pipeline directory with YAML and numbered prompts."""
    prompts_a = tmp_path / "prompts" / "agent-a"
    prompts_a.mkdir(parents=True)
    (prompts_a / "1.md").write_text("Describe the project structure in one paragraph.")
    (prompts_a / "2.md").write_text("List the top 3 files by importance.")

    prompts_b = tmp_path / "prompts" / "agent-b"
    prompts_b.mkdir(parents=True)
    (prompts_b / "1.md").write_text("Based on the context, suggest one improvement.")

    yaml_content = """\
name: test-pipeline
description: A minimal test pipeline

agents:
  - id: agent-a
    prompts: prompts/agent-a/
    permissions: read-only
    timeout: 120

  - id: agent-b
    prompts: prompts/agent-b/
    permissions: read-only
    timeout: 120
"""
    (tmp_path / "pipeline.yaml").write_text(yaml_content)
    return tmp_path


@pytest.fixture()
def gated_pipeline_dir(tmp_path: Path) -> Path:
    """Create a pipeline with a human gate on the first agent."""
    prompts_a = tmp_path / "prompts" / "planner"
    prompts_a.mkdir(parents=True)
    (prompts_a / "1.md").write_text("List all files in this project.")

    prompts_b = tmp_path / "prompts" / "executor"
    prompts_b.mkdir(parents=True)
    (prompts_b / "1.md").write_text("Based on the context, echo back the plan.")

    yaml_content = """\
name: gated-pipeline
description: Pipeline with a human gate

agents:
  - id: planner
    prompts: prompts/planner/
    gate: human
    permissions: read-only
    timeout: 120

  - id: executor
    prompts: prompts/executor/
    permissions: read-only
    timeout: 120
"""
    (tmp_path / "pipeline.yaml").write_text(yaml_content)
    return tmp_path


@pytest.fixture()
def setup_pipeline_dir(tmp_path: Path) -> Path:
    """Create a pipeline with a setup.sh script."""
    prompts = tmp_path / "prompts" / "agent"
    prompts.mkdir(parents=True)
    (prompts / "1.md").write_text("Say hello.")

    setup = tmp_path / "setup.sh"
    setup.write_text("#!/usr/bin/env bash\nset -euo pipefail\necho 'setup done'\n")
    setup.chmod(0o755)

    yaml_content = """\
name: setup-pipeline
description: Pipeline with setup script
setup: setup.sh

agents:
  - id: agent
    prompts: prompts/agent/
    permissions: read-only
    timeout: 120
"""
    (tmp_path / "pipeline.yaml").write_text(yaml_content)
    return tmp_path
