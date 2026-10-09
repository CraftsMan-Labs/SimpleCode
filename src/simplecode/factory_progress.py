"""Durable, sanitized progress tracking for one factory job."""

from __future__ import annotations

import hashlib
import html
import logging
from typing import Protocol

from simplecode.plane import PlaneComment, PlaneNotFoundError, sanitize_error_text
from simplecode.store import SessionStore

logger = logging.getLogger(__name__)


class ProgressPlaneClient(Protocol):
    """Plane comment surface used by the progress tracker."""

    def post_comment(self, project_id: str, work_item_id: str, comment: str) -> PlaneComment: ...

    def list_comments(self, project_id: str, work_item_id: str) -> list[PlaneComment]: ...

    def update_comment(
        self,
        project_id: str,
        work_item_id: str,
        comment_id: str,
        comment: str,
    ) -> PlaneComment: ...


class FactoryProgressTracker:
    """Persist factory/pipeline boundaries and maintain one Plane comment.

    Callers provide only short, operator-authored labels. Ticket bodies, agent
    output, stdout, and model reasoning never enter this tracker.
    """

    def __init__(
        self,
        store: SessionStore,
        plane: ProgressPlaneClient,
        external_key: str,
        project_id: str,
        work_item_id: str,
    ) -> None:
        self._store = store
        self._plane = plane
        self.external_key = external_key
        self.project_id = project_id
        self.work_item_id = work_item_id
        self.session_id = store.load_factory_job(external_key).session_id
        self._comment_marker = factory_progress_marker(external_key)

    def milestone(
        self,
        event_type: str,
        stage: str,
        *,
        status: str | None = None,
        message: str | None = None,
        metadata: dict[str, object] | None = None,
        update_comment: bool = True,
    ) -> None:
        """Record a factory boundary and clear stale agent/prompt fields."""
        self._persist(
            event_type,
            stage,
            status=status,
            message=message,
            metadata=metadata,
            current_agent=None,
            current_prompt=None,
        )
        if update_comment:
            self._sync_comment()

    def on_session(self, session_id: str) -> None:
        self.session_id = session_id
        job = self._store.load_factory_job(self.external_key)
        self._store.update_factory_job(
            self.external_key,
            job.status,
            session_id=session_id,
        )
        self._store.append_factory_event(
            self.external_key,
            "session_started",
            session_id=session_id,
            stage=job.current_stage or "pipeline",
            status=job.status,
            message="Pipeline session allocated",
        )

    def on_pipeline_start(self, name: str, total_agents: int) -> None:
        self.milestone(
            "pipeline_started",
            "pipeline",
            message="Pipeline started",
            metadata={"total_agents": total_agents},
        )

    def on_pipeline_done(self, name: str, total_agents: int, status: str) -> None:
        self.milestone(
            "pipeline_finished",
            "pipeline",
            status=status,
            message=f"Pipeline {status}",
            metadata={"total_agents": total_agents},
        )

    def on_agent_start(
        self,
        index: int,
        agent_id: str,
        total_agents: int,
        total_prompts: int,
    ) -> None:
        self._persist(
            "agent_started",
            agent_id,
            message=f"Agent {agent_id} started",
            metadata={
                "agent_index": index,
                "total_agents": total_agents,
                "total_prompts": total_prompts,
            },
            current_agent=agent_id,
            current_prompt=None,
        )
        self._sync_comment()

    def on_prompt_start(self, agent_id: str, prompt_index: int, total_prompts: int) -> None:
        self._persist(
            "prompt_started",
            agent_id,
            message=f"Prompt {prompt_index} started",
            metadata={"prompt_index": prompt_index, "total_prompts": total_prompts},
            current_agent=agent_id,
            current_prompt=prompt_index,
        )

    def on_prompt_done(self, agent_id: str, prompt_index: int, total_prompts: int) -> None:
        self._persist(
            "prompt_finished",
            agent_id,
            message=f"Prompt {prompt_index} finished",
            metadata={"prompt_index": prompt_index, "total_prompts": total_prompts},
            current_agent=agent_id,
            current_prompt=prompt_index,
        )

    def on_agent_done(self, index: int, agent_id: str, total_agents: int) -> None:
        self._persist(
            "agent_finished",
            agent_id,
            message=f"Agent {agent_id} finished",
            metadata={"agent_index": index, "total_agents": total_agents},
            current_agent=None,
            current_prompt=None,
        )
        self._sync_comment()

    def on_gate_start(self, agent_id: str) -> None:
        self._persist(
            "gate_started",
            agent_id,
            message=f"Gate for {agent_id} started",
            current_agent=agent_id,
            current_prompt=None,
        )

    def _persist(
        self,
        event_type: str,
        stage: str,
        *,
        status: str | None = None,
        message: str | None = None,
        metadata: dict[str, object] | None = None,
        current_agent: str | None,
        current_prompt: int | None,
    ) -> None:
        job = self._store.load_factory_job(self.external_key)
        event_status = status or job.status
        self._store.update_factory_job(
            self.external_key,
            job.status,
            session_id=self.session_id,
            current_stage=stage,
            current_agent=current_agent,
            current_prompt=current_prompt,
        )
        self._store.append_factory_event(
            self.external_key,
            event_type,
            session_id=self.session_id,
            stage=stage,
            status=event_status,
            message=message,
            metadata=metadata,
        )

    def _sync_comment(self) -> None:
        job = self._store.load_factory_job(self.external_key)
        body = self._render_comment()
        if job.plane_progress_comment_id:
            try:
                self._plane.update_comment(
                    self.project_id,
                    self.work_item_id,
                    job.plane_progress_comment_id,
                    body,
                )
                return
            except PlaneNotFoundError:
                # A deleted progress comment gets exactly one replacement.
                self._adopt_or_post_comment(
                    body,
                    exclude_comment_id=job.plane_progress_comment_id,
                )
            except Exception as exc:
                self._log_comment_failure(exc)
            return
        self._adopt_or_post_comment(body)

    def _adopt_or_post_comment(
        self,
        body: str,
        *,
        exclude_comment_id: str | None = None,
    ) -> None:
        try:
            comments = self._plane.list_comments(self.project_id, self.work_item_id)
        except Exception as exc:
            # Posting when discovery failed would reopen the duplicate window.
            self._log_comment_failure(exc)
            return
        existing = next(
            (
                comment
                for comment in comments
                if comment.id != exclude_comment_id
                and (self._comment_marker in comment.html or self._comment_marker in comment.body)
            ),
            None,
        )
        if existing is None:
            self._post_new_comment(body)
            return

        self._remember_comment(existing.id)
        try:
            self._plane.update_comment(
                self.project_id,
                self.work_item_id,
                existing.id,
                body,
            )
        except PlaneNotFoundError:
            self._post_new_comment(body)
        except Exception as exc:
            self._log_comment_failure(exc)

    def _post_new_comment(self, body: str) -> None:
        try:
            comment = self._plane.post_comment(self.project_id, self.work_item_id, body)
        except Exception as exc:
            self._log_comment_failure(exc)
            return
        self._remember_comment(comment.id)

    def _remember_comment(self, comment_id: str) -> None:
        job = self._store.load_factory_job(self.external_key)
        self._store.update_factory_job(
            self.external_key,
            job.status,
            plane_progress_comment_id=comment_id,
        )

    def _render_comment(self) -> str:
        job = self._store.load_factory_job(self.external_key)
        items = [
            _progress_item("Status", job.status),
            _progress_item("Stage", job.current_stage or "pending"),
        ]
        if job.current_agent:
            items.append(_progress_item("Agent", job.current_agent))
        if job.current_prompt is not None:
            items.append(_progress_item("Prompt", job.current_prompt))
        if job.session_id:
            items.append(_progress_item("Session", job.session_id))
        if job.branch:
            items.append(_progress_item("Branch", job.branch))
        if job.pr_url:
            items.append(_progress_item("Pull request", job.pr_url))
        if job.last_error:
            error = sanitize_error_text(job.last_error, secret="")
            items.append(_progress_item("Error", error))
        marker = html.escape(self._comment_marker, quote=True)
        return (
            "<h3>Factory Progress</h3>"
            f"<ul>{''.join(items)}</ul>"
            "<p><small>Factory marker: "
            f"<code>{marker}</code>"
            "</small></p>"
        )

    def _log_comment_failure(self, exc: BaseException) -> None:
        logger.warning(
            "Best-effort Plane progress comment failed for %s: %s",
            self.work_item_id,
            sanitize_error_text(str(exc), secret=""),
        )


def factory_progress_marker(external_key: str) -> str:
    """Return a deterministic opaque marker without exposing durable job IDs."""
    digest = hashlib.sha256(external_key.encode("utf-8")).hexdigest()
    return f"simplecode-factory-progress:{digest}"


def _progress_item(label: str, value: object) -> str:
    safe_label = html.escape(label, quote=True)
    safe_value = html.escape(str(value), quote=True)
    return f"<li><strong>{safe_label}:</strong> {safe_value}</li>"
