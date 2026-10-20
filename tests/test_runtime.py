"""Tests for runtime configuration and command building."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from simplecode.errors import AgentError
from simplecode.runtime import RUNTIMES, exec_cli, make_cli_session_id, make_session_id


def test_make_session_id_unique() -> None:
    ids = {make_session_id() for _ in range(100)}
    assert len(ids) == 100


def test_make_cli_session_id_deterministic() -> None:
    a = make_cli_session_id("sess1", "planner", "claude")
    b = make_cli_session_id("sess1", "planner", "claude")
    assert a == b


def test_make_cli_session_id_differs_by_agent() -> None:
    a = make_cli_session_id("sess1", "planner", "claude")
    b = make_cli_session_id("sess1", "executor", "claude")
    assert a != b


def test_claude_build_new_cmd() -> None:
    rt = RUNTIMES["claude"]
    cmd = rt.build_new_cmd("test-uuid", "write", "opus")

    assert "claude" in cmd
    assert "-p" in cmd
    assert "--session-id" in cmd
    assert "test-uuid" in cmd
    assert "--model" in cmd
    assert "opus" in cmd
    assert "--permission-mode" in cmd
    assert "acceptEdits" in cmd


def test_claude_build_resume_cmd() -> None:
    rt = RUNTIMES["claude"]
    cmd = rt.build_resume_cmd("test-uuid", "write")

    assert "--resume" in cmd
    assert "test-uuid" in cmd


def test_codex_uses_arg_input_mode() -> None:
    rt = RUNTIMES["codex"]
    assert rt.input_mode == "arg"


def test_cursor_build_new_cmd() -> None:
    rt = RUNTIMES["cursor"]
    cmd = rt.build_new_cmd("test-id", "full")

    assert "agent" in cmd
    assert "--force" in cmd
    assert "--trust" in cmd
    assert "--approve-mcps" in cmd


def test_opencode_build_resume_cmd() -> None:
    rt = RUNTIMES["opencode"]
    cmd = rt.build_resume_cmd("test-id", "write")

    assert "--session" in cmd
    assert "test-id" in cmd
    assert "--dangerously-skip-permissions" in cmd


def test_pi_builds_deterministic_session_commands() -> None:
    rt = RUNTIMES["pi"]

    new_cmd = rt.build_new_cmd("test-id", "read-only", "openai/gpt-5.4")
    resume_cmd = rt.build_resume_cmd("test-id", "write")

    assert new_cmd == [
        "pi",
        "--print",
        "--model",
        "openai/gpt-5.4",
        "--tools",
        "read,grep,find,ls",
        "--session-id",
        "test-id",
    ]
    assert resume_cmd == [
        "pi",
        "--print",
        "--tools",
        "read,bash,edit,write,grep,find,ls",
        "--session-id",
        "test-id",
    ]
    assert rt.input_mode == "arg"


def test_all_runtimes_have_permission_flags() -> None:
    for _name, rt in RUNTIMES.items():
        for level in ("read-only", "write", "full"):
            cmd = rt.build_new_cmd("test", level)
            assert isinstance(cmd, list)


def test_exec_cli_raises_on_nonzero_exit(tmp_path: Path) -> None:
    with pytest.raises(AgentError):
        exec_cli(
            [sys.executable, "-c", "import sys; print('err', file=sys.stderr); sys.exit(1)"],
            "",
            tmp_path,
            input_mode="stdin",
            timeout=10,
        )


def test_exec_cli_streams_and_captures(tmp_path: Path) -> None:
    chunks: list[tuple[str, str]] = []

    out = exec_cli(
        [
            sys.executable,
            "-c",
            "import sys; print('hello'); print('world', file=sys.stderr)",
        ],
        "",
        tmp_path,
        input_mode="stdin",
        timeout=10,
        on_output=lambda stream, chunk: chunks.append((stream, chunk)),
    )
    assert "hello" in out
    assert chunks


def test_exec_cli_idle_timeout(tmp_path: Path) -> None:
    with pytest.raises(AgentError, match="Idle timeout"):
        exec_cli(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            "",
            tmp_path,
            input_mode="stdin",
            timeout=1,
        )


def test_exec_cli_cancellation_terminates_process(tmp_path: Path) -> None:
    start = time.monotonic()
    with pytest.raises(AgentError, match="Cancelled by factory operator") as captured:
        exec_cli(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            "",
            tmp_path,
            input_mode="stdin",
            timeout=10,
            cancel_check=lambda: True,
        )

    assert captured.value.exit_code == -2
    assert time.monotonic() - start < 3


def test_exec_cli_cmd_prefix_empty(tmp_path: Path) -> None:
    out = exec_cli(
        [sys.executable, "-c", "print('prefixed')"],
        "",
        tmp_path,
        input_mode="stdin",
        timeout=10,
        cmd_prefix=(),
    )
    assert "prefixed" in out


def test_exec_cli_resets_idle_on_output(tmp_path: Path) -> None:
    script = (
        "import sys, time\n"
        "for i in range(3):\n"
        "    print(i, flush=True)\n"
        "    sys.stdout.buffer.flush()\n"
        "    time.sleep(0.3)\n"
    )
    start = time.monotonic()
    out = exec_cli(
        [sys.executable, "-c", script],
        "",
        tmp_path,
        input_mode="stdin",
        timeout=2,
    )
    elapsed = time.monotonic() - start
    assert "2" in out
    assert elapsed >= 0.8
