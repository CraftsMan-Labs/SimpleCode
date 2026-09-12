"""Tests for the webhook server and handler."""

from __future__ import annotations

import hashlib
import hmac
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from simplecode.webhook import (
    _MAX_WEBHOOK_BODY,
    Poller,
    WebhookConfig,
    WebhookHandler,
    _ensure_repo,
    _find_session_id,
    _make_request_handler,
    _verify_signature,
)


def _make_config(**overrides) -> WebhookConfig:
    defaults = {
        "pipeline_map": {
            "triage": "examples/github-issue-triage",
            "feature": "examples/github-issue-feature-creator",
        },
        "default_pipeline": "examples/github-issue-feature-creator",
        "github_user": "testuser",
    }
    defaults.update(overrides)
    return WebhookConfig(**defaults)


def _make_poller(repos: list[str] | None = None, **config_overrides) -> Poller:
    config = _make_config(**config_overrides)
    return Poller(config, repos or ["org/repo"], WebhookHandler(config))


class TestParseCommand:
    def test_stop(self) -> None:
        cmd, text = WebhookHandler._parse_command("/stop")
        assert cmd == "stop"
        assert text is None

    def test_stop_with_trailing(self) -> None:
        cmd, text = WebhookHandler._parse_command("/stop the pipeline")
        assert cmd == "stop"
        assert text is None

    def test_help_at_mention(self) -> None:
        cmd, text = WebhookHandler._parse_command("@simplecode --help")
        assert cmd == "help"

    def test_list_at_mention(self) -> None:
        cmd, text = WebhookHandler._parse_command("@simplecode --list")
        assert cmd == "help"

    def test_at_mention_bare(self) -> None:
        cmd, text = WebhookHandler._parse_command("@simplecode")
        assert cmd == "help"

    def test_trigger_keyword(self) -> None:
        cmd, text = WebhookHandler._parse_command("@simplecode triage")
        assert cmd == "trigger"
        assert text == "triage"

    def test_trigger_keyword_case_insensitive(self) -> None:
        cmd, text = WebhookHandler._parse_command("@simplecode Triage")
        assert cmd == "trigger"
        assert text == "triage"

    def test_trigger_keyword_extra_text_ignored(self) -> None:
        cmd, text = WebhookHandler._parse_command("@simplecode feature please handle this")
        assert cmd == "trigger"
        assert text == "feature"

    def test_reply_is_default(self) -> None:
        cmd, text = WebhookHandler._parse_command("looks good but add more tests")
        assert cmd == "reply"
        assert text == "looks good but add more tests"

    def test_reply_multiline(self) -> None:
        cmd, text = WebhookHandler._parse_command("line one\nline two\nline three")
        assert cmd == "reply"
        assert "line one" in text
        assert "line three" in text

    def test_approve_command(self) -> None:
        cmd, text = WebhookHandler._parse_command("/approve")
        assert cmd == "approve"
        assert text is None

    def test_continue_command(self) -> None:
        cmd, text = WebhookHandler._parse_command("/continue")
        assert cmd == "approve"
        assert text is None

    def test_empty_returns_none(self) -> None:
        cmd, text = WebhookHandler._parse_command("")
        assert cmd is None
        assert text is None


class TestVerifySignature:
    def test_valid_signature(self) -> None:
        secret = "mysecret"
        body = b'{"action":"labeled"}'
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_signature(body, sig, secret) is True

    def test_invalid_signature(self) -> None:
        assert _verify_signature(b"body", "sha256=bad", "secret") is False

    def test_empty_signature(self) -> None:
        assert _verify_signature(b"body", "", "secret") is False


