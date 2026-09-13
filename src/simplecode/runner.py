"""AgentRunner — execute numbered prompts sequentially within a single CLI session."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from simplecode.errors import PipelineConfigError
from simplecode.progress import Heartbeat, ProgressCallback, emit_output
from simplecode.runtime import RuntimeConfig, exec_cli, make_cli_session_id

logger = logging.getLogger(__name__)

_PROMPT_PATTERN = re.compile(r"^(\d+)\.md$")


@dataclass
class AgentResult:
    """Outcome of running a single agent's prompt sequence."""

    agent_id: str
    session_id: str
    output_dir: Path
    prompt_count: int
    success: bool
    final_output: str


def discover_prompts(prompts_dir: Path) -> list[Path]:
    """Find numbered prompt files (1.md, 2.md, ...) sorted numerically.

    Raises PipelineConfigError if the directory is missing, empty, or has gaps.
    """
    if not prompts_dir.is_dir():
        raise PipelineConfigError(f"Prompts directory not found: {prompts_dir}")

    numbered: list[tuple[int, Path]] = []
    for f in prompts_dir.iterdir():
        m = _PROMPT_PATTERN.match(f.name)
        if m:
            numbered.append((int(m.group(1)), f))

    if not numbered:
        raise PipelineConfigError(f"No numbered prompts (1.md, 2.md, ...) in {prompts_dir}")

    numbered.sort(key=lambda x: x[0])

    expected = list(range(numbered[0][0], numbered[0][0] + len(numbered)))
    actual = [n for n, _ in numbered]
    if actual != expected:
        raise PipelineConfigError(
            f"Gap in prompt numbering in {prompts_dir}: found {actual}, expected {expected}"
        )

    return [p for _, p in numbered]


