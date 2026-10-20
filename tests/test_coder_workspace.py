from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence

import pytest

from simplecode.coder_workspace import (
    CoderCommandResult,
    CoderWorkspaceCancelledError,
    CoderWorkspaceClient,
    CoderWorkspaceConfig,
    CoderWorkspaceError,
    SubprocessCoderCommandRunner,
    deterministic_workspace_name,
    sanitize_coder_error,
)


class FakeRunner:
    def __init__(self, responses: list[tuple[int, bytes, bytes]]) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], dict[str, str], bytes | None, float]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        input_bytes: bytes | None,
        timeout: float,
        cancel_check: Callable[[], bool] | None = None,
    ) -> CoderCommandResult:
        del cancel_check
        args = tuple(argv)
        self.calls.append((args, dict(env), input_bytes, timeout))
        returncode, stdout, stderr = self.responses.pop(0)
        return CoderCommandResult(args, returncode, stdout, stderr)


def _client(runner: FakeRunner, *, max_bundle_bytes: int = 1024) -> CoderWorkspaceClient:
    return CoderWorkspaceClient(
        CoderWorkspaceConfig(
            template="factory-template",
            organization="engineering",
            stop_after="90m",
            max_bundle_bytes=max_bundle_bytes,
        ),
        runner=runner,
        environ={
            "CODER_URL": "https://coder.example.test",
            "CODER_SESSION_TOKEN": "secret-session-token",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
        },
    )


def test_deterministic_workspace_name_is_stable_per_attempt() -> None:
    assert deterministic_workspace_name("plane:project:item-1", 2) == (
        deterministic_workspace_name("plane:project:item-1", 2)
    )
    assert deterministic_workspace_name("plane:project:item-1", 2).startswith("sc-")
    assert deterministic_workspace_name("plane:project:item-1", 2).endswith("-a2")
    assert deterministic_workspace_name("plane:project:item-1", 1) != (
        deterministic_workspace_name("plane:project:item-1", 2)
    )
    with pytest.raises(ValueError, match="at least 1"):
        deterministic_workspace_name("job", 0)


def test_create_uses_cli_environment_not_secret_argv_and_checks_ready() -> None:
    runner = FakeRunner([(0, b"created", b""), (0, b"Status: Running\nAgent: Connected", b"")])
    client = _client(runner)

    workspace = client.create("job-7", 3)

    create_argv, env, _, timeout = runner.calls[0]
    assert create_argv[:3] == ("coder", "create", "--yes")
    assert create_argv[-1] == workspace.name
    assert ("--template", "factory-template") == create_argv[3:5]
    assert "--org" in create_argv and "engineering" in create_argv
    assert "secret-session-token" not in create_argv
    assert env["CODER_SESSION_TOKEN"] == "secret-session-token"
    assert env["CODER_URL"] == "https://coder.example.test"
    assert env["CODER_SSH_FORWARD_AGENT"] == "false"
    assert "SSH_AUTH_SOCK" not in env
    assert timeout == 600
    assert runner.calls[1][0] == ("coder", "show", workspace.name)


def test_create_rejects_workspace_that_is_not_ready() -> None:
    runner = FakeRunner([(0, b"created", b""), (0, b"Status: Failed", b"")])
    with pytest.raises(CoderWorkspaceError, match="not ready"):
        _client(runner).create("job", 1)


def test_run_is_noninteractive_preserves_byte_io_and_argument_boundaries() -> None:
    runner = FakeRunner([(0, b"result\x00", b"")])
    client = _client(runner)
    result = client.run(
        "sc-abc-a1", ["python", "-c", "print('hello world')"], input_bytes=b"in\x00"
    )

    argv, _, input_bytes, _ = runner.calls[0]
    assert argv[:5] == ("coder", "ssh", "--tty=false", "--wait=yes", "sc-abc-a1")
    assert argv[-3:] == ("python", "-c", "print('hello world')")
    assert input_bytes == b"in\x00"
    assert result.stdout == b"result\x00"


