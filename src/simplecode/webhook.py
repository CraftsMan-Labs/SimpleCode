"""GitHub webhook server and polling mode for triggering and resuming pipelines."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from simplecode.errors import CheckpointError, SimpleCodeError
from simplecode.pipeline import Pipeline, PipelineConfig
from simplecode.progress import CLIProgressCallback
from simplecode.store import SessionStore

logger = logging.getLogger(__name__)

_SESSION_MARKER_RE = re.compile(r"<!-- simplecode:session=([a-zA-Z0-9]+) -->")


_REPOS_BASE = Path.home() / ".simplecode" / "repos"


@dataclass
class WebhookConfig:
    """Configuration for the webhook server."""

    pipeline_map: dict[str, str]
    default_pipeline: str | None = None
    runtime: str = "claude"
    working_dir: str | None = None
    output_dir: str = "./pipeline-output"
    timeout: int = 86400
    model: str | None = None
    permissions: str = "write"
    port: int = 8080
    webhook_secret: str | None = None
    github_user: str | None = None
    database_url: str | None = None
    repos_dir: Path = _REPOS_BASE


def _gh(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a gh CLI command and return the result."""
    cmd = ["gh", *args]
    logger.debug("gh %s", " ".join(args))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        input=input_text,
    )
    if result.returncode != 0:
        logger.error("gh command failed: %s\nstderr: %s", " ".join(cmd), result.stderr)
    return result


