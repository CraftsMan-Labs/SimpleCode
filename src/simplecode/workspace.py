"""Git worktree + optional Docker isolation for pipeline runs.

Functional API — no provider classes. Defaults preserve host cwd behaviour:
``branch_strategy="head"`` and no ``docker_image`` → agents run on the host
repo exactly as before.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from simplecode.errors import WorkspaceError

logger = logging.getLogger(__name__)

BRANCH_STRATEGIES = ("head", "branch", "merge-to-head")
_CONTAINER_WORKSPACE = "/workspace"
_WORKTREE_DIRNAME = ".simplecode/worktrees"


@dataclass(frozen=True)
class WorkspaceInfo:
    """Resolved workspace for a pipeline run."""

    host_repo: Path
    working_dir: Path
    branch_strategy: str
    branch: str | None
    worktree_path: Path | None
    docker_image: str | None
    container_id: str | None
    cmd_prefix: tuple[str, ...]
    container_cwd: str | None
    target_branch: str | None = None
    preserved: bool = False
    merge_result: str | None = None


@dataclass
class _WorkspaceState:
    info: WorkspaceInfo
    preserved: bool = False
    merge_result: str | None = None


def _run_git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise WorkspaceError(f"git {' '.join(args)} failed: {detail}")
    return result


def _is_git_repo(path: Path) -> bool:
    result = _run_git(["rev-parse", "--is-inside-work-tree"], path, check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def _repo_root(path: Path) -> Path:
    result = _run_git(["rev-parse", "--show-toplevel"], path)
    return Path(result.stdout.strip()).resolve()


def _current_branch(repo: Path) -> str:
    result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        raise WorkspaceError("Detached HEAD — checkout a branch before using worktrees")
    return branch


def _has_uncommitted_changes(path: Path, *, ignore_simplecode: bool = False) -> bool:
    result = _run_git(["status", "--porcelain"], path)
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if ignore_simplecode:
        lines = [
            ln
            for ln in lines
            if ".simplecode/" not in ln and not ln.rstrip().endswith(".simplecode")
        ]
    return bool(lines)


def _sanitize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "run"


def _timestamp() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y%m%d-%H%M%S")


def _generate_temp_branch(session_id: str | None = None) -> str:
    suffix = secrets.token_hex(3)
    label = _sanitize_name(session_id) if session_id else "run"
    return f"simplecode/{label}/{_timestamp()}-{suffix}"


def _worktree_dir_name(branch: str) -> str:
    return branch.replace("/", "-")


def _list_worktree_paths(repo: Path) -> dict[str, Path]:
    """Map branch name → worktree path for attached worktrees."""
    result = _run_git(["worktree", "list", "--porcelain"], repo)
    mapping: dict[str, Path] = {}
    current_path: Path | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line[len("worktree ") :].strip())
        elif line.startswith("branch ") and current_path is not None:
            ref = line[len("branch ") :].strip()
            if ref.startswith("refs/heads/"):
                mapping[ref[len("refs/heads/") :]] = current_path.resolve()
            current_path = None
    return mapping


def _create_or_reuse_worktree(
    repo: Path,
    branch: str,
    *,
    create_branch: bool,
) -> Path:
    worktrees_root = repo / ".simplecode" / "worktrees"
    worktrees_root.mkdir(parents=True, exist_ok=True)
    wt_path = (worktrees_root / _worktree_dir_name(branch)).resolve()

    existing = _list_worktree_paths(repo)
    if branch in existing:
        path = existing[branch]
        managed = path.resolve().is_relative_to(worktrees_root.resolve())
        if not managed:
            raise WorkspaceError(
                f"Branch '{branch}' already checked out at '{path}' "
                f"(outside {_WORKTREE_DIRNAME}). Pick another branch."
            )
        if _has_uncommitted_changes(path):
            logger.warning(
                "Reusing dirty worktree at %s (branch '%s')",
                path,
                branch,
            )
        else:
            logger.info("Reusing worktree at %s (branch '%s')", path, branch)
        return path.resolve()

    if wt_path.exists():
        raise WorkspaceError(
            f"Worktree path exists but is not registered: {wt_path}. "
            "Remove it or run `git worktree prune`."
        )

    if create_branch:
        # Prefer checkout existing branch; else create from HEAD.
        add = _run_git(
            ["worktree", "add", str(wt_path), branch],
            repo,
            check=False,
        )
        if add.returncode != 0:
            detail = (add.stderr or add.stdout or "").strip()
            if "invalid reference" in detail.lower() or "not a valid" in detail.lower():
                _run_git(
                    ["worktree", "add", "-b", branch, str(wt_path), "HEAD"],
                    repo,
                )
            else:
                raise WorkspaceError(f"git worktree add failed: {detail}")
    else:
        _run_git(["worktree", "add", "-b", branch, str(wt_path), "HEAD"], repo)

    return wt_path


def _remove_worktree(repo: Path, worktree_path: Path) -> None:
    result = _run_git(
        ["worktree", "remove", "--force", str(worktree_path)],
        repo,
        check=False,
    )
    if result.returncode != 0:
        logger.warning(
            "Failed to remove worktree %s: %s",
            worktree_path,
            (result.stderr or result.stdout or "").strip(),
        )
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)


def _merge_to_host(repo: Path, source_branch: str, target_branch: str) -> str:
    """Merge *source_branch* into *target_branch* on the host repo.

    Returns ``"merged"``, ``"conflict"``, or ``"dirty-host"``.
    """
    if _has_uncommitted_changes(repo, ignore_simplecode=True):
        logger.warning(
            "Host repo dirty — cannot merge '%s' into '%s'",
            source_branch,
            target_branch,
        )
        return "dirty-host"

    current = _current_branch(repo)
    switched = False
    try:
        if current != target_branch:
            _run_git(["checkout", target_branch], repo)
            switched = True
        merge = _run_git(
            ["merge", "--no-ff", "-m", f"simplecode: merge {source_branch}", source_branch],
            repo,
            check=False,
        )
        if merge.returncode != 0:
            _run_git(["merge", "--abort"], repo, check=False)
            return "conflict"
        return "merged"
    finally:
        if switched:
            _run_git(["checkout", current], repo, check=False)


_DEFAULT_STACK_NETWORK = "simplecode-stack"


def _docker_network_exists(name: str) -> bool:
    result = subprocess.run(
        ["docker", "network", "inspect", name],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _resolve_stack_network() -> str | None:
    """Join the shared agent stack when present.

    ``SIMPLECODE_DOCKER_NETWORK`` overrides the default name. Set it to empty
    to skip joining. Missing networks are ignored so local runs still work.
    """
    if "SIMPLECODE_DOCKER_NETWORK" in os.environ:
        configured = os.environ["SIMPLECODE_DOCKER_NETWORK"].strip()
        if not configured:
            return None
        if not _docker_network_exists(configured):
            raise WorkspaceError(
                f"SIMPLECODE_DOCKER_NETWORK='{configured}' does not exist"
            )
        return configured
    if _docker_network_exists(_DEFAULT_STACK_NETWORK):
        return _DEFAULT_STACK_NETWORK
    return None


def _start_container(image: str, host_path: Path) -> str:
    name = f"simplecode-{secrets.token_hex(4)}"
    cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--add-host",
        "host.docker.internal:host-gateway",
        "-v",
        f"{host_path.resolve()}:{_CONTAINER_WORKSPACE}",
        "-w",
        _CONTAINER_WORKSPACE,
    ]
    network = _resolve_stack_network()
    if network:
        cmd.extend(["--network", network])
        logger.info("Attaching container to Docker network %s", network)
    cmd.extend([image, "sleep", "infinity"])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise WorkspaceError(f"Failed to start Docker container from '{image}': {detail}")
    container_id = result.stdout.strip()
    if not container_id:
        raise WorkspaceError("Docker run returned empty container id")
    logger.info("Started container %s (%s) mounting %s", name, container_id[:12], host_path)
    return container_id


def _stop_container(container_id: str) -> None:
    result = subprocess.run(
        ["docker", "rm", "-f", container_id],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning(
            "Failed to stop container %s: %s",
            container_id[:12],
            (result.stderr or result.stdout or "").strip(),
        )


def docker_exec_prefix(container_id: str, cwd: str = _CONTAINER_WORKSPACE) -> tuple[str, ...]:
    """Argv prefix for ``docker exec -i`` into a running container."""
    return ("docker", "exec", "-i", "-w", cwd, container_id)


def finalize_workspace(
    state: _WorkspaceState,
    *,
    success: bool,
) -> WorkspaceInfo:
    """Merge / preserve worktree based on pipeline outcome. Mutates *state*."""
    info = state.info
    if info.branch_strategy == "head" or info.worktree_path is None:
        state.merge_result = "n/a"
        state.info = _refresh_info(state)
        return state.info

    wt = info.worktree_path
    dirty = _has_uncommitted_changes(wt)

    if info.branch_strategy == "branch":
        # Named branch worktrees are reusable — never auto-remove.
        state.preserved = True
        state.merge_result = "preserved"
        if dirty:
            logger.warning("Worktree has uncommitted changes: %s", wt)
        state.info = _refresh_info(state)
        return state.info

    # merge-to-head
    if not success:
        state.preserved = True
        state.merge_result = "preserved-failed"
        logger.info("Pipeline unsuccessful — preserving worktree at %s", wt)
        state.info = _refresh_info(state)
        return state.info

    if dirty:
        state.preserved = True
        state.merge_result = "preserved-dirty"
        logger.warning(
            "Uncommitted changes in worktree — preserving %s (no merge)",
            wt,
        )
        state.info = _refresh_info(state)
        return state.info

    assert info.branch is not None
    assert info.target_branch is not None
    merge_result = _merge_to_host(info.host_repo, info.branch, info.target_branch)
    state.merge_result = merge_result
    if merge_result == "merged":
        _remove_worktree(info.host_repo, wt)
        # Drop the ephemeral branch after successful merge.
        _run_git(["branch", "-D", info.branch], info.host_repo, check=False)
        state.preserved = False
    else:
        state.preserved = True
        logger.warning(
            "Merge result=%s — preserving worktree at %s",
            merge_result,
            wt,
        )
    state.info = _refresh_info(state)
    return state.info


def _refresh_info(state: _WorkspaceState) -> WorkspaceInfo:
    info = state.info
    return WorkspaceInfo(
        host_repo=info.host_repo,
        working_dir=info.working_dir,
        branch_strategy=info.branch_strategy,
        branch=info.branch,
        worktree_path=info.worktree_path,
        docker_image=info.docker_image,
        container_id=info.container_id,
        cmd_prefix=info.cmd_prefix,
        container_cwd=info.container_cwd,
        target_branch=info.target_branch,
        preserved=state.preserved,
        merge_result=state.merge_result,
    )


@contextmanager
def open_workspace(
    repo_dir: str | Path,
    *,
    branch_strategy: str = "head",
    branch: str | None = None,
    docker_image: str | None = None,
    session_id: str | None = None,
    resume_worktree: str | Path | None = None,
) -> Iterator[_WorkspaceState]:
    """Open a workspace for a pipeline run.

    Yields mutable :class:`_WorkspaceState`. Caller should call
    :func:`finalize_workspace` before exit when the pipeline outcome is known.
    Container is always torn down on exit. Named ``branch`` worktrees are kept;
    ``merge-to-head`` worktrees are removed only after a clean successful merge.
    """
    if branch_strategy not in BRANCH_STRATEGIES:
        raise WorkspaceError(
            f"Invalid branch_strategy '{branch_strategy}'. "
            f"Expected one of: {', '.join(BRANCH_STRATEGIES)}"
        )
    if branch_strategy == "branch" and not branch and not resume_worktree:
        raise WorkspaceError("branch_strategy='branch' requires branch=...")
    if branch_strategy == "head" and branch and not resume_worktree:
        # Ignore leftover branch metadata from prior runs / session rows.
        branch = None

    root = Path(repo_dir).resolve()
    if not root.is_dir():
        raise WorkspaceError(f"Working directory does not exist: {root}")

    needs_git = branch_strategy != "head" or resume_worktree is not None
    if needs_git and not _is_git_repo(root):
        raise WorkspaceError(f"Not a git repository: {root}")

    host_repo = _repo_root(root) if _is_git_repo(root) else root
    target_branch: str | None = None
    worktree_path: Path | None = None
    effective_branch: str | None = None
    working_dir = host_repo

    if resume_worktree is not None:
        wt = Path(resume_worktree).resolve()
        if not wt.is_dir():
            raise WorkspaceError(f"Resume worktree not found: {wt}")
        worktree_path = wt
        working_dir = wt
        if branch:
            effective_branch = branch
        else:
            effective_branch = _current_branch(wt)
        if branch_strategy == "merge-to-head":
            target_branch = _current_branch(host_repo)
    elif branch_strategy == "head":
        working_dir = host_repo
        effective_branch = _current_branch(host_repo) if _is_git_repo(host_repo) else None
    elif branch_strategy == "branch":
        assert branch is not None
        target_branch = _current_branch(host_repo)
        worktree_path = _create_or_reuse_worktree(host_repo, branch, create_branch=True)
        working_dir = worktree_path
        effective_branch = branch
    else:  # merge-to-head
        target_branch = _current_branch(host_repo)
        effective_branch = _generate_temp_branch(session_id)
        worktree_path = _create_or_reuse_worktree(
            host_repo, effective_branch, create_branch=False
        )
        working_dir = worktree_path

    container_id: str | None = None
    cmd_prefix: tuple[str, ...] = ()
    container_cwd: str | None = None

    if docker_image:
        container_id = _start_container(docker_image, working_dir)
        container_cwd = _CONTAINER_WORKSPACE
        cmd_prefix = docker_exec_prefix(container_id, container_cwd)

    info = WorkspaceInfo(
        host_repo=host_repo,
        working_dir=working_dir,
        branch_strategy=branch_strategy,
        branch=effective_branch,
        worktree_path=worktree_path,
        docker_image=docker_image,
        container_id=container_id,
        cmd_prefix=cmd_prefix,
        container_cwd=container_cwd,
        target_branch=target_branch,
    )
    state = _WorkspaceState(info=info)

    try:
        yield state
    finally:
        if container_id:
            _stop_container(container_id)