class TestHandleIssues:
    def test_assigned_triggers_default_pipeline(self) -> None:
        config = _make_config()
        handler = WebhookHandler(config)

        payload = {
            "action": "assigned",
            "assignee": {"login": "testuser"},
            "issue": {"number": 10, "html_url": "https://github.com/org/repo/issues/10"},
            "repository": {"full_name": "org/repo"},
        }

        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            handler._handle_issues(payload)
            mock_thread.assert_called_once()
            call_kwargs = mock_thread.call_args
            assert call_kwargs.kwargs["args"][2] == "examples/github-issue-feature-creator"

    def test_assigned_wrong_user_ignored(self) -> None:
        config = _make_config()
        handler = WebhookHandler(config)

        payload = {
            "action": "assigned",
            "assignee": {"login": "someoneelse"},
            "issue": {"number": 10, "html_url": "https://github.com/org/repo/issues/10"},
            "repository": {"full_name": "org/repo"},
        }

        with patch("threading.Thread") as mock_thread:
            handler._handle_issues(payload)
            mock_thread.assert_not_called()

    def test_assigned_no_default_pipeline_ignored(self) -> None:
        config = _make_config(default_pipeline=None)
        handler = WebhookHandler(config)

        payload = {
            "action": "assigned",
            "assignee": {"login": "testuser"},
            "issue": {"number": 10, "html_url": "https://github.com/org/repo/issues/10"},
            "repository": {"full_name": "org/repo"},
        }

        with patch("threading.Thread") as mock_thread:
            handler._handle_issues(payload)
            mock_thread.assert_not_called()

    def test_labeled_event_ignored(self) -> None:
        """Labels no longer trigger pipelines."""
        config = _make_config()
        handler = WebhookHandler(config)

        payload = {
            "action": "labeled",
            "label": {"name": "simplecode:triage"},
            "issue": {"number": 42, "html_url": "https://github.com/org/repo/issues/42"},
            "repository": {"full_name": "org/repo"},
        }

        with patch("threading.Thread") as mock_thread:
            handler._handle_issues(payload)
            mock_thread.assert_not_called()


