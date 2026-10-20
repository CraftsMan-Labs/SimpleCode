"""Pipeline orchestration — load config, run agents, manage gates."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from simplecode.errors import (
    CheckpointError,
    PipelineConfigError,
    SetupError,
)
from simplecode.gate import Gate, GateResult
from simplecode.progress import ProgressCallback, emit_progress
from simplecode.runner import AgentRunner, discover_prompts
from simplecode.runtime import RUNTIMES, CancelCheck, exec_cli, make_session_id
from simplecode.workspace import finalize_workspace, open_workspace

if TYPE_CHECKING:
    from simplecode.store import SessionStore
    from simplecode.workspace import WorkspaceInfo

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Configuration for a single agent within a pipeline."""

    id: str
    prompts: str
    gate: str | None = None
    permissions: str | None = "write"
    timeout: int | None = 300
    runtime: str | None = None
    model: str | None = None


@dataclass
class PipelineConfig:
    """Full pipeline configuration (from YAML or constructed in Python)."""

    name: str
    description: str = ""
    setup: str | None = None
    agents: list[AgentConfig] = field(default_factory=list)
    branch_strategy: str = "head"
    branch: str | None = None
    docker_image: str | None = None

    @classmethod
    def load(cls, yaml_path: Path) -> PipelineConfig:
        if not yaml_path.exists():
            raise PipelineConfigError(f"Pipeline file not found: {yaml_path}")

        data = yaml.safe_load(yaml_path.read_text())
        if not isinstance(data, dict):
            raise PipelineConfigError(
                f"Invalid pipeline YAML: expected a mapping, got {type(data)}"
            )

        agents_data = data.get("agents", [])
        if not agents_data:
            raise PipelineConfigError("No agents defined in pipeline YAML")

        agents = []
        for i, agent_def in enumerate(agents_data):
            if not isinstance(agent_def, dict):
                raise PipelineConfigError(f"Agent {i} is not a mapping")
            if "id" not in agent_def:
                raise PipelineConfigError(f"Agent {i} missing required 'id' field")
            if "prompts" not in agent_def:
                raise PipelineConfigError(
                    f"Agent '{agent_def['id']}' missing required 'prompts' field"
                )

            agents.append(
                AgentConfig(
                    id=agent_def["id"],
                    prompts=agent_def["prompts"],
                    gate=agent_def.get("gate"),
                    permissions=agent_def.get("permissions"),
                    timeout=agent_def.get("timeout"),
                    runtime=agent_def.get("runtime"),
                    model=agent_def.get("model"),
                )
            )

        return cls(
            name=data.get("name", yaml_path.stem),
            description=data.get("description", ""),
            setup=data.get("setup"),
            agents=agents,
            branch_strategy=data.get("branch_strategy", "head"),
            branch=data.get("branch"),
            docker_image=data.get("docker_image"),
        )

    def validate(self) -> None:
        if not self.agents:
            raise PipelineConfigError("Pipeline has no agents")

        from simplecode.workspace import BRANCH_STRATEGIES

        if self.branch_strategy not in BRANCH_STRATEGIES:
            raise PipelineConfigError(
                f"Invalid branch_strategy '{self.branch_strategy}'. "
                f"Expected one of: {', '.join(BRANCH_STRATEGIES)}"
            )
        if self.branch_strategy == "branch" and not self.branch:
            raise PipelineConfigError("branch_strategy='branch' requires a 'branch' field")
        if self.branch_strategy == "head" and self.branch:
            raise PipelineConfigError("branch= is not valid with branch_strategy='head'")

        seen_ids: set[str] = set()
        for agent in self.agents:
            if agent.id in seen_ids:
                raise PipelineConfigError(f"Duplicate agent ID: '{agent.id}'")
            seen_ids.add(agent.id)

            if agent.gate and agent.gate != "human":
                raise PipelineConfigError(
                    f"Agent '{agent.id}': unsupported gate type "
                    f"'{agent.gate}' (only 'human' is supported)"
                )

            if agent.permissions is not None and agent.permissions not in (
                "read-only",
                "write",
                "full",
            ):
                raise PipelineConfigError(
                    f"Agent '{agent.id}': invalid permissions '{agent.permissions}'"
                )