def _ensure_repo(repo: str, repos_dir: Path) -> Path:
    """Clone or pull a repo into repos_dir/<owner>/<name>. Returns the local path."""
    repo_path = repos_dir / repo
    if repo_path.exists():
        logger.info("Pulling latest for %s", repo)
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=120,
        )
    else:
        logger.info("Cloning %s into %s", repo, repo_path)
        repo_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["gh", "repo", "clone", repo, str(repo_path)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to clone {repo}: {result.stderr}")
    return repo_path


def _post_comment(repo: str, issue_number: int, body: str) -> None:
    _gh("issue", "comment", str(issue_number), "--repo", repo, "--body", body)


def _ensure_label(repo: str, label: str) -> None:
    """Create a label in the repo if it doesn't already exist."""
    colors = {
        "simplecode:waiting": "fbca04",
        "simplecode:done": "0e8a16",
        "simplecode:stopped": "d93f0b",
    }
    color = colors.get(label, "ededed")
    result = _gh("label", "create", label, "--repo", repo, "--color", color, "--force")
    if result.returncode != 0:
        logger.debug("Label create for '%s' on %s: %s", label, repo, result.stderr.strip())


def _add_label(repo: str, issue_number: int, label: str) -> None:
    _ensure_label(repo, label)
    _gh("issue", "edit", str(issue_number), "--repo", repo, "--add-label", label)


def _remove_label(repo: str, issue_number: int, label: str) -> None:
    _gh("issue", "edit", str(issue_number), "--repo", repo, "--remove-label", label)


def _find_session_id(repo: str, issue_number: int) -> str | None:
    """Scan issue comments for the latest simplecode session marker."""
    result = _gh(
        "api",
        f"repos/{repo}/issues/{issue_number}/comments",
        "--jq", ".[].body",
    )
    if result.returncode != 0:
        return None
    session_id = None
    for match in _SESSION_MARKER_RE.finditer(result.stdout):
        session_id = match.group(1)
    return session_id


def _checkpoint_comment(
    pipeline_name: str, agent_id: str, session_id: str, agent_index: int, total_agents: int,
) -> str:
    return (
        f"## Pipeline Checkpointed\n\n"
        f"**Pipeline:** {pipeline_name}\n"
        f"**Agent:** {agent_id} ({agent_index}/{total_agents})\n"
        f"**Session:** `{session_id}`\n\n"
        f"The agent has finished its work and is waiting for your review.\n\n"
        f"### Actions\n"
        f"- **Reply** to send feedback to the agent (stays at this checkpoint)\n"
        f"- `/approve` to approve and advance to the next agent\n"
        f"- `/stop` to stop the entire pipeline\n"
        f"- `@simplecode --help` to list available agents\n\n"
        f"<!-- simplecode:session={session_id} -->"
    )


def _complete_comment(pipeline_name: str, agents_completed: int, total: int) -> str:
    return (
        f"## Pipeline Complete\n\n"
        f"**Pipeline:** {pipeline_name}\n"
        f"**Agents completed:** {agents_completed}/{total}\n\n"
        f"<!-- simplecode:status=complete -->"
    )


def _stopped_comment(pipeline_name: str, stopped_at: str) -> str:
    return (
        f"## Pipeline Stopped\n\n"
        f"**Pipeline:** {pipeline_name}\n"
        f"**Stopped at:** {stopped_at}\n\n"
        f"<!-- simplecode:status=stopped -->"
    )


def _agents_list_comment(pipeline_name: str, agents: list[dict[str, str]]) -> str:
    lines = [
        f"## @simplecode — {pipeline_name}\n",
        "| # | Agent | Gate | Permissions |",
        "|---|-------|------|-------------|",
    ]
    for i, a in enumerate(agents, 1):
        gate = a.get("gate") or "—"
        perm = a.get("permissions", "write")
        lines.append(f"| {i} | {a['id']} | {gate} | {perm} |")
    return "\n".join(lines)


def _error_comment(message: str) -> str:
    return f"## Pipeline Error\n\n```\n{message}\n```"


class WebhookHandler:
    """Handles GitHub webhook events and dispatches pipeline operations."""

    def __init__(self, config: WebhookConfig) -> None:
        self.config = config
        self._lock = threading.Lock()

    def handle_event(self, event_type: str, payload: dict) -> None:
        """Dispatch a GitHub webhook event to the appropriate handler."""
        if event_type == "issue_comment":
            self._handle_issue_comment(payload)
        elif event_type == "issues":
            self._handle_issues(payload)
        else:
            logger.debug("Ignoring event type: %s", event_type)

    def _handle_issues(self, payload: dict) -> None:
        action = payload.get("action")
        if action != "assigned":
            return

        issue = payload.get("issue", {})
        issue_number = issue.get("number")
        repo = payload.get("repository", {}).get("full_name")

        if not issue_number or not repo:
            return

        if not self.config.default_pipeline:
            logger.debug("No default pipeline configured, ignoring assignment")
            return
        assignee = payload.get("assignee", {}).get("login", "")
        if self.config.github_user and assignee != self.config.github_user:
            logger.debug(
                "Assigned to '%s', not '%s', ignoring",
                assignee, self.config.github_user,
            )
            return

        issue_url = issue.get("html_url", "")
        logger.info(
            "Issue #%d assigned to '%s' → default pipeline %s",
            issue_number, assignee, self.config.default_pipeline,
        )
        thread = threading.Thread(
            target=self._start_pipeline,
            args=(issue_url, issue_number, self.config.default_pipeline, repo),
            daemon=True,
        )
        thread.start()

    def _handle_issue_comment(self, payload: dict) -> None:
        action = payload.get("action")
        if action != "created":
            return

        comment = payload.get("comment", {})
        author = comment.get("user", {}).get("login", "")
        body = (comment.get("body") or "").strip()
        issue = payload.get("issue", {})
        issue_number = issue.get("number")
        repo = payload.get("repository", {}).get("full_name")

        if not issue_number or not repo:
            return

        if author.endswith("[bot]"):
            return

        command, text = self._parse_command(body)

        if command == "help":
            thread = threading.Thread(
                target=self._post_agents_list,
                args=(issue_number, repo),
                daemon=True,
            )
            thread.start()
            return

        if command == "trigger":
            issue_url = issue.get("html_url", "")
            pipeline_dir = self.config.pipeline_map.get(text or "")
            if not pipeline_dir:
                available = ", ".join(sorted(self.config.pipeline_map.keys()))
                _post_comment(
                    repo, issue_number,
                    _error_comment(
                        f"Unknown keyword: `{text}`\n\nAvailable: {available}"
                    ),
                )
                return
            logger.info(
                "Issue #%d @simplecode %s by %s → pipeline %s",
                issue_number, text, author, pipeline_dir,
            )
            thread = threading.Thread(
                target=self._start_pipeline,
                args=(issue_url, issue_number, pipeline_dir, repo),
                daemon=True,
            )
            thread.start()
            return

        labels = [lbl.get("name", "") for lbl in issue.get("labels", [])]
        if "simplecode:waiting" not in labels:
            logger.debug("Issue #%d missing simplecode:waiting label, ignoring comment", issue_number)
            return

        if command == "stop":
            logger.info("Issue #%d comment by %s: /stop", issue_number, author)
            thread = threading.Thread(
                target=self._resume_from_comment,
                args=(issue_number, repo, "stop", None),
                daemon=True,
            )
            thread.start()
            return

        if command == "approve":
            logger.info("Issue #%d comment by %s → approve + advance", issue_number, author)
            thread = threading.Thread(
                target=self._resume_from_comment,
                args=(issue_number, repo, "approve", None),
                daemon=True,
            )
            thread.start()
            return

        if command == "reply":
            preview = text[:60] + "..." if text and len(text) > 60 else text
            logger.info("Issue #%d comment by %s → feedback: %s", issue_number, author, preview)
            thread = threading.Thread(
                target=self._resume_from_comment,
                args=(issue_number, repo, "reply", text),
                daemon=True,
            )
            thread.start()

    @staticmethod
    def _parse_command(body: str) -> tuple[str | None, str | None]:
        """Parse a command from a comment body.

        Returns (command, text):
        - ("stop", None) for /stop
        - ("help", None) for @simplecode --help or @simplecode --list
        - ("trigger", keyword) for @simplecode <keyword> (starts a pipeline)
        - ("reply", <body>) for any other non-empty comment (feedback + continue)
        - (None, None) for empty comments
        """
        if body.startswith("/stop"):
            return "stop", None
        if body.startswith("/approve") or body.startswith("/continue"):
            return "approve", None
        if body.startswith("@simplecode"):
            rest = body[len("@simplecode"):].strip()
            if rest in ("--help", "--list", "help", "list", ""):
                return "help", None
            return "trigger", rest.split()[0].lower()
        if not body:
            return None, None
        return "reply", body

    def _post_agents_list(self, issue_number: int, repo: str) -> None:
        """Post a comment listing available agents from the session's pipeline."""
        session_id = _find_session_id(repo, issue_number)
        if not session_id:
            for pdir in self.config.pipeline_map.values():
                pipeline_path = Path(pdir).resolve() / "pipeline.yaml"
                if pipeline_path.exists():
                    config = PipelineConfig.load(pipeline_path)
                    agents = [{"id": a.id, "gate": a.gate, "permissions": a.permissions} for a in config.agents]
                    _post_comment(repo, issue_number, _agents_list_comment(config.name, agents))
                    return
            _post_comment(repo, issue_number, _error_comment("No pipeline found."))
            return

        try:
            store = self._get_store()
            session = store.load_session(session_id)
            pipeline_yaml_path = Path(session.pipeline_yaml)
            if not pipeline_yaml_path.exists():
                _post_comment(repo, issue_number, _error_comment(f"Pipeline YAML not found: {pipeline_yaml_path}"))
                return
            config = PipelineConfig.load(pipeline_yaml_path)
            agents = [{"id": a.id, "gate": a.gate, "permissions": a.permissions} for a in config.agents]
            _post_comment(repo, issue_number, _agents_list_comment(config.name, agents))
        except SimpleCodeError as e:
            _post_comment(repo, issue_number, _error_comment(str(e)))

    def _get_store(self) -> SessionStore:
        from simplecode.store import SQLITE_DEFAULT_PATH

        url = self.config.database_url or str(SQLITE_DEFAULT_PATH)
        store = SessionStore(url)
        store.ensure_schema()
        return store

    def _start_pipeline(
        self,
        issue_url: str,
        issue_number: int,
        pipeline_dir: str,
        repo: str,
        trigger_label: str | None = None,
    ) -> None:
        pipeline_path = Path(pipeline_dir).resolve() / "pipeline.yaml"
        if not pipeline_path.exists():
            logger.error("Pipeline YAML not found: %s", pipeline_path)
            _post_comment(repo, issue_number, _error_comment(f"Pipeline YAML not found: {pipeline_path}"))
            return

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(f"# GitHub Issue\n\n**Issue URL:** {issue_url}\n")
            issue_md_path = f.name

        try:
            config = PipelineConfig.load(pipeline_path)
            pipeline = Pipeline(config, pipeline_path.parent, pipeline_yaml_path=pipeline_path)
            store = self._get_store()

            if self.config.working_dir:
                working_dir = self.config.working_dir
            else:
                working_dir = str(_ensure_repo(repo, self.config.repos_dir))

            logger.info("Starting pipeline '%s' for issue #%d in %s", config.name, issue_number, working_dir)

            result = pipeline.run(
                runtime=self.config.runtime,
                working_dir=working_dir,
                input_files=[issue_md_path],
                gate_mode="checkpoint",
                output_dir=self.config.output_dir,
                timeout=self.config.timeout,
                model=self.config.model,
                permissions=self.config.permissions,
                progress=CLIProgressCallback(),
                heartbeat_interval=30,
                store=store,
            )

            if trigger_label:
                _remove_label(repo, issue_number, trigger_label)

            if result.status == "checkpointed":
                gated_index = 0
                if result.checkpoint_agent:
                    for i, a in enumerate(config.agents):
                        if a.id == result.checkpoint_agent:
                            gated_index = i + 1
                            break
                _post_comment(
                    repo, issue_number,
                    _checkpoint_comment(
                        config.name,
                        result.checkpoint_agent or "unknown",
                        result.session_id,
                        gated_index,
                        len(config.agents),
                    ),
                )
                _add_label(repo, issue_number, "simplecode:waiting")
                logger.info(
                    "Pipeline '%s' checkpointed at '%s' (session %s)",
                    config.name, result.checkpoint_agent, result.session_id,
                )
            elif result.status == "complete":
                _post_comment(
                    repo, issue_number,
                    _complete_comment(config.name, result.agents_completed, result.total_agents),
                )
                _add_label(repo, issue_number, "simplecode:done")
                logger.info("Pipeline '%s' completed", config.name)
            elif result.gate_rejected_at:
                _post_comment(
                    repo, issue_number,
                    _stopped_comment(config.name, result.gate_rejected_at),
                )
                _add_label(repo, issue_number, "simplecode:stopped")

        except SimpleCodeError as e:
            logger.error("Pipeline error for issue #%d: %s", issue_number, e)
            _post_comment(repo, issue_number, _error_comment(str(e)))
        except Exception:
            logger.exception("Unexpected error running pipeline for issue #%d", issue_number)
            _post_comment(repo, issue_number, _error_comment("Unexpected server error. Check server logs."))
        finally:
            Path(issue_md_path).unlink(missing_ok=True)

    def _resume_from_comment(
        self,
        issue_number: int,
        repo: str,
        command: str,
        feedback_text: str | None,
    ) -> None:
        session_id = _find_session_id(repo, issue_number)
        if not session_id:
            _post_comment(repo, issue_number, _error_comment("No SimpleCode session found on this issue."))
            return

        try:
            store = self._get_store()
            session = store.load_session(session_id)

            pipeline_yaml_path = Path(session.pipeline_yaml)
            if not pipeline_yaml_path.exists():
                _post_comment(
                    repo, issue_number,
                    _error_comment(f"Pipeline YAML not found: {pipeline_yaml_path}"),
                )
                return

            config = PipelineConfig.load(pipeline_yaml_path)
            pipeline = Pipeline(config, pipeline_yaml_path.parent, pipeline_yaml_path=pipeline_yaml_path)

            if command == "stop":
                result = pipeline.resume(
                    session_id=session_id,
                    store=store,
                    reject=True,
                    gate_mode="checkpoint",
                    progress=CLIProgressCallback(),
                )
            elif command == "approve":
                result = pipeline.resume(
                    session_id=session_id,
                    store=store,
                    approve=True,
                    gate_mode="checkpoint",
                    progress=CLIProgressCallback(),
                )
            else:
                result = pipeline.resume(
                    session_id=session_id,
                    store=store,
                    feedback=feedback_text,
                    gate_mode="checkpoint",
                    progress=CLIProgressCallback(),
                )

            if result.status == "checkpointed":
                gated_index = 0
                if result.checkpoint_agent:
                    for i, a in enumerate(config.agents):
                        if a.id == result.checkpoint_agent:
                            gated_index = i + 1
                            break
                _post_comment(
                    repo, issue_number,
                    _checkpoint_comment(
                        config.name,
                        result.checkpoint_agent or "unknown",
                        result.session_id,
                        gated_index,
                        result.total_agents,
                    ),
                )
                logger.info(
                    "Pipeline '%s' re-checkpointed at '%s' (session %s)",
                    config.name, result.checkpoint_agent, result.session_id,
                )
            elif result.status == "complete":
                _remove_label(repo, issue_number, "simplecode:waiting")
                _post_comment(
                    repo, issue_number,
                    _complete_comment(config.name, result.agents_completed, result.total_agents),
                )
                _add_label(repo, issue_number, "simplecode:done")
                logger.info("Pipeline '%s' completed (session %s)", config.name, result.session_id)
            elif result.status == "rejected":
                _remove_label(repo, issue_number, "simplecode:waiting")
                _post_comment(
                    repo, issue_number,
                    _stopped_comment(config.name, result.gate_rejected_at or "unknown"),
                )
                _add_label(repo, issue_number, "simplecode:stopped")
                logger.info("Pipeline '%s' stopped (session %s)", config.name, result.session_id)

        except CheckpointError as e:
            if "expected 'waiting'" in str(e):
                logger.warning(
                    "Resume skipped for issue #%d (session %s): %s",
                    issue_number, session_id, e,
                )
            else:
                logger.error("Resume error for issue #%d (session %s): %s", issue_number, session_id, e)
                _post_comment(repo, issue_number, _error_comment(str(e)))
        except SimpleCodeError as e:
            logger.error("Resume error for issue #%d (session %s): %s", issue_number, session_id, e)
            _post_comment(repo, issue_number, _error_comment(str(e)))
        except Exception:
            logger.exception("Unexpected error resuming session %s for issue #%d", session_id, issue_number)
            _post_comment(repo, issue_number, _error_comment("Unexpected server error. Check server logs."))


def _verify_signature(payload_body: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook signature (X-Hub-Signature-256)."""
    expected = "sha256=" + hmac.new(
        secret.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _make_request_handler(
    handler: WebhookHandler,
    webhook_secret: str | None,
) -> type[BaseHTTPRequestHandler]:
    """Create a request handler class bound to the webhook handler."""

    class RequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            if self.path != "/webhook":
                self.send_response(404)
                self.end_headers()
                return

            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

            if webhook_secret:
                signature = self.headers.get("X-Hub-Signature-256", "")
                if not _verify_signature(body, signature, webhook_secret):
                    logger.warning("Invalid webhook signature from %s", self.client_address[0])
                    self.send_response(401)
                    self.end_headers()
                    return

            event_type = self.headers.get("X-GitHub-Event", "")

            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return

            self.send_response(200)
            self.end_headers()

            handler.handle_event(event_type, payload)

        def log_message(self, format: str, *args: object) -> None:
            logger.debug("HTTP %s", format % args)

    return RequestHandler


class Poller:
    """Polls GitHub for new comments on issues, replacing the need for webhooks/tunnels."""

    def __init__(self, config: WebhookConfig, repos: list[str], interval: int = 30) -> None:
        self.config = config
        self.repos = repos
        self.interval = interval
        self.handler = WebhookHandler(config)
        self._processed_comment_ids: set[int] = set()
        self._last_poll: str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._stop_event = threading.Event()

    def _fetch_recent_comments(self, repo: str) -> list[dict]:
        result = _gh(
            "api",
            f"repos/{repo}/issues/comments?since={self._last_poll}&sort=created&direction=asc",
            "--paginate",
        )
        if result.returncode != 0:
            logger.error("Failed to fetch comments for %s: %s", repo, result.stderr)
            return []
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

    def _fetch_issue(self, repo: str, issue_number: int) -> dict:
        result = _gh("api", f"repos/{repo}/issues/{issue_number}")
        if result.returncode != 0:
            return {}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}

    def _issue_number_from_url(self, issue_url: str) -> int | None:
        match = re.search(r"/issues/(\d+)", issue_url)
        return int(match.group(1)) if match else None

    def _repo_from_url(self, url: str) -> str | None:
        match = re.search(r"repos/([^/]+/[^/]+)/", url)
        return match.group(1) if match else None

    def _poll_once(self) -> None:
        poll_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for repo in self.repos:
            comments = self._fetch_recent_comments(repo)
            for comment in comments:
                comment_id = comment.get("id")
                if not comment_id or comment_id in self._processed_comment_ids:
                    continue

                author = comment.get("user", {}).get("login", "")
                if author.endswith("[bot]"):
                    self._processed_comment_ids.add(comment_id)
                    continue

                if self.config.github_user and author == self.config.github_user:
                    self._processed_comment_ids.add(comment_id)
                    continue

                body = (comment.get("body") or "").strip()
                issue_url = comment.get("issue_url", "")
                issue_number = self._issue_number_from_url(issue_url)
                if not issue_number:
                    self._processed_comment_ids.add(comment_id)
                    continue

                command, _text = WebhookHandler._parse_command(body)
                if command is None:
                    self._processed_comment_ids.add(comment_id)
                    continue

                logger.info(
                    "Poll: new comment #%d on %s#%d by %s → %s",
                    comment_id, repo, issue_number, author, command,
                )

                issue = self._fetch_issue(repo, issue_number)
                labels = [lbl.get("name", "") for lbl in issue.get("labels", [])]
                issue_html_url = issue.get("html_url", "")

                payload = {
                    "action": "created",
                    "comment": {
                        "body": body,
                        "user": {"login": author},
                    },
                    "issue": {
                        "number": issue_number,
                        "html_url": issue_html_url,
                        "labels": [{"name": l} for l in labels],
                    },
                    "repository": {"full_name": repo},
                }

                self.handler._handle_issue_comment(payload)
                self._processed_comment_ids.add(comment_id)

        self._last_poll = poll_start

    def run(self) -> None:
        logger.info(
            "Polling %d repo(s) every %ds: %s",
            len(self.repos), self.interval, ", ".join(self.repos),
        )
        try:
            while not self._stop_event.is_set():
                self._poll_once()
                self._stop_event.wait(self.interval)
        except KeyboardInterrupt:
            logger.info("Polling stopped")

    def stop(self) -> None:
        self._stop_event.set()


def run_poller(config: WebhookConfig, repos: list[str], interval: int = 30) -> None:
    """Start polling GitHub for new comments (blocking). No tunnel needed."""
    poller = Poller(config, repos, interval)
    poller.run()


def run_server(config: WebhookConfig) -> None:
    """Start the webhook HTTP server (blocking)."""
    handler = WebhookHandler(config)
    request_handler = _make_request_handler(handler, config.webhook_secret)
    server = HTTPServer(("0.0.0.0", config.port), request_handler)

    logger.info("Webhook server listening on port %d", config.port)
    logger.info("Pipeline map: %s", json.dumps(config.pipeline_map, indent=2))
    if config.default_pipeline:
        logger.info("Default pipeline (assignments): %s", config.default_pipeline)
    if config.github_user:
        logger.info("GitHub user filter: %s", config.github_user)
    logger.info("Endpoints: POST /webhook, GET /health")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down webhook server")
        server.shutdown()
