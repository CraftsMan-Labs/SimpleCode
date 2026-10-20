"""Small, credential-safe wrapper around the Coder CLI.

The factory owns GitHub credentials and repository publication.  This module only
provisions an ephemeral Coder workspace, moves Git bundles over ``coder ssh``, and
runs commands inside that workspace.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

CancelCheck = Callable[[], bool]

_REMOTE_ROOT = "/workspace"
_REMOTE_REPOSITORY = f"{_REMOTE_ROOT}/repo"
_INPUT_BUNDLE = f"{_REMOTE_ROOT}/simplecode-input.bundle"
_RESULT_BUNDLE = f"{_REMOTE_ROOT}/simplecode-result.bundle"
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"),
    re.compile(r"(?i)(token\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(https?://[^\s/:@]+:)[^\s/@]+@"),
)


class CoderWorkspaceError(RuntimeError):
    """A sanitized Coder lifecycle or remote-command failure."""


class CoderWorkspaceCancelledError(CoderWorkspaceError):
    """The caller requested cancellation of an active Coder command."""


@dataclass(frozen=True)
class CoderWorkspaceConfig:
    """Settings needed to provision and use one class of Coder workspace."""

    template: str
    organization: str | None = None
    stop_after: str = "2h"
    provision_timeout: float = 600
    command_timeout: float = 3600
    cleanup_timeout: float = 600
    max_bundle_bytes: int = 512 * 1024 * 1024
    url_env: str = "CODER_URL"
    token_env: str = "CODER_SESSION_TOKEN"

    def __post_init__(self) -> None:
        if not self.template.strip():
            raise ValueError("Coder template must not be empty")
        if not self.stop_after.strip():
            raise ValueError("Coder stop_after must not be empty")
        if min(self.provision_timeout, self.command_timeout, self.cleanup_timeout) <= 0:
            raise ValueError("Coder timeouts must be positive")
        if self.max_bundle_bytes <= 0:
            raise ValueError("Coder max_bundle_bytes must be positive")


@dataclass(frozen=True)
class CoderWorkspace:
    """Stable identity of a provisioned workspace."""

    name: str
    template: str


@dataclass(frozen=True)
class CoderWorkspaceStatus:
    """Sanitized output from ``coder show`` and its readiness interpretation."""

    name: str
    ready: bool
    details: str


@dataclass(frozen=True)
class CoderCommandResult:
    """Raw byte output from a local Coder CLI invocation."""

    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


class CoderCommandRunner(Protocol):
    """Injectable command boundary used by tests and alternate launchers."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        input_bytes: bytes | None,
        timeout: float,
        cancel_check: CancelCheck | None = None,
    ) -> CoderCommandResult: ...


def deterministic_workspace_name(job_key: str, attempt: int) -> str:
    """Return a retry-safe Coder workspace name for a factory attempt."""
    if attempt < 1:
        raise ValueError("Factory attempt must be at least 1")
    digest = hashlib.sha256(job_key.encode("utf-8")).hexdigest()[:12]
    return f"sc-{digest}-a{attempt}"


def sanitize_coder_error(value: str, *secrets: str) -> str:
    """Remove credentials and bound error text before persistence or display."""
    cleaned = value
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub(r"\1[REDACTED]", cleaned)
    cleaned = " ".join(cleaned.replace("\x00", "").split())
    return cleaned[:2000]


