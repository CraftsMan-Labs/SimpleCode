"""GitHub repository manager and draft-PR publisher.

All git/gh invocations use argv lists (never ``shell=True``). A command runner
can be injected so unit tests never call the network, ``gh``, or a real clone.
Credentials that appear in remotes or command output are stripped before log
or exception text is produced.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse, urlunparse

from simplecode.errors import SimpleCodeError

logger = logging.getLogger(__name__)

_DEFAULT_REPOS_DIR = Path.home() / ".simplecode" / "repos"
_MANAGED_WORKTREE_DIR = Path(".simplecode") / "worktrees"

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")
_PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<n>[1-9][0-9]*)$"
)
_TOKEN_RE = re.compile(r"(?i)(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[a-zA-Z0-9_]+")
_HEADER_SECRET_RE = re.compile(r"(?i)(authorization|x-access-token)\s*[:=]\s*\S+")
_MAX_ERROR = 240

_CLONE_TIMEOUT = 300.0
_GIT_TIMEOUT = 120.0
_PR_TIMEOUT = 60.0


class GitHubError(SimpleCodeError):
    """Base error for GitHub clone, update, or publish failures."""


class GitHubValidationError(GitHubError):
    """Owner/repo, branch, or pull-request URL failed validation."""


class GitHubOriginMismatchError(GitHubError):
    """Local checkout origin does not match the allowlisted owner/repo."""


class GitHubDirtyHostError(GitHubError):
    """Host checkout has uncommitted changes (excluding managed worktrees)."""


class GitHubRepoError(GitHubError):
    """Clone, fetch, or fast-forward of the host checkout failed."""


class GitHubWorktreeError(GitHubError):
    """Named factory worktree is missing or not a managed worktree."""


class GitHubDirtyWorktreeError(GitHubError):
    """Worktree has uncommitted changes; publish requires a clean tree."""


class GitHubNoCommitsError(GitHubError):
    """Branch is not strictly ahead of the remote base branch."""


class GitHubDivergedError(GitHubError):
    """Branch does not contain the current remote base branch."""


class GitHubPushError(GitHubError):
    """Push of the feature branch to origin failed."""


class GitHubPullRequestError(GitHubError):
    """Draft pull-request creation failed or returned an invalid URL."""


@dataclass(frozen=True)
class CommandResult:
    """Captured argv command result."""

    returncode: int
    stdout: str
    stderr: str
    argv: tuple[str, ...]


class CommandRunner(Protocol):
    """Injectable argv runner used by the GitHub helpers."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run ``argv`` without a shell and return captured output."""


@dataclass(frozen=True)
class RepoCheckout:
    """Local host checkout of an allowlisted GitHub repository."""

    repo: str
    path: Path
    base_branch: str