class TestHandleIssueComment:
    def test_at_mention_triggers_pipeline(self) -> None:
        config = _make_config()
        handler = WebhookHandler(config)

        payload = {
            "action": "created",
            "comment": {
                "body": "@simplecode triage",
                "user": {"login": "otheruser"},
            },
            "issue": {
                "number": 42,
                "html_url": "https://github.com/org/repo/issues/42",
                "labels": [],
            },
            "repository": {"full_name": "org/repo"},
        }

        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            handler._handle_issue_comment(payload)
            mock_thread.assert_called_once()
            call_kwargs = mock_thread.call_args
            assert call_kwargs.kwargs["target"] == handler._start_pipeline
            assert call_kwargs.kwargs["args"][2] == "examples/github-issue-triage"

    def test_at_mention_unknown_keyword_posts_error(self) -> None:
        config = _make_config()
        handler = WebhookHandler(config)

        payload = {
            "action": "created",
            "comment": {
                "body": "@simplecode banana",
                "user": {"login": "otheruser"},
            },
            "issue": {
                "number": 42,
                "html_url": "https://github.com/org/repo/issues/42",
                "labels": [],
            },
            "repository": {"full_name": "org/repo"},
        }

        with patch("simplecode.webhook._post_comment") as mock_post:
            handler._handle_issue_comment(payload)
            mock_post.assert_called_once()
            body = mock_post.call_args.args[2]
            assert "Unknown keyword" in body
            assert "banana" in body
            assert "feature" in body
            assert "triage" in body

    def test_reply_triggers_resume(self) -> None:
        config = _make_config()
        handler = WebhookHandler(config)

        payload = {
            "action": "created",
            "comment": {
                "body": "Looks good, also handle the edge case in parse()",
                "user": {"login": "otheruser"},
            },
            "issue": {
                "number": 42,
                "labels": [{"name": "simplecode:waiting"}],
            },
            "repository": {"full_name": "org/repo"},
        }

        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            handler._handle_issue_comment(payload)
            mock_thread.assert_called_once()
            call_kwargs = mock_thread.call_args
            assert call_kwargs.kwargs["args"][2] == "reply"
            assert "edge case" in call_kwargs.kwargs["args"][3]

    def test_stop_triggers_reject(self) -> None:
        config = _make_config()
        handler = WebhookHandler(config)

        payload = {
            "action": "created",
            "comment": {
                "body": "/stop",
                "user": {"login": "otheruser"},
            },
            "issue": {
                "number": 42,
                "labels": [{"name": "simplecode:waiting"}],
            },
            "repository": {"full_name": "org/repo"},
        }

        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            handler._handle_issue_comment(payload)
            mock_thread.assert_called_once()
            call_kwargs = mock_thread.call_args
            assert call_kwargs.kwargs["args"][2] == "stop"

    def test_help_triggers_agents_list(self) -> None:
        config = _make_config()
        handler = WebhookHandler(config)

        payload = {
            "action": "created",
            "comment": {
                "body": "@simplecode --help",
                "user": {"login": "otheruser"},
            },
            "issue": {
                "number": 42,
                "labels": [{"name": "simplecode:waiting"}],
            },
            "repository": {"full_name": "org/repo"},
        }

        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            handler._handle_issue_comment(payload)
            mock_thread.assert_called_once()
            call_kwargs = mock_thread.call_args
            assert call_kwargs.kwargs["target"] == handler._post_agents_list

    def test_help_works_without_waiting_label(self) -> None:
        """@simplecode --help should work even without the waiting label."""
        config = _make_config()
        handler = WebhookHandler(config)

        payload = {
            "action": "created",
            "comment": {
                "body": "@simplecode --list",
                "user": {"login": "otheruser"},
            },
            "issue": {
                "number": 42,
                "labels": [{"name": "bug"}],
            },
            "repository": {"full_name": "org/repo"},
        }

        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            handler._handle_issue_comment(payload)
            mock_thread.assert_called_once()

    def test_operator_can_trigger(self) -> None:
        config = _make_config()
        handler = WebhookHandler(config)

        payload = {
            "action": "created",
            "comment": {
                "id": 901,
                "body": "@simplecode triage",
                "user": {"login": "testuser"},
            },
            "issue": {
                "number": 42,
                "html_url": "https://github.com/org/repo/issues/42",
                "labels": [],
            },
            "repository": {"full_name": "org/repo"},
        }

        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            handler._handle_issue_comment(payload)
            mock_thread.assert_called_once()
            assert mock_thread.call_args.kwargs["target"] == handler._start_pipeline

    def test_bot_marker_comment_ignored(self) -> None:
        config = _make_config()
        handler = WebhookHandler(config)

        payload = {
            "action": "created",
            "comment": {
                "id": 902,
                "body": "## Pipeline Checkpointed\n<!-- simplecode:session=abc12345 -->",
                "user": {"login": "testuser"},
            },
            "issue": {
                "number": 42,
                "labels": [{"name": "simplecode:waiting"}],
            },
            "repository": {"full_name": "org/repo"},
        }

        with patch("threading.Thread") as mock_thread:
            handler._handle_issue_comment(payload)
            mock_thread.assert_not_called()

    def test_bot_comment_ignored(self) -> None:
        config = _make_config()
        handler = WebhookHandler(config)

        payload = {
            "action": "created",
            "comment": {
                "body": "some bot message",
                "user": {"login": "github-actions[bot]"},
            },
            "issue": {
                "number": 42,
                "labels": [{"name": "simplecode:waiting"}],
            },
            "repository": {"full_name": "org/repo"},
        }

        with patch("threading.Thread") as mock_thread:
            handler._handle_issue_comment(payload)
            mock_thread.assert_not_called()

    def test_missing_waiting_label_ignores_reply(self) -> None:
        config = _make_config()
        handler = WebhookHandler(config)

        payload = {
            "action": "created",
            "comment": {
                "body": "looks good",
                "user": {"login": "otheruser"},
            },
            "issue": {
                "number": 42,
                "labels": [{"name": "bug"}],
            },
            "repository": {"full_name": "org/repo"},
        }

        with patch("threading.Thread") as mock_thread:
            handler._handle_issue_comment(payload)
            mock_thread.assert_not_called()