class SubprocessCoderCommandRunner:
    """Cancellation-aware, shell-free subprocess implementation."""

    poll_interval = 0.1

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        input_bytes: bytes | None,
        timeout: float,
        cancel_check: CancelCheck | None = None,
    ) -> CoderCommandResult:
        args = list(argv)
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env),
            )
        except OSError as exc:
            raise CoderWorkspaceError(f"Unable to start Coder CLI: {exc}") from exc

        started = time.monotonic()
        pending_input = input_bytes
        while True:
            if cancel_check is not None and cancel_check():
                _terminate(process)
                raise CoderWorkspaceCancelledError("Coder command cancelled")
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                _terminate(process)
                raise CoderWorkspaceError(f"Coder command timed out after {timeout:g}s")
            try:
                stdout, stderr = process.communicate(
                    input=pending_input,
                    timeout=min(self.poll_interval, remaining),
                )
                break
            except subprocess.TimeoutExpired:
                pending_input = None

        return CoderCommandResult(tuple(args), process.returncode, stdout, stderr)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class CoderWorkspaceClient:
    """Provision, execute in, transfer bundles to, and delete Coder workspaces."""

    def __init__(
        self,
        config: CoderWorkspaceConfig,
        *,
        runner: CoderCommandRunner | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self._runner = runner or SubprocessCoderCommandRunner()
        self._environ = environ if environ is not None else os.environ

    def create(
        self,
        job_key: str,
        attempt: int,
        *,
        cancel_check: CancelCheck | None = None,
    ) -> CoderWorkspace:
        workspace = CoderWorkspace(
            deterministic_workspace_name(job_key, attempt), self.config.template
        )
        argv = [
            "coder",
            "create",
            "--yes",
            "--template",
            self.config.template,
            "--stop-after",
            self.config.stop_after,
        ]
        if self.config.organization:
            argv.extend(["--org", self.config.organization])
        argv.append(workspace.name)
        self._checked(argv, timeout=self.config.provision_timeout, cancel_check=cancel_check)
        status = self.show(workspace, cancel_check=cancel_check)
        if not status.ready:
            raise CoderWorkspaceError(
                f"Coder workspace {workspace.name} is not ready: {status.details}"
            )
        return workspace

    def show(
        self,
        workspace: CoderWorkspace | str,
        *,
        cancel_check: CancelCheck | None = None,
    ) -> CoderWorkspaceStatus:
        name = _workspace_name(workspace)
        result = self._checked(
            ["coder", "show", name],
            timeout=self.config.provision_timeout,
            cancel_check=cancel_check,
        )
        details = self._sanitize(result.stdout.decode("utf-8", errors="replace"))
        lowered = details.lower()
        failed = any(word in lowered for word in ("failed", "stopped", "offline", "error"))
        ready = not failed and any(word in lowered for word in ("running", "ready", "connected"))
        return CoderWorkspaceStatus(name=name, ready=ready, details=details)

    def run(
        self,
        workspace: CoderWorkspace | str,
        command: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout: float | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> CoderCommandResult:
        """Run argv in the remote repository without allocating a TTY or local shell."""
        if not command:
            raise ValueError("Remote command must not be empty")
        name = _workspace_name(workspace)
        argv = [
            "coder",
            "ssh",
            "--tty=false",
            "--wait=yes",
            name,
            "--",
            "sh",
            "-c",
            f'cd {_REMOTE_REPOSITORY} && exec "$@"',
            "simplecode",
            *command,
        ]
        return self._checked(
            argv,
            input_bytes=input_bytes,
            timeout=timeout or self.config.command_timeout,
            cancel_check=cancel_check,
        )

    def command_prefix(self, workspace: CoderWorkspace | str) -> tuple[str, ...]:
        """Return the safe argv prefix used to execute pipeline agent commands."""
        name = _workspace_name(workspace)
        return (
            "coder",
            "ssh",
            "--tty=false",
            "--wait=yes",
            name,
            "--",
            "sh",
            "-c",
            f'cd {_REMOTE_REPOSITORY} && exec "$@"',
            "simplecode",
        )

    def command_environment(self) -> dict[str, str]:
        """Return local-only Coder CLI authentication and connection settings."""
        env, _token = self._environment()
        return env

    def upload_git_bundle(
        self,
        workspace: CoderWorkspace | str,
        bundle: bytes,
        branch: str,
        *,
        cancel_check: CancelCheck | None = None,
    ) -> None:
        """Upload a bundle and check out its branch at the fixed repository path."""
        self._validate_bundle(bundle)
        ref = _validate_ref(branch)
        name = _workspace_name(workspace)
        self._checked(
            [
                "coder",
                "ssh",
                "--tty=false",
                "--wait=yes",
                name,
                "--",
                "sh",
                "-c",
                f"umask 077; mkdir -p {_REMOTE_ROOT}; cat > {_INPUT_BUNDLE}",
            ],
            input_bytes=bundle,
            timeout=self.config.command_timeout,
            cancel_check=cancel_check,
        )
        self._checked(
            [
                "coder",
                "ssh",
                "--tty=false",
                "--wait=yes",
                name,
                "--",
                "sh",
                "-c",
                (
                    f"rm -rf {_REMOTE_REPOSITORY}; git clone {_INPUT_BUNDLE} "
                    f"{_REMOTE_REPOSITORY}; cd {_REMOTE_REPOSITORY}; "
                    'git checkout -B "$1" "refs/heads/$1"'
                ),
                "simplecode",
                ref,
            ],
            timeout=self.config.command_timeout,
            cancel_check=cancel_check,
        )

    def download_git_bundle(
        self,
        workspace: CoderWorkspace | str,
        branch: str,
        *,
        cancel_check: CancelCheck | None = None,
    ) -> bytes:
        """Require a clean checkout and download the expected branch as a bundle."""
        ref = _validate_ref(branch)
        name = _workspace_name(workspace)
        self._checked(
            [
                "coder",
                "ssh",
                "--tty=false",
                "--wait=yes",
                name,
                "--",
                "sh",
                "-c",
                (
                    f'cd {_REMOTE_REPOSITORY}; test -z "$(git status --porcelain)"; '
                    f'rm -f {_RESULT_BUNDLE}; git bundle create {_RESULT_BUNDLE} "$1"'
                ),
                "simplecode",
                ref,
            ],
            timeout=self.config.command_timeout,
            cancel_check=cancel_check,
        )
        result = self._checked(
            [
                "coder",
                "ssh",
                "--tty=false",
                "--wait=yes",
                name,
                "--",
                "sh",
                "-c",
                f"cat {_RESULT_BUNDLE}",
            ],
            timeout=self.config.command_timeout,
            cancel_check=cancel_check,
        )
        self._validate_bundle(result.stdout)
        return result.stdout

    def delete(
        self,
        workspace: CoderWorkspace | str,
        *,
        cancel_check: CancelCheck | None = None,
    ) -> bool:
        """Delete a workspace; return false when it was already absent."""
        name = _workspace_name(workspace)
        result = self._invoke(
            ["coder", "delete", "--yes", name],
            timeout=self.config.cleanup_timeout,
            cancel_check=cancel_check,
        )
        if result.returncode == 0:
            return True
        message = self._result_error(result)
        lowered = message.lower()
        if any(marker in lowered for marker in ("not found", "does not exist", "404")):
            return False
        raise CoderWorkspaceError(f"Coder command failed ({result.returncode}): {message}")

    def _validate_bundle(self, bundle: bytes) -> None:
        if not bundle:
            raise CoderWorkspaceError("Git bundle is empty")
        if len(bundle) > self.config.max_bundle_bytes:
            raise CoderWorkspaceError(
                f"Git bundle exceeds {self.config.max_bundle_bytes} byte limit"
            )

    def _environment(self) -> tuple[dict[str, str], str]:
        url = self._environ.get(self.config.url_env, "").strip()
        token = self._environ.get(self.config.token_env, "").strip()
        if not url:
            raise CoderWorkspaceError(f"Missing Coder URL environment: {self.config.url_env}")
        if not token:
            raise CoderWorkspaceError(
                f"Missing Coder session token environment: {self.config.token_env}"
            )
        env = dict(self._environ)
        env["CODER_URL"] = url
        env["CODER_SESSION_TOKEN"] = token
        env["CODER_SSH_FORWARD_AGENT"] = "false"
        env.pop("SSH_AUTH_SOCK", None)
        return env, token

    def _invoke(
        self,
        argv: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout: float,
        cancel_check: CancelCheck | None = None,
    ) -> CoderCommandResult:
        env, token = self._environment()
        try:
            return self._runner.run(
                argv,
                env=env,
                input_bytes=input_bytes,
                timeout=timeout,
                cancel_check=cancel_check,
            )
        except CoderWorkspaceCancelledError:
            raise
        except Exception as exc:
            raise CoderWorkspaceError(sanitize_coder_error(str(exc), token)) from exc

    def _checked(
        self,
        argv: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout: float,
        cancel_check: CancelCheck | None = None,
    ) -> CoderCommandResult:
        result = self._invoke(
            argv,
            input_bytes=input_bytes,
            timeout=timeout,
            cancel_check=cancel_check,
        )
        if result.returncode != 0:
            raise CoderWorkspaceError(
                f"Coder command failed ({result.returncode}): {self._result_error(result)}"
            )
        return result

    def _result_error(self, result: CoderCommandResult) -> str:
        env, token = self._environment()
        del env
        raw = (result.stderr or result.stdout).decode("utf-8", errors="replace")
        return sanitize_coder_error(raw, token)

    def _sanitize(self, value: str) -> str:
        env, token = self._environment()
        del env
        return sanitize_coder_error(value, token)


def _workspace_name(workspace: CoderWorkspace | str) -> str:
    name = workspace.name if isinstance(workspace, CoderWorkspace) else workspace
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
        raise ValueError(f"Invalid Coder workspace name: {name!r}")
    return name


def _validate_ref(branch: str) -> str:
    if not _REF_RE.fullmatch(branch) or ".." in branch or branch.endswith(("/", ".lock")):
        raise ValueError(f"Invalid Git branch: {branch!r}")
    return branch


__all__ = [
    "CancelCheck",
    "CoderCommandResult",
    "CoderCommandRunner",
    "CoderWorkspace",
    "CoderWorkspaceCancelledError",
    "CoderWorkspaceClient",
    "CoderWorkspaceConfig",
    "CoderWorkspaceError",
    "CoderWorkspaceStatus",
    "SubprocessCoderCommandRunner",
    "deterministic_workspace_name",
    "sanitize_coder_error",
]
