"""Tests for git worktree + Docker workspace helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from simplecode.errors import WorkspaceError
from simplecode.workspace import (
    docker_exec_prefix,
    finalize_workspace,
    open_workspace,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        _git(repo, "checkout", "-b", "main")
    return repo


def test_head_strategy_uses_host_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    with open_workspace(repo, branch_strategy="head") as state:
        assert state.info.working_dir == repo.resolve()
        assert state.info.worktree_path is None
        assert state.info.cmd_prefix == ()
        finalize_workspace(state, success=True)
    assert state.info.merge_result == "n/a"


def test_named_branch_creates_reusable_worktree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    with open_workspace(
        repo, branch_strategy="branch", branch="agent/feature-1"
    ) as state:
        wt = state.info.worktree_path
        assert wt is not None
        assert wt.is_dir()
        assert state.info.branch == "agent/feature-1"
        (wt / "feature.txt").write_text("x\n")
        _git(wt, "add", "feature.txt")
        _git(wt, "commit", "-m", "feature")
        finalize_workspace(state, success=True)

    assert state.info.preserved is True
    assert wt is not None and wt.exists()

    with open_workspace(
        repo, branch_strategy="branch", branch="agent/feature-1"
    ) as state2:
        assert state2.info.worktree_path == wt
        finalize_workspace(state2, success=True)


def test_merge_to_head_merges_and_removes_worktree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    with open_workspace(repo, branch_strategy="merge-to-head", session_id="abc123") as state:
        wt = state.info.worktree_path
        assert wt is not None
        (wt / "change.txt").write_text("y\n")
        _git(wt, "add", "change.txt")
        _git(wt, "commit", "-m", "change")
        branch = state.info.branch
        finalize_workspace(state, success=True)

    assert state.info.merge_result == "merged"
    assert not (wt and wt.exists())
    assert (repo / "change.txt").exists()
    branches = _git(repo, "branch")
    assert branch is not None
    assert branch not in branches


def test_merge_to_head_preserves_dirty_worktree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    with open_workspace(repo, branch_strategy="merge-to-head") as state:
        wt = state.info.worktree_path
        assert wt is not None
        (wt / "dirty.txt").write_text("z\n")
        finalize_workspace(state, success=True)

    assert state.info.merge_result == "preserved-dirty"
    assert state.info.preserved is True
    assert wt.exists()


def test_branch_strategy_requires_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    with pytest.raises(WorkspaceError, match="requires branch"):
        with open_workspace(repo, branch_strategy="branch"):
            pass


def test_docker_exec_prefix() -> None:
    prefix = docker_exec_prefix("abc123")
    assert prefix[:4] == ("docker", "exec", "-i", "-w")
    assert prefix[-1] == "abc123"


def test_docker_image_starts_and_stops_container(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    fake_id = "deadbeefcafebabe"
    real_run = subprocess.run
    seen_run: list[list[str]] = []

    def _side_effect(
        cmd: list[str], **kwargs: object
    ) -> MagicMock | subprocess.CompletedProcess[str]:
        if cmd and cmd[0] == "docker":
            result = MagicMock()
            result.stderr = ""
            if cmd[:3] == ["docker", "network", "inspect"]:
                result.returncode = 1
                result.stdout = ""
                return result
            result.returncode = 0
            if cmd[:2] == ["docker", "run"]:
                seen_run.append(list(cmd))
                result.stdout = fake_id + "\n"
            else:
                result.stdout = ""
            return result
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    with patch("simplecode.workspace.subprocess.run", side_effect=_side_effect):
        with open_workspace(
            repo, branch_strategy="head", docker_image="alpine:3.20"
        ) as state:
            assert state.info.container_id == fake_id
            assert state.info.cmd_prefix[0:3] == ("docker", "exec", "-i")
            assert "/workspace" in state.info.cmd_prefix
            finalize_workspace(state, success=True)

    assert seen_run
    assert "--add-host" in seen_run[0]
    assert "host.docker.internal:host-gateway" in seen_run[0]
    assert "--network" not in seen_run[0]


def test_docker_joins_stack_network_when_present(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    fake_id = "aabbccddeeff0011"
    real_run = subprocess.run
    seen_run: list[list[str]] = []

    def _side_effect(
        cmd: list[str], **kwargs: object
    ) -> MagicMock | subprocess.CompletedProcess[str]:
        if cmd and cmd[0] == "docker":
            result = MagicMock()
            result.stderr = ""
            if cmd[:3] == ["docker", "network", "inspect"]:
                result.returncode = 0
                result.stdout = "[]"
                return result
            result.returncode = 0
            if cmd[:2] == ["docker", "run"]:
                seen_run.append(list(cmd))
                result.stdout = fake_id + "\n"
            else:
                result.stdout = ""
            return result
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    with patch("simplecode.workspace.subprocess.run", side_effect=_side_effect):
        with open_workspace(
            repo, branch_strategy="head", docker_image="alpine:3.20"
        ) as state:
            assert state.info.container_id == fake_id
            finalize_workspace(state, success=True)

    assert "--network" in seen_run[0]
    assert "simplecode-stack" in seen_run[0]


def test_docker_network_env_empty_skips_join(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path)
    fake_id = "1122334455667788"
    real_run = subprocess.run
    seen_run: list[list[str]] = []
    monkeypatch.setenv("SIMPLECODE_DOCKER_NETWORK", "")

    def _side_effect(
        cmd: list[str], **kwargs: object
    ) -> MagicMock | subprocess.CompletedProcess[str]:
        if cmd and cmd[0] == "docker":
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if cmd[:2] == ["docker", "run"]:
                seen_run.append(list(cmd))
                result.stdout = fake_id + "\n"
            else:
                result.stdout = ""
            return result
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    with patch("simplecode.workspace.subprocess.run", side_effect=_side_effect):
        with open_workspace(
            repo, branch_strategy="head", docker_image="alpine:3.20"
        ) as state:
            finalize_workspace(state, success=True)

    assert "--network" not in seen_run[0]
