"""Tests for the GitHub repository manager and draft-PR publisher.

A fake argv runner stands in for git/gh. No network and no real ``gh``.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from simplecode.github import (
    CommandResult,
    GitHubDirtyHostError,
    GitHubDirtyWorktreeError,
    GitHubDivergedError,
    GitHubNoCommitsError,
    GitHubOriginMismatchError,
    GitHubPublisher,
    GitHubPullRequestError,
    GitHubPushError,
    GitHubRepoError,
    GitHubRepositoryManager,
    GitHubValidationError,
    GitHubWorktreeError,
    SubprocessCommandRunner,
    is_simplecode_status_line,
    parse_github_origin,
    sanitize_git_text,
    validate_branch,
    validate_repo,
)

REPO = "acme/api"
BRANCH = "simplecode/eng-42-add-login"


class GitHarness:
    """Scripted git/gh runner that records argv and never touches a network."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.origin_url = "https://github.com/acme/api.git"
        self.host_head = "main"
        self.host_porcelain = ""
        self.symbolic_ref = "refs/remotes/origin/main"
        self.symbolic_rc = 0
        self.default_branch = "main"
        self.ahead = 2
        self.worktree_head = BRANCH
        self.worktree_porcelain = ""
        self.inside_work_tree = True
        self.clone_rc = 0
        self.fetch_rc = 0
        self.checkout_rc = 0
        self.track_rc = 0
        self.ff_rc = 0
        self.push_rc = 0
        self.pr_rc = 0
        self.view_rc = 0
        self.rev_list_rc = 0
        self.ancestor_rc = 0
        self.pr_stdout = "https://github.com/acme/api/pull/12\n"
        self.existing_pr_stdout = ""
        self.pr_stderr = ""
        self.clone_stderr = ""
        self.push_stderr = ""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        assert not isinstance(argv, str)
        return self.run_with_cwd(argv, cwd=cwd, timeout=timeout, env=env)

    def argv_programs(self) -> list[str]:
        return [call[0][0] for call in self.calls]

    def all_argv(self) -> list[tuple[str, ...]]:
        return [call[0] for call in self.calls]

    def _dispatch(self, cmd: tuple[str, ...]) -> int:
        if cmd[:3] == ("gh", "repo", "clone"):
            return self.clone_rc
        if cmd[:3] == ("gh", "repo", "view"):
            return self.view_rc
        if cmd[:3] == ("gh", "pr", "create"):
            return self.pr_rc
        if cmd[:3] == ("gh", "pr", "list"):
            return 0
        git = cmd[1:]
        if git[:2] == ("rev-parse", "--is-inside-work-tree"):
            return 0 if self.inside_work_tree else 1
        if git[:1] == ("symbolic-ref",):
            return self.symbolic_rc
        if git[:1] == ("fetch",):
            return self.fetch_rc
        if git[:1] == ("switch",):
            result = self.track_rc if "--track" in git else self.checkout_rc
            if result == 0:
                self.host_head = git[-1]
            return result
        if git[:2] == ("merge", "--ff-only"):
            return self.ff_rc
        if git[:2] == ("rev-list", "--count"):
            return self.rev_list_rc
        if git[:2] == ("merge-base", "--is-ancestor"):
            return self.ancestor_rc
        if git[:1] == ("push",):
            return self.push_rc
        return 0

    def _stdout(self, cmd: tuple[str, ...]) -> str:
        git = cmd[1:] if cmd and cmd[0] == "git" else ()
        if cmd[:3] == ("gh", "repo", "view"):
            return f"{self.default_branch}\n"
        if cmd[:3] == ("gh", "pr", "create"):
            return self.pr_stdout
        if cmd[:3] == ("gh", "pr", "list"):
            return self.existing_pr_stdout
        if git[:2] == ("rev-parse", "--is-inside-work-tree"):
            return "true\n" if self.inside_work_tree else "false\n"
        if git[:3] == ("remote", "get-url", "origin"):
            return f"{self.origin_url}\n"
        if git[:2] == ("status", "--porcelain"):
            return self.worktree_porcelain if ".simplecode" in "".join(cmd) else self.host_porcelain
        if git[:1] == ("symbolic-ref",):
            return f"{self.symbolic_ref}\n"
        if git == ("rev-parse", "--abbrev-ref", "HEAD"):
            # HEAD is read from host or worktree via cwd, not argv.
            return ""
        if git[:2] == ("rev-list", "--count"):
            return f"{self.ahead}\n"
        return ""

    def _stderr(self, cmd: tuple[str, ...]) -> str:
        if cmd[:3] == ("gh", "repo", "clone"):
            return self.clone_stderr
        if cmd[:3] == ("gh", "pr", "create"):
            return self.pr_stderr
        if cmd[:2] == ("git", "push"):
            return self.push_stderr
        return ""

    def run_with_cwd(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Same as run, but HEAD/status follow cwd (host vs worktree)."""
        assert not isinstance(argv, str)
        cmd = tuple(str(part) for part in argv)
        self.calls.append((cmd, None if cwd is None else str(cwd)))
        cwd_path = None if cwd is None else Path(cwd)
        in_worktree = cwd_path is not None and "worktrees" in cwd_path.parts
        code = self._dispatch(cmd)
        stdout = self._stdout_for_cwd(cmd, in_worktree)
        return CommandResult(code, stdout, self._stderr(cmd), cmd)

    def _stdout_for_cwd(self, cmd: tuple[str, ...], in_worktree: bool) -> str:
        git = cmd[1:] if cmd and cmd[0] == "git" else ()
        if git[:2] == ("status", "--porcelain"):
            return self.worktree_porcelain if in_worktree else self.host_porcelain
        if git == ("rev-parse", "--abbrev-ref", "HEAD"):
            return f"{self.worktree_head if in_worktree else self.host_head}\n"
        return self._stdout(cmd)


def _manager(tmp_path: Path, harness: GitHarness) -> GitHubRepositoryManager:
    return GitHubRepositoryManager(
        tmp_path / "repos",
        allowed_repos=[REPO],
        runner=harness,
    )


def _publisher(harness: GitHarness) -> GitHubPublisher:
    harness.run = harness.run_with_cwd  # type: ignore[method-assign]
    return GitHubPublisher(allowed_repos=[REPO], runner=harness)


def _existing_checkout(tmp_path: Path) -> Path:
    dest = tmp_path / "repos" / "acme" / "api"
    dest.mkdir(parents=True)
    (dest / "README.md").write_text("hello\n")
    return dest


def _worktree(tmp_path: Path) -> tuple[Path, Path]:
    host = tmp_path / "host"
    worktree = host / ".simplecode" / "worktrees" / BRANCH.replace("/", "-")
    worktree.mkdir(parents=True)
    return host, worktree


def test_validate_repo_accepts_owner_repo() -> None:
    assert validate_repo("acme/api") == ("acme", "api")


@pytest.mark.parametrize(
    "repo",
    ["acme", "acme/api/extra", "../etc/passwd", "acme/..", "/acme/api", "acme/api.git", ""],
)
def test_validate_repo_rejects_unsafe_names(repo: str) -> None:
    with pytest.raises(GitHubValidationError):
        validate_repo(repo)


@pytest.mark.parametrize(
    "branch",
    ["", "-option", ".hidden", "main/", "release.lock", "feat//double", "feat/../escape"],
)
def test_validate_branch_rejects_invalid_refs(branch: str) -> None:
    with pytest.raises(GitHubValidationError):
        validate_branch(branch)


def test_parse_github_origin_strips_credentials() -> None:
    assert parse_github_origin("https://x-access-token:ghs_SECRET@github.com/acme/api.git") == (
        "acme",
        "api",
    )
    assert parse_github_origin("git@github.com:acme/api.git") == ("acme", "api")
    assert parse_github_origin("ssh://git@github.com/acme/api.git") == ("acme", "api")
    assert parse_github_origin("org-123@github.com:acme/api.git") == ("acme", "api")


def test_sanitize_git_text_redacts_tokens_and_url_passwords() -> None:
    text = sanitize_git_text("clone https://user:ghs_SECRETHERE@github.com/acme/api.git ghp_TOKEN1")
    assert "SECRETHERE" not in text
    assert "TOKEN1" not in text
    assert "<redacted>" in text


def test_simplecode_status_lines_are_ignored() -> None:
    assert is_simplecode_status_line("?? .simplecode/")
    assert is_simplecode_status_line("?? .simplecode/worktrees/foo")
    assert is_simplecode_status_line("?? .simplecode")
    assert not is_simplecode_status_line(" M README.md")
    assert not is_simplecode_status_line(" M src/.simplecode/config")


def test_clone_uses_argv_and_owner_repo_layout(tmp_path: Path) -> None:
    harness = GitHarness()
    manager = _manager(tmp_path, harness)

    checkout = manager.ensure_repo(REPO, base_branch="main")

    assert checkout.repo == REPO
    assert checkout.path == (tmp_path / "repos" / "acme" / "api").resolve()
    assert checkout.base_branch == "main"
    clone = next(argv for argv in harness.all_argv() if argv[:3] == ("gh", "repo", "clone"))
    assert clone == ("gh", "repo", "clone", REPO, str(checkout.path))
    assert all(isinstance(argv, tuple) for argv in harness.all_argv())
    assert "sh" not in harness.argv_programs()
    assert "-c" not in [part for argv in harness.all_argv() for part in argv]


def test_existing_matching_origin_fetches_and_fast_forwards(tmp_path: Path) -> None:
    dest = _existing_checkout(tmp_path)
    harness = GitHarness()
    manager = GitHubRepositoryManager(
        tmp_path / "repos",
        allowed_repos=[REPO],
        runner=harness,
    )

    checkout = manager.ensure_repo(REPO, base_branch="main")

    assert checkout.path == dest.resolve()
    assert not any(argv[:3] == ("gh", "repo", "clone") for argv in harness.all_argv())
    assert ("git", "fetch", "--prune", "--", "origin") in harness.all_argv()
    assert ("git", "merge", "--ff-only", "--", "origin/main") in harness.all_argv()


def test_configured_base_branch_skips_remote_default_lookup(tmp_path: Path) -> None:
    _existing_checkout(tmp_path)
    harness = GitHarness()
    harness.host_head = "main"
    manager = GitHubRepositoryManager(
        tmp_path / "repos",
        allowed_repos=[REPO],
        runner=harness,
    )

    checkout = manager.ensure_repo(REPO, base_branch="develop")

    assert checkout.base_branch == "develop"
    assert not any(argv[:3] == ("gh", "repo", "view") for argv in harness.all_argv())
    assert ("git", "switch", "develop") in harness.all_argv()
    assert ("git", "merge", "--ff-only", "--", "origin/develop") in harness.all_argv()


def test_missing_local_base_branch_tracks_remote(tmp_path: Path) -> None:
    _existing_checkout(tmp_path)
    harness = GitHarness()
    harness.checkout_rc = 1
    manager = _manager(tmp_path, harness)

    checkout = manager.ensure_repo(REPO, base_branch="develop")

    assert checkout.base_branch == "develop"
    assert (
        "git",
        "switch",
        "--track",
        "-c",
        "develop",
        "origin/develop",
    ) in harness.all_argv()


def test_remote_default_branch_from_origin_head(tmp_path: Path) -> None:
    _existing_checkout(tmp_path)
    harness = GitHarness()
    harness.symbolic_ref = "refs/remotes/origin/trunk"
    manager = _manager(tmp_path, harness)

    checkout = manager.ensure_repo(REPO)

    assert checkout.base_branch == "trunk"
    assert ("git", "switch", "trunk") in harness.all_argv()


def test_remote_default_branch_falls_back_to_gh(tmp_path: Path) -> None:
    _existing_checkout(tmp_path)
    harness = GitHarness()
    harness.symbolic_rc = 1
    harness.default_branch = "release"
    manager = _manager(tmp_path, harness)

    checkout = manager.ensure_repo(REPO)

    assert checkout.base_branch == "release"
    view = next(argv for argv in harness.all_argv() if argv[:3] == ("gh", "repo", "view"))
    assert view[:6] == ("gh", "repo", "view", REPO, "--json", "defaultBranchRef")
    assert "--jq" in view


def test_origin_mismatch_rejected(tmp_path: Path) -> None:
    _existing_checkout(tmp_path)
    harness = GitHarness()
    harness.origin_url = "https://github.com/other/repo.git"
    manager = _manager(tmp_path, harness)

    with pytest.raises(GitHubOriginMismatchError, match="does not match"):
        manager.ensure_repo(REPO, base_branch="main")


def test_origin_with_embedded_credentials_matches_and_is_not_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _existing_checkout(tmp_path)
    harness = GitHarness()
    harness.origin_url = "https://x-access-token:ghs_SUPERSECRET@github.com/acme/api.git"
    manager = _manager(tmp_path, harness)

    with caplog.at_level(logging.DEBUG, logger="simplecode.github"):
        checkout = manager.ensure_repo(REPO, base_branch="main")

    assert checkout.repo == REPO
    assert "ghs_SUPERSECRET" not in caplog.text
    assert all("ghs_SUPERSECRET" not in " ".join(argv) for argv in harness.all_argv())


def test_origin_match_is_case_insensitive(tmp_path: Path) -> None:
    _existing_checkout(tmp_path)
    harness = GitHarness()
    harness.origin_url = "https://github.com/Acme/API.git"
    manager = _manager(tmp_path, harness)

    checkout = manager.ensure_repo(REPO, base_branch="main")

    assert checkout.repo == REPO


def test_dirty_host_rejected(tmp_path: Path) -> None:
    _existing_checkout(tmp_path)
    harness = GitHarness()
    harness.host_porcelain = " M README.md\n"
    manager = _manager(tmp_path, harness)

    with pytest.raises(GitHubDirtyHostError, match="uncommitted"):
        manager.ensure_repo(REPO, base_branch="main")


def test_simplecode_worktrees_do_not_count_as_host_dirt(tmp_path: Path) -> None:
    _existing_checkout(tmp_path)
    harness = GitHarness()
    harness.host_porcelain = "?? .simplecode/\n?? .simplecode/worktrees/foo\n"
    manager = _manager(tmp_path, harness)

    checkout = manager.ensure_repo(REPO, base_branch="main")
    assert checkout.base_branch == "main"


def test_unallowlisted_repo_rejected(tmp_path: Path) -> None:
    harness = GitHarness()
    manager = _manager(tmp_path, harness)
    with pytest.raises(GitHubValidationError, match="not allowlisted"):
        manager.ensure_repo("evil/repo", base_branch="main")


def test_clone_path_cannot_escape_through_owner_symlink(tmp_path: Path) -> None:
    repos = tmp_path / "repos"
    repos.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repos / "acme").symlink_to(outside, target_is_directory=True)
    harness = GitHarness()
    manager = GitHubRepositoryManager(repos, allowed_repos=[REPO], runner=harness)

    with pytest.raises(GitHubValidationError, match="escapes"):
        manager.ensure_repo(REPO, base_branch="main")

    assert harness.calls == []


def test_fast_forward_failure(tmp_path: Path) -> None:
    _existing_checkout(tmp_path)
    harness = GitHarness()
    harness.ff_rc = 1
    manager = _manager(tmp_path, harness)
    with pytest.raises(GitHubRepoError, match="fast-forward"):
        manager.ensure_repo(REPO, base_branch="main")


def test_publish_draft_pr_success(tmp_path: Path) -> None:
    host, worktree = _worktree(tmp_path)
    harness = GitHarness()
    publisher = _publisher(harness)

    url = publisher.publish_draft_pr(
        REPO,
        host_repo=host,
        branch=BRANCH,
        base_branch="main",
        title="ENG-42: Add login",
        body="Factory body",
        worktree_path=worktree,
    )

    assert url == "https://github.com/acme/api/pull/12"
    push = next(argv for argv in harness.all_argv() if argv[:2] == ("git", "push"))
    assert push == (
        "git",
        "push",
        "--set-upstream",
        "origin",
        f"refs/heads/{BRANCH}:refs/heads/{BRANCH}",
    )
    create = next(argv for argv in harness.all_argv() if argv[:3] == ("gh", "pr", "create"))
    assert create[:4] == ("gh", "pr", "create", "--draft")
    assert "--repo" in create and REPO in create
    assert "--base" in create and "main" in create
    assert "--head" in create and BRANCH in create
    assert "--title" in create and "ENG-42: Add login" in create
    assert "--body" in create and "Factory body" in create
    assert "--fill" not in create
    assert not any("merge" in argv for argv in harness.all_argv())
    assert not any(argv[:3] == ("gh", "pr", "merge") for argv in harness.all_argv())
    pushed_default = any(
        part == "main" and argv[:2] == ("git", "push")
        for argv in harness.all_argv()
        for part in argv
    )
    assert not pushed_default


def test_publish_missing_worktree(tmp_path: Path) -> None:
    host = tmp_path / "host"
    host.mkdir()
    harness = GitHarness()
    publisher = _publisher(harness)
    with pytest.raises(GitHubWorktreeError, match="does not exist"):
        publisher.publish_draft_pr(
            REPO,
            host_repo=host,
            branch=BRANCH,
            base_branch="main",
            title="t",
            body="b",
        )


def test_publish_rejects_unmanaged_worktree(tmp_path: Path) -> None:
    host = tmp_path / "host"
    other = tmp_path / "other-wt"
    other.mkdir(parents=True)
    host.mkdir()
    harness = GitHarness()
    publisher = _publisher(harness)
    with pytest.raises(GitHubWorktreeError, match="managed"):
        publisher.publish_draft_pr(
            REPO,
            host_repo=host,
            branch=BRANCH,
            base_branch="main",
            title="t",
            body="b",
            worktree_path=other,
        )


def test_publish_rejects_a_different_managed_worktree(tmp_path: Path) -> None:
    host, _worktree_path = _worktree(tmp_path)
    other = host / ".simplecode" / "worktrees" / "another-branch"
    other.mkdir(parents=True)
    harness = GitHarness()
    publisher = _publisher(harness)
    with pytest.raises(GitHubWorktreeError, match="does not match"):
        publisher.publish_draft_pr(
            REPO,
            host_repo=host,
            branch=BRANCH,
            base_branch="main",
            title="t",
            body="b",
            worktree_path=other,
        )


def test_publish_branch_mismatch(tmp_path: Path) -> None:
    host, worktree = _worktree(tmp_path)
    harness = GitHarness()
    harness.worktree_head = "wrong-branch"
    publisher = _publisher(harness)
    with pytest.raises(GitHubWorktreeError, match="does not match"):
        publisher.publish_draft_pr(
            REPO,
            host_repo=host,
            branch=BRANCH,
            base_branch="main",
            title="t",
            body="b",
            worktree_path=worktree,
        )


def test_publish_dirty_worktree(tmp_path: Path) -> None:
    host, worktree = _worktree(tmp_path)
    harness = GitHarness()
    harness.worktree_porcelain = " M src/app.py\n"
    publisher = _publisher(harness)
    with pytest.raises(GitHubDirtyWorktreeError, match="uncommitted"):
        publisher.publish_draft_pr(
            REPO,
            host_repo=host,
            branch=BRANCH,
            base_branch="main",
            title="t",
            body="b",
            worktree_path=worktree,
        )


def test_publish_no_commits_ahead(tmp_path: Path) -> None:
    host, worktree = _worktree(tmp_path)
    harness = GitHarness()
    harness.ahead = 0
    publisher = _publisher(harness)
    with pytest.raises(GitHubNoCommitsError, match="no commits ahead"):
        publisher.publish_draft_pr(
            REPO,
            host_repo=host,
            branch=BRANCH,
            base_branch="main",
            title="t",
            body="b",
            worktree_path=worktree,
        )


def test_publish_rejects_branch_diverged_from_base(tmp_path: Path) -> None:
    host, worktree = _worktree(tmp_path)
    harness = GitHarness()
    harness.ancestor_rc = 1
    publisher = _publisher(harness)
    with pytest.raises(GitHubDivergedError, match="current base"):
        publisher.publish_draft_pr(
            REPO,
            host_repo=host,
            branch=BRANCH,
            base_branch="main",
            title="t",
            body="b",
            worktree_path=worktree,
        )
    assert not any(argv[:2] == ("git", "push") for argv in harness.all_argv())


def test_publish_push_failure_does_not_create_pr(tmp_path: Path) -> None:
    host, worktree = _worktree(tmp_path)
    harness = GitHarness()
    harness.push_rc = 1
    harness.push_stderr = "remote rejected ghp_PUSHSECRET"
    publisher = _publisher(harness)
    with pytest.raises(GitHubPushError, match="push failed") as exc_info:
        publisher.publish_draft_pr(
            REPO,
            host_repo=host,
            branch=BRANCH,
            base_branch="main",
            title="t",
            body="b",
            worktree_path=worktree,
        )
    assert "PUSHSECRET" not in str(exc_info.value)
    assert not any(argv[:3] == ("gh", "pr", "create") for argv in harness.all_argv())


def test_publish_pr_failure(tmp_path: Path) -> None:
    host, worktree = _worktree(tmp_path)
    harness = GitHarness()
    harness.pr_rc = 1
    harness.pr_stderr = "GraphQL: https://user:ghs_PRSECRET@github.com/acme/api"
    publisher = _publisher(harness)
    with pytest.raises(GitHubPullRequestError) as exc_info:
        publisher.publish_draft_pr(
            REPO,
            host_repo=host,
            branch=BRANCH,
            base_branch="main",
            title="t",
            body="b",
            worktree_path=worktree,
        )
    assert "PRSECRET" not in str(exc_info.value)


def test_publish_returns_existing_pr_without_creating_another(tmp_path: Path) -> None:
    host, worktree = _worktree(tmp_path)
    harness = GitHarness()
    harness.existing_pr_stdout = "https://github.com/Acme/API/pull/7\n"
    publisher = _publisher(harness)

    url = publisher.publish_draft_pr(
        REPO,
        host_repo=host,
        branch=BRANCH,
        base_branch="main",
        title="t",
        body="b",
        worktree_path=worktree,
    )

    assert url == "https://github.com/Acme/API/pull/7"
    assert not any(argv[:3] == ("gh", "pr", "create") for argv in harness.all_argv())


def test_publish_rejects_invalid_pr_url(tmp_path: Path) -> None:
    host, worktree = _worktree(tmp_path)
    harness = GitHarness()
    harness.pr_stdout = "created but no url\n"
    publisher = _publisher(harness)
    with pytest.raises(GitHubPullRequestError, match="valid GitHub pull request URL"):
        publisher.publish_draft_pr(
            REPO,
            host_repo=host,
            branch=BRANCH,
            base_branch="main",
            title="t",
            body="b",
            worktree_path=worktree,
        )


def test_publish_rejects_default_branch(tmp_path: Path) -> None:
    host, worktree = _worktree(tmp_path)
    harness = GitHarness()
    publisher = _publisher(harness)
    with pytest.raises(GitHubValidationError, match="default branch"):
        publisher.publish_draft_pr(
            REPO,
            host_repo=host,
            branch="main",
            base_branch="main",
            title="t",
            body="b",
            worktree_path=worktree,
        )


def test_command_runner_rejects_shell_strings() -> None:
    runner = SubprocessCommandRunner()
    with pytest.raises(Exception, match="argv"):
        runner.run("git status")  # type: ignore[arg-type]


def test_subprocess_runner_never_sets_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        argv: list[str] | tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(list(argv), 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = SubprocessCommandRunner().run(["git", "status"], cwd="/tmp")
    assert result.returncode == 0
    assert captured["shell"] is False
    assert captured["argv"] == ("git", "status")
    assert isinstance(captured["argv"], tuple)