class AgentRunner:
    """Runs a single agent's numbered prompts in one CLI session.

    Prompt 1 starts a new session. Prompts 2+ resume the same session.
    After all prompts, the runner can accept feedback via resume_with_feedback().
    """

    def __init__(
        self,
        agent_id: str,
        runtime_config: RuntimeConfig,
        output_dir: Path,
        timeout: int,
        session_id: str,
        permissions: str = "write",
        model: str | None = None,
        cmd_prefix: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.runtime = runtime_config
        self.output_dir = output_dir / agent_id
        self.timeout = timeout
        self.session_id = session_id
        self.cli_session_id = make_cli_session_id(session_id, agent_id, runtime_config.name)
        self.permissions = permissions
        self.model = model
        self.cmd_prefix = tuple(cmd_prefix) if cmd_prefix else ()
        self._working_dir: str | Path = "."
        self._latest_output: str = ""
        self._on_output: Callable[[str, str], None] | None = None

    def run(
        self,
        prompts_dir: Path,
        context_files: list[str | Path] | None = None,
        working_dir: str | Path = ".",
        progress: ProgressCallback | None = None,
        heartbeat_interval: float = 0,
    ) -> AgentResult:
        """Execute all numbered prompts in sequence. Returns AgentResult."""
        self._working_dir = working_dir
        self._on_output = (
            (lambda stream, chunk: emit_output(progress, stream, chunk)) if progress else None
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)

        prompt_files = discover_prompts(prompts_dir)
        logger.info(
            "Agent '%s': %d prompts, session %s",
            self.agent_id,
            len(prompt_files),
            self.cli_session_id,
        )

        for i, prompt_file in enumerate(prompt_files, 1):
            prompt_text = prompt_file.read_text()

            if progress:
                progress.on_prompt_start(self.agent_id, i, len(prompt_files))

            if i == 1:
                full_prompt = self._assemble_prompt(prompt_text, context_files)
                output = self._exec_with_heartbeat(
                    lambda: self._call_new(full_prompt),
                    i,
                    len(prompt_files),
                    heartbeat_interval,
                )
            else:
                output = self._exec_with_heartbeat(
                    lambda _pt=prompt_text: self._call_resume(_pt),  # type: ignore[misc]
                    i,
                    len(prompt_files),
                    heartbeat_interval,
                )

            self._write_artifact(f"prompt-{i}.md", prompt_text)
            self._write_artifact(f"{i}-output.md", output)
            self._latest_output = output

            if progress:
                progress.on_prompt_done(self.agent_id, i, len(prompt_files))
            logger.info("Agent '%s': prompt %d/%d done", self.agent_id, i, len(prompt_files))

        self._write_output_md(self._latest_output)

        return AgentResult(
            agent_id=self.agent_id,
            session_id=self.session_id,
            output_dir=self.output_dir,
            prompt_count=len(prompt_files),
            success=True,
            final_output=self._latest_output,
        )

    def resume_with_feedback(
        self,
        feedback: str,
        working_dir: str | Path | None = None,
        heartbeat_interval: float = 0,
    ) -> str:
        """Resume the same CLI session with human feedback. Used by Gate."""
        if working_dir:
            self._working_dir = working_dir
        output = self._exec_with_heartbeat(
            lambda: self._call_resume(feedback),
            0,
            0,
            heartbeat_interval,
            label="gate feedback",
        )
        self._latest_output = output
        return output

    def write_final_output(self) -> Path:
        """Write/overwrite output.md with the latest response (post-gate)."""
        return self._write_output_md(self._latest_output)

    def _exec_with_heartbeat(
        self,
        fn: Callable[[], str],
        prompt_index: int,
        total_prompts: int,
        heartbeat_interval: float,
        label: str | None = None,
    ) -> str:
        if heartbeat_interval <= 0:
            return fn()
        msg = label or f"agent '{self.agent_id}' prompt {prompt_index}/{total_prompts}"
        with Heartbeat(lambda: msg, interval=heartbeat_interval):
            return fn()

    def _assemble_prompt(
        self,
        prompt_text: str,
        context_files: list[str | Path] | None = None,
    ) -> str:
        parts = [prompt_text]

        if context_files:
            parts.append("\n\n---\n\n## Context from previous agents\n")
            for f in context_files:
                p = Path(f)
                if p.exists():
                    parts.append(f"### {p.parent.name}/{p.name}\n\n{p.read_text()}\n")
                else:
                    parts.append(f"### {p}\n\n*File not found*\n")

        return "\n".join(parts)

    def _call_new(self, prompt: str) -> str:
        cmd = self.runtime.build_new_cmd(self.cli_session_id, self.permissions, self.model)
        return exec_cli(
            cmd,
            prompt,
            self._working_dir,
            input_mode=self.runtime.input_mode,
            timeout=self.timeout,
            env_extra={"SIMPLECODE_AGENT": self.agent_id},
            cmd_prefix=self.cmd_prefix or None,
            on_output=self._on_output,
        )

    def _call_resume(self, prompt: str) -> str:
        cmd = self.runtime.build_resume_cmd(self.cli_session_id, self.permissions, self.model)
        return exec_cli(
            cmd,
            prompt,
            self._working_dir,
            input_mode=self.runtime.input_mode,
            timeout=self.timeout,
            env_extra={"SIMPLECODE_AGENT": self.agent_id},
            cmd_prefix=self.cmd_prefix or None,
            on_output=self._on_output,
        )

    def _write_artifact(self, name: str, content: str) -> Path:
        p = self.output_dir / name
        p.write_text(content)
        return p

    def _write_output_md(self, content: str) -> Path:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        out = self.output_dir / "output.md"
        out.write_text(
            f"# Agent: {self.agent_id}\n\n"
            f"- **Session:** {self.session_id}\n"
            f"- **Runtime:** {self.runtime.name}\n"
            f"- **Model:** {self.model or 'default'}\n"
            f"- **Timestamp:** {now}\n\n"
            f"---\n\n"
            f"## Output\n\n{content}\n"
        )
        return out