class TestFindSessionId:
    def test_extracts_session_from_comments(self) -> None:
        mock_stdout = (
            "Some regular comment\n"
            "## Pipeline Checkpointed\n<!-- simplecode:session=abc12345 -->\n"
            "Another comment\n"
        )
        with patch("simplecode.webhook._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout=mock_stdout)
            result = _find_session_id("org/repo", 42)
            assert result == "abc12345"
            assert "--paginate" in mock_gh.call_args.args

    def test_returns_latest_session(self) -> None:
        mock_stdout = (
            "<!-- simplecode:session=first123 -->\n"
            "<!-- simplecode:session=second45 -->\n"
        )
        with patch("simplecode.webhook._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout=mock_stdout)
            result = _find_session_id("org/repo", 42)
            assert result == "second45"

    def test_returns_none_when_no_session(self) -> None:
        with patch("simplecode.webhook._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout="just regular comments\n")
            result = _find_session_id("org/repo", 42)
            assert result is None

    def test_returns_none_on_api_failure(self) -> None:
        with patch("simplecode.webhook._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=1, stdout="")
            result = _find_session_id("org/repo", 42)
            assert result is None


class TestEnsureRepo:
    def test_clones_new_repo(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _ensure_repo("org/myrepo", tmp_path)
            assert result == tmp_path / "org" / "myrepo"
            mock_run.assert_called_once()
            cmd = mock_run.call_args.args[0]
            assert cmd[0] == "gh"
            assert "clone" in cmd

    def test_pulls_existing_repo(self, tmp_path: Path) -> None:
        repo_path = tmp_path / "org" / "myrepo"
        repo_path.mkdir(parents=True)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _ensure_repo("org/myrepo", tmp_path)
            assert result == repo_path
            cmd = mock_run.call_args.args[0]
            assert "pull" in cmd

    def test_clone_failure_raises(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="not found")
            try:
                _ensure_repo("org/badrepo", tmp_path)
                assert False, "Should have raised"
            except RuntimeError as e:
                assert "not found" in str(e)

    def test_pull_failure_raises(self, tmp_path: Path) -> None:
        repo_path = tmp_path / "org" / "myrepo"
        repo_path.mkdir(parents=True)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="diverged")
            try:
                _ensure_repo("org/myrepo", tmp_path)
                assert False, "Should have raised"
            except RuntimeError as e:
                assert "diverged" in str(e)


class TestWebhookConfigDefaults:
    def test_working_dir_defaults_to_none(self) -> None:
        config = _make_config()
        assert config.working_dir is None

    def test_repos_dir_has_default(self) -> None:
        config = _make_config()
        assert config.repos_dir == Path.home() / ".simplecode" / "repos"

    def test_custom_repos_dir(self) -> None:
        config = _make_config(repos_dir=Path("/tmp/myrepos"))
        assert config.repos_dir == Path("/tmp/myrepos")


class TestPoller:
    def test_creates_with_repos(self) -> None:
        poller = _make_poller(["org/repo1", "org/repo2"])
        assert poller.repos == ["org/repo1", "org/repo2"]
        assert poller.interval == 30

    def test_skips_already_processed_comments(self) -> None:
        poller = _make_poller()

        comments = [
            {
                "id": 100,
                "body": "@simplecode feature",
                "user": {"login": "otheruser"},
                "issue_url": "https://api.github.com/repos/org/repo/issues/42",
            }
        ]

        issue = {
            "number": 42,
            "html_url": "https://github.com/org/repo/issues/42",
            "labels": [],
        }
        with patch.object(poller, "_fetch_recent_comments", return_value=comments), \
             patch.object(poller, "_fetch_issue", return_value=issue), \
             patch.object(poller.handler, "_start_pipeline") as mock_start:
            poller._poll_once()
            assert mock_start.call_count == 1

            poller._poll_once()
            assert mock_start.call_count == 1

    def test_ignores_bot_comments(self) -> None:
        poller = _make_poller()

        comments = [
            {
                "id": 200,
                "body": "bot output",
                "user": {"login": "github-actions[bot]"},
                "issue_url": "https://api.github.com/repos/org/repo/issues/42",
            }
        ]

        with patch.object(poller, "_fetch_recent_comments", return_value=comments), \
             patch.object(poller.handler, "_start_pipeline") as mock_start:
            poller._poll_once()
            mock_start.assert_not_called()
            assert 200 in poller.handler._processed_comment_ids

    def test_skips_self_comments(self) -> None:
        poller = _make_poller()

        comments = [
            {
                "id": 400,
                "body": "## Pipeline Error\n\n```\nsome error\n```\n\n<!-- simplecode:bot -->",
                "user": {"login": "testuser"},
                "issue_url": "https://api.github.com/repos/org/repo/issues/42",
            }
        ]

        with patch.object(poller, "_fetch_recent_comments", return_value=comments), \
             patch.object(poller.handler, "_start_pipeline") as mock_start:
            poller._poll_once()
            mock_start.assert_not_called()
            assert 400 in poller.handler._processed_comment_ids

    def test_failed_fetch_does_not_advance_watermark(self) -> None:
        poller = _make_poller()
        before = dict(poller._last_poll)

        with patch.object(poller, "_fetch_recent_comments", return_value=None):
            poller._poll_once()

        assert poller._last_poll == before

    def test_trigger_skips_issue_lookup(self) -> None:
        poller = _make_poller()
        comments = [
            {
                "id": 301,
                "body": "@simplecode triage",
                "user": {"login": "otheruser"},
                "issue_url": "https://api.github.com/repos/org/repo/issues/10",
            }
        ]

        with patch.object(poller, "_fetch_recent_comments", return_value=comments), \
             patch.object(poller, "_fetch_issue") as mock_issue, \
             patch.object(poller.handler, "_handle_issue_comment") as mock_handle:
            poller._poll_once()
            mock_issue.assert_not_called()
            mock_handle.assert_called_once()

    def test_processes_trigger_comment(self) -> None:
        poller = _make_poller()

        comments = [
            {
                "id": 300,
                "body": "@simplecode triage",
                "user": {"login": "otheruser"},
                "issue_url": "https://api.github.com/repos/org/repo/issues/10",
            }
        ]
        issue = {"number": 10, "html_url": "https://github.com/org/repo/issues/10", "labels": []}

        with patch.object(poller, "_fetch_recent_comments", return_value=comments), \
             patch.object(poller, "_fetch_issue", return_value=issue), \
             patch.object(poller.handler, "_handle_issue_comment") as mock_handle:
            poller._poll_once()
            mock_handle.assert_called_once()
            payload = mock_handle.call_args.args[0]
            assert payload["comment"]["body"] == "@simplecode triage"
            assert payload["repository"]["full_name"] == "org/repo"

    def test_issue_number_from_url(self) -> None:
        poller = _make_poller()
        url = "https://api.github.com/repos/org/repo/issues/42"
        assert poller._issue_number_from_url(url) == 42
        assert poller._issue_number_from_url("https://example.com/nope") is None

    def test_polls_multiple_repos(self) -> None:
        poller = _make_poller(["org/repo1", "org/repo2"])

        with patch.object(poller, "_fetch_recent_comments", return_value=[]) as mock_fetch:
            poller._poll_once()
            assert mock_fetch.call_count == 2
            repos_called = [call.args[0] for call in mock_fetch.call_args_list]
            assert "org/repo1" in repos_called
            assert "org/repo2" in repos_called


class TestWebhookHTTP:
    def test_rejects_oversized_body(self) -> None:
        handler = WebhookHandler(_make_config())
        request_cls = _make_request_handler(handler, None)
        req = request_cls.__new__(request_cls)
        req.path = "/webhook"
        req.headers = {"Content-Length": str(_MAX_WEBHOOK_BODY + 1)}
        req.rfile = BytesIO(b"x")
        req.client_address = ("127.0.0.1", 1)
        codes: list[int] = []
        req.send_response = lambda code, *args, **kwargs: codes.append(code)
        req.send_header = lambda *args, **kwargs: None
        req.end_headers = lambda: None
        req.do_POST()
        assert codes == [413]


def test_example_pipelines_have_prompt_dirs() -> None:
    from pathlib import Path

    from simplecode.pipeline import PipelineConfig
    from simplecode.runner import discover_prompts

    root = Path(__file__).resolve().parents[1]
    for yaml_path in (root / "examples").glob("*/pipeline.yaml"):
        config = PipelineConfig.load(yaml_path)
        for agent in config.agents:
            discover_prompts(yaml_path.parent / agent.prompts)