class SubprocessCommandRunner:
    """``subprocess.run`` adapter that never uses a shell."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        command = _require_argv(argv)
        logged = " ".join(sanitize_git_text(part) for part in command)
        logger.debug("argv: %s", logged)
        try:
            completed = subprocess.run(
                command,
                cwd=None if cwd is None else str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
                env=None if env is None else dict(env),
            )
        except subprocess.TimeoutExpired as exc:
            raise GitHubError(f"command timed out: {logged}") from exc
        except OSError as exc:
            detail = sanitize_git_text(str(exc))
            raise GitHubError(f"command failed to start: {logged}: {detail}") from None
        return CommandResult(
            returncode=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            argv=command,
        )


def default_repos_dir() -> Path:
    """Return ``~/.simplecode/repos``."""
    return _DEFAULT_REPOS_DIR


def validate_repo(repo: str) -> tuple[str, str]:
    """Return ``(owner, name)`` for a GitHub ``owner/repo`` path."""
    value = repo.strip()
    if (
        not _REPO_RE.fullmatch(value)
        or value.startswith("/")
        or value.endswith("/")
        or ".." in value
    ):
        raise GitHubValidationError("repository must be a GitHub owner/repo path")
    owner, name = value.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        raise GitHubValidationError("repository must be a GitHub owner/repo path")
    if name.endswith(".git"):
        raise GitHubValidationError("repository must be a GitHub owner/repo path")
    return owner, name


def validate_branch(branch: str, *, field: str = "branch") -> str:
    """Return a git branch name that cannot escape refs or inject options."""
    value = branch.strip()
    parts = value.split("/")
    invalid = (
        not value
        or not _BRANCH_RE.fullmatch(value)
        or value.startswith(("-", ".", "/"))
        or value.endswith((".", "/"))
        or ".." in value
        or "//" in value
        or any(not part or part.startswith(".") or part.endswith(".lock") for part in parts)
    )
    if invalid:
        raise GitHubValidationError(f"{field} must be a valid git branch name")
    return value


def managed_worktree_path(host_repo: Path, branch: str) -> Path:
    """Return the ``.simplecode/worktrees`` path for a named branch."""
    safe = validate_branch(branch)
    return (host_repo / _MANAGED_WORKTREE_DIR / safe.replace("/", "-")).resolve()


def parse_github_origin(url: str) -> tuple[str, str] | None:
    """Extract ``(owner, repo)`` from a remote URL. Credentials are discarded."""
    raw = url.strip()
    if not raw:
        return None
    cleaned = _strip_url_credentials(raw)
    match = re.search(
        r"(?i)github\.com[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)",
        cleaned,
    )
    if match is None:
        return None
    owner = match.group("owner")
    name = match.group("repo")
    if name.endswith(".git"):
        name = name[: -len(".git")]
    try:
        return validate_repo(f"{owner}/{name}")
    except GitHubValidationError:
        return None


def sanitize_git_text(text: str) -> str:
    """Redact tokens and URL credentials, then truncate for logs/exceptions."""
    cleaned = _TOKEN_RE.sub("<redacted>", text)
    cleaned = _HEADER_SECRET_RE.sub(r"\1: <redacted>", cleaned)
    cleaned = _redact_url_credentials(cleaned)
    cleaned = cleaned.replace("\n", " ").strip()
    if len(cleaned) > _MAX_ERROR:
        return cleaned[:_MAX_ERROR] + "…"
    return cleaned


def is_simplecode_status_line(line: str) -> bool:
    """Return True when a porcelain line is a managed ``.simplecode`` path."""
    payload = line[3:] if len(line) >= 4 else line.strip()
    paths = payload.split(" -> ")
    return bool(paths) and all(
        path == ".simplecode" or path.startswith(".simplecode/") for path in paths
    )


class GitHubRepositoryManager:
    """Clone or safely update allowlisted repositories under ``repos_dir``."""

    def __init__(
        self,
        repos_dir: Path,
        *,
        allowed_repos: Iterable[str],
        runner: CommandRunner | None = None,
    ) -> None:
        self.repos_dir = Path(repos_dir)
        self._allowed = {
            f"{owner}/{name}" for owner, name in (_validated(repo) for repo in allowed_repos)
        }
        self._runner = runner or SubprocessCommandRunner()

    def ensure_repo(self, repo: str, *, base_branch: str | None = None) -> RepoCheckout:
        """Clone into ``repos_dir/owner/repo`` or fast-forward a matching origin."""
        owner, name = self._require_allowed(repo)
        qualified = f"{owner}/{name}"
        root = self.repos_dir.expanduser().resolve()
        dest = (root / owner / name).resolve()
        if not dest.is_relative_to(root):
            raise GitHubValidationError("repository path escapes the managed clone directory")
        configured = validate_branch(base_branch, field="base_branch") if base_branch else None

        if dest.exists() and any(dest.iterdir()):
            self._assert_git_repo(dest)
            self._assert_origin(dest, qualified)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._clone(qualified, dest)
            self._assert_git_repo(dest)
            self._assert_origin(dest, qualified)

        self._assert_clean_host(dest)
        resolved_base = configured or self._default_base_branch(dest, qualified)
        self._fetch_origin(dest)
        self._checkout_base(dest, resolved_base)
        self._assert_clean_host(dest)
        self._fast_forward(dest, resolved_base)
        return RepoCheckout(repo=qualified, path=dest, base_branch=resolved_base)

    def _require_allowed(self, repo: str) -> tuple[str, str]:
        owner, name = validate_repo(repo)
        qualified = f"{owner}/{name}"
        if qualified not in self._allowed:
            raise GitHubValidationError(f"repository is not allowlisted: {qualified}")
        return owner, name

    def _clone(self, repo: str, dest: Path) -> None:
        logger.info("Cloning %s", repo)
        result = self._runner.run(
            ["gh", "repo", "clone", repo, str(dest)],
            timeout=_CLONE_TIMEOUT,
            env=_git_env(),
        )
        if result.returncode != 0:
            raise GitHubRepoError(
                f"failed to clone {repo}: {sanitize_git_text(result.stderr or result.stdout)}"
            )

    def _assert_git_repo(self, dest: Path) -> None:
        result = self._git(dest, ["rev-parse", "--is-inside-work-tree"], check=False)
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise GitHubRepoError("existing path is not a git repository")

    def _assert_origin(self, dest: Path, expected: str) -> None:
        result = self._git(dest, ["remote", "get-url", "origin"])
        parsed = parse_github_origin(result.stdout)
        if parsed is None or f"{parsed[0]}/{parsed[1]}".lower() != expected.lower():
            raise GitHubOriginMismatchError(
                f"origin does not match allowlisted repository {expected}"
            )

    def _assert_clean_host(self, dest: Path) -> None:
        result = self._git(dest, ["status", "--porcelain"])
        dirty = [
            line
            for line in result.stdout.splitlines()
            if line.strip() and not is_simplecode_status_line(line)
        ]
        if dirty:
            raise GitHubDirtyHostError("host checkout has uncommitted changes")

    def _default_base_branch(self, dest: Path, repo: str) -> str:
        symbolic = self._git(
            dest,
            ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
            check=False,
        )
        if symbolic.returncode == 0:
            ref = symbolic.stdout.strip()
            prefix = "refs/remotes/origin/"
            if ref.startswith(prefix):
                return validate_branch(ref[len(prefix) :], field="base_branch")
        view = self._runner.run(
            [
                "gh",
                "repo",
                "view",
                repo,
                "--json",
                "defaultBranchRef",
                "--jq",
                ".defaultBranchRef.name",
            ],
            cwd=dest,
            timeout=_GIT_TIMEOUT,
            env=_git_env(),
        )
        if view.returncode != 0:
            raise GitHubRepoError(
                f"could not determine default branch for {repo}: "
                f"{sanitize_git_text(view.stderr or view.stdout)}"
            )
        name = view.stdout.strip()
        if not name:
            raise GitHubRepoError(f"could not determine default branch for {repo}")
        return validate_branch(name, field="base_branch")

    def _fetch_origin(self, dest: Path) -> None:
        logger.info("Fetching origin")
        self._git(dest, ["fetch", "--prune", "--", "origin"])

    def _checkout_base(self, dest: Path, base_branch: str) -> None:
        current = self._git(dest, ["rev-parse", "--abbrev-ref", "HEAD"])
        head = current.stdout.strip()
        if head == base_branch:
            return
        switched = self._git(dest, ["switch", base_branch], check=False)
        if switched.returncode == 0:
            return
        tracked = self._git(
            dest,
            ["switch", "--track", "-c", base_branch, f"origin/{base_branch}"],
            check=False,
        )
        if tracked.returncode != 0:
            detail = sanitize_git_text(tracked.stderr or tracked.stdout)
            raise GitHubRepoError(f"could not switch to base branch {base_branch}: {detail}")

    def _fast_forward(self, dest: Path, base_branch: str) -> None:
        remote_ref = f"origin/{base_branch}"
        result = self._git(dest, ["merge", "--ff-only", "--", remote_ref], check=False)
        if result.returncode != 0:
            raise GitHubRepoError(
                f"fast-forward of {base_branch} failed: "
                f"{sanitize_git_text(result.stderr or result.stdout)}"
            )

    def _git(
        self,
        cwd: Path,
        args: Sequence[str],
        *,
        check: bool = True,
    ) -> CommandResult:
        result = self._runner.run(
            ["git", *args],
            cwd=cwd,
            timeout=_GIT_TIMEOUT,
            env=_git_env(),
        )
        if check and result.returncode != 0:
            raise GitHubRepoError(
                f"git {args[0]} failed: {sanitize_git_text(result.stderr or result.stdout)}"
            )
        return result


class GitHubPublisher:
    """Push a named worktree branch and open a draft pull request."""

    def __init__(
        self,
        *,
        allowed_repos: Iterable[str] | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        allowed = list(allowed_repos or ())
        self._allowed = (
            {f"{owner}/{name}" for owner, name in (_validated(repo) for repo in allowed)}
            if allowed
            else None
        )
        self._runner = runner or SubprocessCommandRunner()

    def publish_draft_pr(
        self,
        repo: str,
        *,
        host_repo: Path,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        worktree_path: Path | None = None,
    ) -> str:
        """Validate the worktree, push ``branch``, and create a draft PR.

        Never writes the default branch and never merges. ``title`` and ``body``
        are passed as argv operands (not interpolated into a shell).
        """
        owner, name = validate_repo(repo)
        qualified = f"{owner}/{name}"
        if self._allowed is not None and qualified not in self._allowed:
            raise GitHubValidationError(f"repository is not allowlisted: {qualified}")
        safe_branch = validate_branch(branch)
        safe_base = validate_branch(base_branch, field="base_branch")
        if safe_branch == safe_base:
            raise GitHubValidationError("refusing to publish the repository default branch")

        host = Path(host_repo).resolve()
        worktree = self._require_worktree(host, safe_branch, worktree_path)
        self._assert_branch(worktree, safe_branch)
        self._assert_clean_worktree(worktree)
        self._fetch_origin(worktree)
        self._assert_base_ancestor(worktree, safe_base)
        self._assert_ahead(worktree, safe_base)
        self._push_branch(worktree, safe_branch)
        return self._create_draft_pr(
            qualified,
            base_branch=safe_base,
            head=safe_branch,
            title=title,
            body=body,
            cwd=worktree,
        )

    def _require_worktree(
        self,
        host_repo: Path,
        branch: str,
        worktree_path: Path | None,
    ) -> Path:
        managed_root = (host_repo / _MANAGED_WORKTREE_DIR).resolve()
        expected = managed_worktree_path(host_repo, branch)
        path = expected if worktree_path is None else Path(worktree_path).resolve()
        if not path.is_dir():
            raise GitHubWorktreeError(f"managed worktree does not exist for branch {branch}")
        try:
            path.relative_to(managed_root)
        except ValueError:
            raise GitHubWorktreeError("worktree is not a .simplecode managed worktree") from None
        if path != expected:
            raise GitHubWorktreeError(
                f"worktree path does not match the managed path for branch {branch}"
            )
        return path

    def _assert_branch(self, worktree: Path, branch: str) -> None:
        result = self._git(worktree, ["rev-parse", "--abbrev-ref", "HEAD"])
        current = result.stdout.strip()
        if current != branch:
            raise GitHubWorktreeError(
                f"worktree branch {current!r} does not match expected {branch!r}"
            )

    def _assert_clean_worktree(self, worktree: Path) -> None:
        result = self._git(worktree, ["status", "--porcelain"])
        dirty = [line for line in result.stdout.splitlines() if line.strip()]
        if dirty:
            raise GitHubDirtyWorktreeError("worktree has uncommitted changes")

    def _fetch_origin(self, cwd: Path) -> None:
        self._git(cwd, ["fetch", "--prune", "--", "origin"])

    def _assert_ahead(self, worktree: Path, base_branch: str) -> None:
        remote_ref = f"origin/{base_branch}"
        result = self._git(
            worktree,
            ["rev-list", "--count", f"{remote_ref}..HEAD"],
            check=False,
        )
        if result.returncode != 0:
            raise GitHubNoCommitsError(
                f"could not compare HEAD to {remote_ref}: "
                f"{sanitize_git_text(result.stderr or result.stdout)}"
            )
        try:
            ahead = int(result.stdout.strip())
        except ValueError:
            raise GitHubNoCommitsError(
                "could not determine commits ahead of the base branch"
            ) from None
        if ahead < 1:
            raise GitHubNoCommitsError("branch has no commits ahead of the base branch")

    def _assert_base_ancestor(self, worktree: Path, base_branch: str) -> None:
        remote_ref = f"origin/{base_branch}"
        result = self._git(
            worktree,
            ["merge-base", "--is-ancestor", remote_ref, "HEAD"],
            check=False,
        )
        if result.returncode != 0:
            raise GitHubDivergedError(
                f"branch does not contain the current base branch {remote_ref}"
            )

    def _push_branch(self, worktree: Path, branch: str) -> None:
        logger.info("Pushing branch %s", branch)
        result = self._git(
            worktree,
            ["push", "--set-upstream", "origin", f"refs/heads/{branch}:refs/heads/{branch}"],
            check=False,
        )
        if result.returncode != 0:
            raise GitHubPushError(
                f"push failed: {sanitize_git_text(result.stderr or result.stdout)}"
            )

    def _create_draft_pr(
        self,
        repo: str,
        *,
        base_branch: str,
        head: str,
        title: str,
        body: str,
        cwd: Path,
    ) -> str:
        argv = [
            "gh",
            "pr",
            "create",
            "--draft",
            "--repo",
            repo,
            "--base",
            base_branch,
            "--head",
            head,
            "--title",
            title,
            "--body",
            body,
        ]
        logger.info("Creating draft pull request for %s", repo)
        existing = self._find_existing_pr(repo, head=head, cwd=cwd)
        if existing is not None:
            return existing
        result = self._runner.run(argv, cwd=cwd, timeout=_PR_TIMEOUT, env=_git_env())
        if result.returncode != 0:
            existing = self._find_existing_pr(repo, head=head, cwd=cwd)
            if existing is not None:
                return existing
            raise GitHubPullRequestError(
                f"draft pull request failed: {sanitize_git_text(result.stderr or result.stdout)}"
            )
        url = _extract_pr_url(result.stdout, expected_repo=repo)
        if url is None:
            raise GitHubPullRequestError(
                "gh pr create did not return a valid GitHub pull request URL"
            )
        return url

    def _find_existing_pr(self, repo: str, *, head: str, cwd: Path) -> str | None:
        result = self._runner.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--head",
                head,
                "--state",
                "open",
                "--json",
                "url",
                "--jq",
                ".[0].url",
            ],
            cwd=cwd,
            timeout=_PR_TIMEOUT,
            env=_git_env(),
        )
        if result.returncode != 0:
            return None
        return _extract_pr_url(result.stdout, expected_repo=repo)

    def _git(
        self,
        cwd: Path,
        args: Sequence[str],
        *,
        check: bool = True,
    ) -> CommandResult:
        result = self._runner.run(
            ["git", *args],
            cwd=cwd,
            timeout=_GIT_TIMEOUT,
            env=_git_env(),
        )
        if check and result.returncode != 0:
            raise GitHubRepoError(
                f"git {args[0]} failed: {sanitize_git_text(result.stderr or result.stdout)}"
            )
        return result


def _validated(repo: str) -> tuple[str, str]:
    return validate_repo(repo)


def _require_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, str) or not argv:
        raise GitHubError("command must be a nonempty argv list")
    command = tuple(str(part) for part in argv)
    if any(part == "" for part in command):
        raise GitHubError("command argv must not contain empty arguments")
    return command


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    return env


def _strip_url_credentials(url: str) -> str:
    if "://" in url:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunparse(parsed._replace(netloc=host, query="", fragment=""))
    if "@" in url and "github.com" in url:
        return url.split("@", 1)[1]
    return url


def _redact_url_credentials(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return f"{match.group(1)}://<redacted>@"

    return re.sub(r"(https?://)[^/\s]+@", replace, text)


def _extract_pr_url(stdout: str, *, expected_repo: str) -> str | None:
    owner, name = validate_repo(expected_repo)
    for raw in reversed(stdout.splitlines()):
        candidate = raw.strip()
        match = _PR_URL_RE.fullmatch(candidate)
        if match is None:
            continue
        if (
            match.group("owner").lower() == owner.lower()
            and match.group("repo").lower() == name.lower()
        ):
            return candidate
    return None