@dataclass
class PipelineResult:
    """Outcome of a pipeline run."""

    session_id: str
    agents_completed: int
    total_agents: int
    gate_rejected_at: str | None
    output_dir: Path
    status: str = "complete"
    checkpoint_agent: str | None = None
    branch: str | None = None
    branch_strategy: str | None = None
    worktree_path: str | None = None
    docker_image: str | None = None
    merge_result: str | None = None
    preserved_worktree: bool = False


class Pipeline:
    """Orchestrate a sequence of agents with numbered prompts and human gates."""

    def __init__(
        self, config: PipelineConfig, pipeline_dir: Path, pipeline_yaml_path: Path | None = None
    ) -> None:
        self.config = config
        self.pipeline_dir = pipeline_dir.resolve()
        if pipeline_yaml_path:
            self.pipeline_yaml_path = pipeline_yaml_path.resolve()
        else:
            self.pipeline_yaml_path = self.pipeline_dir / "pipeline.yaml"
        self.config.validate()

    def run(
        self,
        runtime: str = "claude",
        working_dir: str | Path = ".",
        input_files: list[str | Path] | None = None,
        gate_mode: str = "stdin",
        output_dir: str | Path = "./pipeline-output",
        timeout: int = 300,
        model: str | None = None,
        permissions: str = "write",
        gate_dir: Path | None = None,
        progress: ProgressCallback | None = None,
        heartbeat_interval: float = 0,
        store: SessionStore | None = None,
        branch_strategy: str | None = None,
        branch: str | None = None,
        docker_image: str | None = None,
        cancel_check: CancelCheck | None = None,
        command_prefix: tuple[str, ...] | list[str] | None = None,
        command_env: dict[str, str] | None = None,
    ) -> PipelineResult:
        """Run with fail-closed boundaries without masking a primary run failure."""
        session_id = make_session_id()
        total_agents = len(self.config.agents)
        try:
            emit_progress(progress, "on_session", session_id)
            emit_progress(progress, "on_pipeline_start", self.config.name, total_agents)
            result = self._run(
                runtime=runtime,
                working_dir=working_dir,
                input_files=input_files,
                gate_mode=gate_mode,
                output_dir=output_dir,
                timeout=timeout,
                model=model,
                permissions=permissions,
                gate_dir=gate_dir,
                progress=progress,
                heartbeat_interval=heartbeat_interval,
                store=store,
                branch_strategy=branch_strategy,
                branch=branch,
                docker_image=docker_image,
                cancel_check=cancel_check,
                command_prefix=command_prefix,
                command_env=command_env,
                _session_id=session_id,
            )
        except Exception as primary_error:
            try:
                emit_progress(
                    progress,
                    "on_pipeline_done",
                    self.config.name,
                    total_agents,
                    "failed",
                )
            except Exception as callback_error:
                primary_error.add_note(
                    f"Suppressed on_pipeline_done callback error: {type(callback_error).__name__}"
                )
            raise
        emit_progress(
            progress,
            "on_pipeline_done",
            self.config.name,
            total_agents,
            result.status,
        )
        return result

    def _run(
        self,
        runtime: str = "claude",
        working_dir: str | Path = ".",
        input_files: list[str | Path] | None = None,
        gate_mode: str = "stdin",
        output_dir: str | Path = "./pipeline-output",
        timeout: int = 300,
        model: str | None = None,
        permissions: str = "write",
        gate_dir: Path | None = None,
        progress: ProgressCallback | None = None,
        heartbeat_interval: float = 0,
        store: SessionStore | None = None,
        branch_strategy: str | None = None,
        branch: str | None = None,
        docker_image: str | None = None,
        cancel_check: CancelCheck | None = None,
        command_prefix: tuple[str, ...] | list[str] | None = None,
        command_env: dict[str, str] | None = None,
        _session_id: str | None = None,
    ) -> PipelineResult:
        session_id = _session_id or make_session_id()
        output_base = Path(output_dir) / session_id
        output_base.mkdir(parents=True, exist_ok=True)

        strategy = branch_strategy or self.config.branch_strategy
        run_branch = branch if branch is not None else self.config.branch
        image = docker_image if docker_image is not None else self.config.docker_image

        logger.info("Pipeline '%s' starting, session %s", self.config.name, session_id)

        with open_workspace(
            working_dir,
            branch_strategy=strategy,
            branch=run_branch,
            docker_image=image,
            session_id=session_id,
        ) as ws_state:
            ws = ws_state.info
            effective_dir = ws.working_dir
            if command_prefix and ws.cmd_prefix:
                raise PipelineConfigError(
                    "command_prefix cannot be combined with a Docker workspace"
                )
            effective_prefix = tuple(command_prefix or ws.cmd_prefix)

            if store is not None:
                store.create_session(
                    session_id=session_id,
                    pipeline_name=self.config.name,
                    pipeline_yaml=str(self.pipeline_yaml_path),
                    runtime=runtime,
                    working_dir=str(effective_dir),
                    output_dir=str(Path(output_dir).resolve()),
                    total_agents=len(self.config.agents),
                    model=model,
                    permissions=permissions,
                    input_files=[str(f) for f in (input_files or [])],
                    timeout=timeout,
                    heartbeat_interval=heartbeat_interval,
                    branch_strategy=ws.branch_strategy,
                    branch=ws.branch,
                    worktree_path=str(ws.worktree_path) if ws.worktree_path else None,
                    docker_image=ws.docker_image,
                    host_repo=str(ws.host_repo),
                )

            if self.config.setup:
                self._run_setup(
                    effective_dir,
                    cmd_prefix=effective_prefix,
                    command_env=command_env,
                )

            context_files: list[str] = []

            if input_files:
                for inp in input_files:
                    inp_path = Path(inp)
                    if not inp_path.is_absolute():
                        # Resolve relative inputs against the host-facing path
                        # the caller passed (not the worktree), then fall back.
                        candidates = [
                            Path(working_dir) / inp_path,
                            ws.host_repo / inp_path,
                            effective_dir / inp_path,
                        ]
                        inp_path = next((c for c in candidates if c.exists()), candidates[0])
                    if inp_path.exists():
                        context_files.append(str(inp_path.resolve()))
                    else:
                        raise PipelineConfigError(f"Input file not found: {inp_path}")

            try:
                result = self._run_agents_from(
                    start_index=0,
                    session_id=session_id,
                    context_files=context_files,
                    agents_completed=0,
                    runtime=runtime,
                    working_dir=effective_dir,
                    output_base=output_base,
                    gate_mode=gate_mode,
                    timeout=timeout,
                    model=model,
                    permissions=permissions,
                    gate_dir=gate_dir,
                    progress=progress,
                    heartbeat_interval=heartbeat_interval,
                    store=store,
                    cmd_prefix=effective_prefix,
                    workspace=ws,
                    cancel_check=cancel_check,
                    command_env=command_env,
                )
            except Exception:
                finalize_workspace(ws_state, success=False)
                raise

            finalize_workspace(
                ws_state,
                success=result.status == "complete",
            )
            return self._attach_workspace(result, ws_state.info)

    def resume(
        self,
        session_id: str,
        store: SessionStore,
        feedback: str | None = None,
        approve: bool = False,
        reject: bool = False,
        gate_mode: str = "stdin",
        gate_dir: Path | None = None,
        progress: ProgressCallback | None = None,
        heartbeat_interval: float | None = None,
    ) -> PipelineResult:
        """Resume a checkpointed pipeline from the DB."""
        session = store.load_session(session_id)

        if session.status != "waiting":
            raise CheckpointError(
                session_id,
                f"Session status is '{session.status}', expected 'waiting'",
            )

        if session.gated_agent_index is None or session.gated_agent_id is None:
            raise CheckpointError(session_id, "No gated agent recorded in checkpoint")

        idx = session.gated_agent_index
        if idx >= len(self.config.agents):
            raise CheckpointError(
                session_id,
                f"Gated agent index {idx} is out of range "
                f"(pipeline has {len(self.config.agents)} agents)",
            )

        agent_cfg = self.config.agents[idx]
        if agent_cfg.id != session.gated_agent_id:
            raise CheckpointError(
                session_id,
                f"Expected agent '{session.gated_agent_id}' at index {idx}, "
                f"found '{agent_cfg.id}'. Pipeline YAML may have changed.",
            )

        runtime_name = agent_cfg.runtime or session.runtime
        agent_runtime = RUNTIMES.get(runtime_name)
        if agent_runtime is None:
            raise PipelineConfigError(f"Unknown runtime '{runtime_name}'")

        hb = heartbeat_interval if heartbeat_interval is not None else session.heartbeat_interval
        output_base = Path(session.output_dir) / session_id

        strategy = session.branch_strategy or self.config.branch_strategy
        resume_repo = session.host_repo or session.working_dir
        resume_wt = session.worktree_path

        with open_workspace(
            resume_repo,
            branch_strategy=strategy,
            branch=session.branch,
            docker_image=session.docker_image,
            session_id=session_id,
            resume_worktree=resume_wt,
        ) as ws_state:
            ws = ws_state.info
            effective_dir = str(ws.working_dir)

            runner = AgentRunner(
                agent_id=agent_cfg.id,
                runtime_config=agent_runtime,
                output_dir=output_base,
                timeout=agent_cfg.timeout or session.timeout,
                session_id=session_id,
                permissions=agent_cfg.permissions or session.permissions,
                model=agent_cfg.model or session.model,
                cmd_prefix=ws.cmd_prefix,
            )
            runner._working_dir = effective_dir

            def _done(result: PipelineResult) -> PipelineResult:
                finalize_workspace(ws_state, success=result.status == "complete")
                return self._attach_workspace(result, ws_state.info)

            if reject:
                store.mark_rejected(session_id, agent_cfg.id)
                return _done(
                    self._finish(
                        output_base,
                        session.runtime,
                        session.model,
                        session_id=session_id,
                        agents_completed=session.agents_completed,
                        total_agents=session.total_agents,
                        status="rejected",
                        gate_rejected_at=agent_cfg.id,
                        workspace=ws,
                    )
                )

            if feedback:
                response = runner.resume_with_feedback(
                    feedback,
                    working_dir=effective_dir,
                    heartbeat_interval=hb,
                )
                runner.write_final_output()

                new_round = session.gate_round + 1
                store.update_after_feedback(session_id, response, new_round)

                if not approve:
                    store.checkpoint(
                        session_id=session_id,
                        gated_agent_id=agent_cfg.id,
                        gated_agent_index=idx,
                        agents_completed=session.agents_completed,
                        context_files=list(session.context_files),
                        last_output=response,
                    )
                    return _done(
                        self._finish(
                            output_base,
                            session.runtime,
                            session.model,
                            session_id=session_id,
                            agents_completed=session.agents_completed,
                            total_agents=session.total_agents,
                            status="checkpointed",
                            checkpoint_agent=agent_cfg.id,
                            workspace=ws,
                        )
                    )

            if approve or feedback:
                store.mark_resumed(session_id)

                context_files = self._append_output_md(list(session.context_files), runner)
                agents_completed = session.agents_completed + 1

                if idx + 1 >= len(self.config.agents):
                    store.mark_completed(session_id, agents_completed)
                    return _done(
                        self._finish(
                            output_base,
                            session.runtime,
                            session.model,
                            session_id=session_id,
                            agents_completed=agents_completed,
                            total_agents=session.total_agents,
                            status="complete",
                            workspace=ws,
                        )
                    )

                try:
                    result = self._run_agents_from(
                        start_index=idx + 1,
                        session_id=session_id,
                        context_files=context_files,
                        agents_completed=agents_completed,
                        runtime=session.runtime,
                        working_dir=effective_dir,
                        output_base=output_base,
                        gate_mode=gate_mode,
                        timeout=session.timeout,
                        model=session.model,
                        permissions=session.permissions,
                        gate_dir=gate_dir,
                        progress=progress,
                        heartbeat_interval=hb,
                        store=store,
                        cmd_prefix=ws.cmd_prefix,
                        workspace=ws,
                    )
                except Exception:
                    finalize_workspace(ws_state, success=False)
                    raise
                return _done(result)

            gate = Gate(output_dir=runner.output_dir)

            def resume_from_checkpoint(feedback: str) -> str:
                return runner.resume_with_feedback(
                    feedback, working_dir=effective_dir, heartbeat_interval=hb
                )

            gate_result = gate.run(
                agent_id=agent_cfg.id,
                last_output=session.last_output or "",
                resume_fn=resume_from_checkpoint,
                gate_mode=gate_mode,
                gate_dir=gate_dir,
            )

            runner.write_final_output()

            if gate_result == GateResult.REJECTED:
                store.mark_rejected(session_id, agent_cfg.id)
                return _done(
                    self._finish(
                        output_base,
                        session.runtime,
                        session.model,
                        session_id=session_id,
                        agents_completed=session.agents_completed,
                        total_agents=session.total_agents,
                        status="rejected",
                        gate_rejected_at=agent_cfg.id,
                        workspace=ws,
                    )
                )

            if gate_result == GateResult.CHECKPOINTED:
                context_files = self._append_output_md(list(session.context_files), runner)
                store.checkpoint(
                    session_id=session_id,
                    gated_agent_id=agent_cfg.id,
                    gated_agent_index=idx,
                    agents_completed=session.agents_completed,
                    context_files=context_files,
                    last_output=runner._latest_output or session.last_output or "",
                )
                return _done(
                    self._finish(
                        output_base,
                        session.runtime,
                        session.model,
                        session_id=session_id,
                        agents_completed=session.agents_completed,
                        total_agents=session.total_agents,
                        status="checkpointed",
                        checkpoint_agent=agent_cfg.id,
                        workspace=ws,
                    )
                )

            store.mark_resumed(session_id)
            context_files = self._append_output_md(list(session.context_files), runner)
            agents_completed = session.agents_completed + 1

            if idx + 1 >= len(self.config.agents):
                store.mark_completed(session_id, agents_completed)
                return _done(
                    self._finish(
                        output_base,
                        session.runtime,
                        session.model,
                        session_id=session_id,
                        agents_completed=agents_completed,
                        total_agents=session.total_agents,
                        status="complete",
                        workspace=ws,
                    )
                )

            try:
                result = self._run_agents_from(
                    start_index=idx + 1,
                    session_id=session_id,
                    context_files=context_files,
                    agents_completed=agents_completed,
                    runtime=session.runtime,
                    working_dir=effective_dir,
                    output_base=output_base,
                    gate_mode=gate_mode,
                    timeout=session.timeout,
                    model=session.model,
                    permissions=session.permissions,
                    gate_dir=gate_dir,
                    progress=progress,
                    heartbeat_interval=hb,
                    store=store,
                    cmd_prefix=ws.cmd_prefix,
                    workspace=ws,
                )
            except Exception:
                finalize_workspace(ws_state, success=False)
                raise
            return _done(result)

    def _run_agents_from(
        self,
        start_index: int,
        session_id: str,
        context_files: list[str],
        agents_completed: int,
        runtime: str,
        working_dir: str | Path,
        output_base: Path,
        gate_mode: str,
        timeout: int,
        model: str | None,
        permissions: str,
        gate_dir: Path | None,
        progress: ProgressCallback | None,
        heartbeat_interval: float,
        store: SessionStore | None = None,
        cmd_prefix: tuple[str, ...] | list[str] | None = None,
        workspace: WorkspaceInfo | None = None,
        cancel_check: CancelCheck | None = None,
        command_env: dict[str, str] | None = None,
    ) -> PipelineResult:
        gate_rejected_at: str | None = None
        prefix = tuple(cmd_prefix) if cmd_prefix else ()

        for i_zero, agent_cfg in enumerate(self.config.agents[start_index:], start_index):
            i = i_zero + 1

            agent_runtime_name = agent_cfg.runtime or runtime
            agent_runtime = RUNTIMES.get(agent_runtime_name)
            if agent_runtime is None:
                raise PipelineConfigError(
                    f"Unknown runtime '{agent_runtime_name}' for agent '{agent_cfg.id}'. "
                    f"Available: {sorted(RUNTIMES)}"
                )

            agent_model = agent_cfg.model or model
            agent_permissions = agent_cfg.permissions or permissions
            agent_timeout = agent_cfg.timeout or timeout

            prompts_dir = self.pipeline_dir / agent_cfg.prompts

            runner = AgentRunner(
                agent_id=agent_cfg.id,
                runtime_config=agent_runtime,
                output_dir=output_base,
                timeout=agent_timeout,
                session_id=session_id,
                permissions=agent_permissions,
                model=agent_model,
                cmd_prefix=prefix,
                command_env=command_env,
                cancel_check=cancel_check,
            )

            prompt_count = len(discover_prompts(prompts_dir))

            emit_progress(
                progress,
                "on_agent_start",
                i,
                agent_cfg.id,
                len(self.config.agents),
                prompt_count,
            )

            logger.info(
                "[%d/%d] Agent '%s' starting (runtime=%s, model=%s)",
                i,
                len(self.config.agents),
                agent_cfg.id,
                agent_runtime_name,
                agent_model or "default",
            )

            result = runner.run(
                prompts_dir=prompts_dir,
                context_files=list(context_files) if context_files else None,
                working_dir=working_dir,
                progress=progress,
                heartbeat_interval=heartbeat_interval,
            )

            if store is not None:
                for prompt_idx in range(1, result.prompt_count + 1):
                    artifact = runner.output_dir / f"{prompt_idx}-output.md"
                    if artifact.exists():
                        store.save_agent_output(
                            session_id, agent_cfg.id, prompt_idx, artifact.read_text()
                        )

            if agent_cfg.gate == "human":
                emit_progress(progress, "on_gate_start", agent_cfg.id)

                gate = Gate(output_dir=runner.output_dir)
                hb = heartbeat_interval

                def resume_after_agent(feedback: str) -> str:
                    return runner.resume_with_feedback(feedback, heartbeat_interval=hb)

                gate_result = gate.run(
                    agent_id=agent_cfg.id,
                    last_output=result.final_output,
                    resume_fn=resume_after_agent,
                    gate_mode=gate_mode,
                    gate_dir=gate_dir,
                )

                if gate_result == GateResult.CHECKPOINTED:
                    runner.write_final_output()
                    cf = self._append_output_md(list(context_files), runner)

                    if store is not None:
                        store.checkpoint(
                            session_id=session_id,
                            gated_agent_id=agent_cfg.id,
                            gated_agent_index=i_zero,
                            agents_completed=agents_completed,
                            context_files=cf,
                            last_output=result.final_output,
                        )

                    return self._finish(
                        output_base,
                        runtime,
                        model,
                        session_id=session_id,
                        agents_completed=agents_completed,
                        total_agents=len(self.config.agents),
                        status="checkpointed",
                        checkpoint_agent=agent_cfg.id,
                        workspace=workspace,
                    )

                runner.write_final_output()

                if gate_result == GateResult.REJECTED:
                    gate_rejected_at = agent_cfg.id
                    logger.info("Pipeline stopped: gate rejected at '%s'", agent_cfg.id)

                    if store is not None:
                        store.mark_rejected(session_id, agent_cfg.id)

                    break

            output_md = str(runner.output_dir / "output.md")
            context_files.append(output_md)
            agents_completed += 1

            if store is not None:
                store.update_progress(session_id, agents_completed)

            emit_progress(
                progress,
                "on_agent_done",
                i,
                agent_cfg.id,
                len(self.config.agents),
            )

            logger.info(
                "[%d/%d] Agent '%s' complete → %s",
                i,
                len(self.config.agents),
                agent_cfg.id,
                output_md,
            )

        status = "rejected" if gate_rejected_at else "complete"

        if store is not None:
            if status == "complete":
                store.mark_completed(session_id, agents_completed)

        return self._finish(
            output_base,
            runtime,
            model,
            session_id=session_id,
            agents_completed=agents_completed,
            total_agents=len(self.config.agents),
            status=status,
            gate_rejected_at=gate_rejected_at,
            workspace=workspace,
        )

    def _run_setup(
        self,
        working_dir: str | Path,
        cmd_prefix: tuple[str, ...] | list[str] | None = None,
        command_env: dict[str, str] | None = None,
    ) -> None:
        assert self.config.setup is not None
        setup_path = self.pipeline_dir / self.config.setup
        if not setup_path.exists():
            raise SetupError(str(setup_path), -1, "Setup script not found")

        logger.info("Running setup: %s", setup_path)
        prefix = list(cmd_prefix) if cmd_prefix else []
        if prefix:
            try:
                exec_cli(
                    ["sh", "-s"],
                    setup_path.read_text(),
                    working_dir,
                    input_mode="stdin",
                    timeout=600,
                    env_extra=command_env,
                    cmd_prefix=prefix,
                )
            except Exception as exc:
                raise SetupError(str(setup_path), -1, str(exc)) from exc
            logger.info("Remote setup complete")
            return
        try:
            result = subprocess.run(
                [str(setup_path)],
                cwd=str(working_dir),
                capture_output=True,
                text=True,
                timeout=600,
            )
        except FileNotFoundError:
            raise SetupError(str(setup_path), -1, "Script not executable or not found")
        except subprocess.TimeoutExpired:
            raise SetupError(str(setup_path), -1, "Setup timed out after 600s")

        if result.returncode != 0:
            raise SetupError(str(setup_path), result.returncode, result.stderr)

        logger.info("Setup complete")

    @staticmethod
    def _append_output_md(context_files: list[str], runner: AgentRunner) -> list[str]:
        output_md = str(runner.output_dir / "output.md")
        if output_md not in context_files:
            context_files.append(output_md)
        return context_files

    @staticmethod
    def _attach_workspace(result: PipelineResult, ws: WorkspaceInfo) -> PipelineResult:
        result.branch = ws.branch
        result.branch_strategy = ws.branch_strategy
        result.worktree_path = str(ws.worktree_path) if ws.worktree_path else None
        result.docker_image = ws.docker_image
        result.merge_result = ws.merge_result
        result.preserved_worktree = ws.preserved
        # Refresh metadata file with workspace fields.
        meta_path = result.output_dir / "pipeline.json"
        if meta_path.exists():
            try:
                data = json.loads(meta_path.read_text())
                data["branch"] = result.branch
                data["branch_strategy"] = result.branch_strategy
                data["worktree_path"] = result.worktree_path
                data["docker_image"] = result.docker_image
                data["merge_result"] = result.merge_result
                data["preserved_worktree"] = result.preserved_worktree
                meta_path.write_text(json.dumps(data, indent=2) + "\n")
            except (json.JSONDecodeError, OSError):
                pass
        return result

    def _finish(
        self,
        output_dir: Path,
        runtime: str,
        model: str | None,
        *,
        session_id: str,
        agents_completed: int,
        total_agents: int,
        status: str,
        gate_rejected_at: str | None = None,
        checkpoint_agent: str | None = None,
        workspace: WorkspaceInfo | None = None,
    ) -> PipelineResult:
        result = PipelineResult(
            session_id=session_id,
            agents_completed=agents_completed,
            total_agents=total_agents,
            gate_rejected_at=gate_rejected_at,
            output_dir=output_dir,
            status=status,
            checkpoint_agent=checkpoint_agent,
            branch=workspace.branch if workspace else None,
            branch_strategy=workspace.branch_strategy if workspace else None,
            worktree_path=(
                str(workspace.worktree_path) if workspace and workspace.worktree_path else None
            ),
            docker_image=workspace.docker_image if workspace else None,
            merge_result=workspace.merge_result if workspace else None,
            preserved_worktree=workspace.preserved if workspace else False,
        )
        self._write_metadata(output_dir, result, runtime, model)
        return result

    def _write_metadata(
        self,
        output_dir: Path,
        result: PipelineResult,
        runtime: str,
        model: str | None,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        metadata = {
            "pipeline": self.config.name,
            "description": self.config.description,
            "session_id": result.session_id,
            "runtime": runtime,
            "model": model or "default",
            "agents_completed": result.agents_completed,
            "total_agents": result.total_agents,
            "gate_rejected_at": result.gate_rejected_at,
            "status": result.status,
            "timestamp": now,
            "branch": result.branch,
            "branch_strategy": result.branch_strategy,
            "worktree_path": result.worktree_path,
            "docker_image": result.docker_image,
            "merge_result": result.merge_result,
            "preserved_worktree": result.preserved_worktree,
            "agents": [
                {
                    "id": a.id,
                    "prompts": a.prompts,
                    "gate": a.gate,
                    "permissions": a.permissions,
                    "model": a.model,
                    "timeout": a.timeout,
                    "runtime": a.runtime,
                }
                for a in self.config.agents
            ],
        }
        if result.checkpoint_agent:
            metadata["checkpoint_agent"] = result.checkpoint_agent

        meta_path = output_dir / "pipeline.json"
        meta_path.write_text(json.dumps(metadata, indent=2) + "\n")
