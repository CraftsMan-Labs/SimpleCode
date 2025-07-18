"""Tests for runtime configuration and command building."""

from __future__ import annotations

from simplecode.runtime import RUNTIMES, make_cli_session_id, make_session_id


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


def test_all_runtimes_have_permission_flags() -> None:
    for name, rt in RUNTIMES.items():
        for level in ("read-only", "write", "full"):
            cmd = rt.build_new_cmd("test", level)
            assert isinstance(cmd, list)