def test_pipeline_command_boundary_is_noninteractive_and_authenticated() -> None:
    client = _client(FakeRunner([]))

    prefix = client.command_prefix("sc-abc-a1")
    env = client.command_environment()

    assert prefix[:5] == ("coder", "ssh", "--tty=false", "--wait=yes", "sc-abc-a1")
    assert prefix[-4:-1] == (
        "sh",
        "-c",
        'cd /workspace/repo && exec "$@"',
    )
    assert prefix[-1] == "simplecode"
    assert "secret-session-token" not in prefix
    assert env["CODER_URL"] == "https://coder.example.test"
    assert env["CODER_SESSION_TOKEN"] == "secret-session-token"
    assert env["CODER_SSH_FORWARD_AGENT"] == "false"
    assert "SSH_AUTH_SOCK" not in env


def test_bundle_upload_and_download_use_fixed_remote_paths() -> None:
    bundle = b"git bundle bytes"
    runner = FakeRunner(
        [
            (0, b"", b""),
            (0, b"", b""),
            (0, b"", b""),
            (0, bundle, b""),
        ]
    )
    client = _client(runner)

    client.upload_git_bundle("sc-abc-a1", bundle, "factory/job-1")
    downloaded = client.download_git_bundle("sc-abc-a1", "factory/job-1")

    assert runner.calls[0][2] == bundle
    assert "/workspace/simplecode-input.bundle" in " ".join(runner.calls[0][0])
    assert "/workspace/repo" in " ".join(runner.calls[1][0])
    assert runner.calls[1][0][-1] == "factory/job-1"
    assert "/workspace/simplecode-result.bundle" in " ".join(runner.calls[2][0])
    assert runner.calls[2][0][-1] == "factory/job-1"
    assert downloaded == bundle


def test_bundle_size_limit_is_applied_on_upload_and_download() -> None:
    client = _client(FakeRunner([]), max_bundle_bytes=3)
    with pytest.raises(CoderWorkspaceError, match="exceeds 3 byte"):
        client.upload_git_bundle("sc-abc-a1", b"four", "branch")

    runner = FakeRunner([(0, b"", b""), (0, b"four", b"")])
    with pytest.raises(CoderWorkspaceError, match="exceeds 3 byte"):
        _client(runner, max_bundle_bytes=3).download_git_bundle("sc-abc-a1", "branch")


def test_delete_is_idempotent() -> None:
    deleted = _client(FakeRunner([(0, b"deleted", b"")])).delete("sc-abc-a1")
    absent = _client(FakeRunner([(1, b"", b"workspace not found")])).delete("sc-abc-a1")
    assert deleted is True
    assert absent is False


def test_error_is_sanitized_and_token_never_appears() -> None:
    runner = FakeRunner(
        [(1, b"", b"Authorization: Bearer secret-session-token https://u:p@example.test")]
    )
    with pytest.raises(CoderWorkspaceError) as captured:
        _client(runner).delete("sc-abc-a1")
    message = str(captured.value)
    assert "secret-session-token" not in message
    assert "https://u:p@" not in message
    assert "[REDACTED]" in message
    assert "secret-session-token" not in runner.calls[0][0]


def test_missing_coder_environment_fails_before_invocation() -> None:
    runner = FakeRunner([])
    client = CoderWorkspaceClient(
        CoderWorkspaceConfig(template="factory"), runner=runner, environ={}
    )
    with pytest.raises(CoderWorkspaceError, match="Missing Coder URL"):
        client.show("sc-abc-a1")
    assert runner.calls == []


def test_subprocess_runner_terminates_on_cancellation() -> None:
    runner = SubprocessCoderCommandRunner()
    cancelled = threading.Event()

    def request_cancel() -> bool:
        return cancelled.is_set()

    timer = threading.Timer(0.1, cancelled.set)
    timer.start()
    try:
        with pytest.raises(CoderWorkspaceCancelledError):
            runner.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                env=os.environ,
                input_bytes=None,
                timeout=5,
                cancel_check=request_cancel,
            )
    finally:
        timer.cancel()


def test_subprocess_runner_enforces_timeout() -> None:
    runner = SubprocessCoderCommandRunner()
    started = time.monotonic()
    with pytest.raises(CoderWorkspaceError, match="timed out"):
        runner.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=os.environ,
            input_bytes=None,
            timeout=0.1,
        )
    assert time.monotonic() - started < 3


def test_sanitize_coder_error_bounds_and_redacts() -> None:
    cleaned = sanitize_coder_error("token=abc https://user:password@example.test " + "x" * 3000)
    assert "abc" not in cleaned
    assert "password" not in cleaned
    assert len(cleaned) == 2000
