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
from simplecode.progress import ProgressCallback
from simplecode.runner import AgentRunner, discover_prompts
from simplecode.runtime import RUNTIMES, make_session_id

if TYPE_CHECKING:
    from simplecode.store import SessionStore

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
        )

    def validate(self) -> None:
        if not self.agents:
            raise PipelineConfigError("Pipeline has no agents")

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
    ) -> PipelineResult:
        session_id = make_session_id()
        output_base = Path(output_dir) / session_id
        output_base.mkdir(parents=True, exist_ok=True)

        logger.info("Pipeline '%s' starting, session %s", self.config.name, session_id)

        if progress:
            progress.on_pipeline_start(self.config.name, len(self.config.agents))

        if store is not None:
            store.create_session(
                session_id=session_id,
                pipeline_name=self.config.name,
                pipeline_yaml=str(self.pipeline_yaml_path),
                runtime=runtime,
                working_dir=str(Path(working_dir).resolve()),
                output_dir=str(Path(output_dir).resolve()),
                total_agents=len(self.config.agents),
                model=model,
                permissions=permissions,
                input_files=[str(f) for f in (input_files or [])],
                timeout=timeout,
                heartbeat_interval=heartbeat_interval,
            )

        if self.config.setup:
            self._run_setup(working_dir)

        context_files: list[str] = []

        if input_files:
            for inp in input_files:
                inp_path = Path(inp)
                if not inp_path.is_absolute():
                    inp_path = Path(working_dir) / inp_path
                if inp_path.exists():
                    context_files.append(str(inp_path))
                else:
                    raise PipelineConfigError(f"Input file not found: {inp_path}")

        return self._run_agents_from(
            start_index=0,
            session_id=session_id,
            context_files=context_files,
            agents_completed=0,
            runtime=runtime,
            working_dir=working_dir,
            output_base=output_base,
            gate_mode=gate_mode,
            timeout=timeout,
            model=model,
            permissions=permissions,
            gate_dir=gate_dir,
            progress=progress,
            heartbeat_interval=heartbeat_interval,
            store=store,
        )

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

        runner = AgentRunner(
            agent_id=agent_cfg.id,
            runtime_config=agent_runtime,
            output_dir=output_base,
            timeout=agent_cfg.timeout or session.timeout,
            session_id=session_id,
            permissions=agent_cfg.permissions or session.permissions,
            model=agent_cfg.model or session.model,
        )
        runner._working_dir = session.working_dir

        if reject:
            store.mark_rejected(session_id, agent_cfg.id)
            return self._finish(
                output_base,
                session.runtime,
                session.model,
                session_id=session_id,
                agents_completed=session.agents_completed,
                total_agents=session.total_agents,
                status="rejected",
                gate_rejected_at=agent_cfg.id,
            )

        if feedback:
            response = runner.resume_with_feedback(
                feedback,
                working_dir=session.working_dir,
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
                return self._finish(
                    output_base,
                    session.runtime,
                    session.model,
                    session_id=session_id,
                    agents_completed=session.agents_completed,
                    total_agents=session.total_agents,
                    status="checkpointed",
                    checkpoint_agent=agent_cfg.id,
                )

        if approve or feedback:
            store.mark_resumed(session_id)

            context_files = self._append_output_md(list(session.context_files), runner)
            agents_completed = session.agents_completed + 1

            if idx + 1 >= len(self.config.agents):
                store.mark_completed(session_id, agents_completed)
                return self._finish(
                    output_base,
                    session.runtime,
                    session.model,
                    session_id=session_id,
                    agents_completed=agents_completed,
                    total_agents=session.total_agents,
                    status="complete",
                )

            return self._run_agents_from(
                start_index=idx + 1,
                session_id=session_id,
                context_files=context_files,
                agents_completed=agents_completed,
                runtime=session.runtime,
                working_dir=session.working_dir,
                output_base=output_base,
                gate_mode=gate_mode,
                timeout=session.timeout,
                model=session.model,
                permissions=session.permissions,
                gate_dir=gate_dir,
                progress=progress,
                heartbeat_interval=hb,
                store=store,
            )

        gate = Gate(output_dir=runner.output_dir)

        def resume_from_checkpoint(feedback: str) -> str:
            return runner.resume_with_feedback(
                feedback, working_dir=session.working_dir, heartbeat_interval=hb
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
            return self._finish(
                output_base,
                session.runtime,
                session.model,
                session_id=session_id,
                agents_completed=session.agents_completed,
                total_agents=session.total_agents,
                status="rejected",
                gate_rejected_at=agent_cfg.id,
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
            return self._finish(
                output_base,
                session.runtime,
                session.model,
                session_id=session_id,
                agents_completed=session.agents_completed,
                total_agents=session.total_agents,
                status="checkpointed",
                checkpoint_agent=agent_cfg.id,
            )

        store.mark_resumed(session_id)
        context_files = self._append_output_md(list(session.context_files), runner)
        agents_completed = session.agents_completed + 1

        if idx + 1 >= len(self.config.agents):
            store.mark_completed(session_id, agents_completed)
            return self._finish(
                output_base,
                session.runtime,
                session.model,
                session_id=session_id,
                agents_completed=agents_completed,
                total_agents=session.total_agents,
                status="complete",
            )

        return self._run_agents_from(
            start_index=idx + 1,
            session_id=session_id,
            context_files=context_files,
            agents_completed=agents_completed,
            runtime=session.runtime,
            working_dir=session.working_dir,
            output_base=output_base,
            gate_mode=gate_mode,
            timeout=session.timeout,
            model=session.model,
            permissions=session.permissions,
            gate_dir=gate_dir,
            progress=progress,
            heartbeat_interval=hb,
            store=store,
        )

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
    ) -> PipelineResult:
        gate_rejected_at: str | None = None

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
            )

            prompt_count = len(discover_prompts(prompts_dir))

            if progress:
                progress.on_agent_start(
                    i, agent_cfg.id, len(self.config.agents), prompt_count
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
                if progress:
                    progress.on_gate_start(agent_cfg.id)

                gate = Gate(output_dir=runner.output_dir)
                hb = heartbeat_interval

                def resume_after_agent(feedback: str) -> str:
                    return runner.resume_with_feedback(
                        feedback, heartbeat_interval=hb
                    )

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

            if progress:
                progress.on_agent_done(i, agent_cfg.id, len(self.config.agents))

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
            # rejected is already handled in the gate block above

        return self._finish(
            output_base,
            runtime,
            model,
            session_id=session_id,
            agents_completed=agents_completed,
            total_agents=len(self.config.agents),
            status=status,
            gate_rejected_at=gate_rejected_at,
        )

    def _run_setup(self, working_dir: str | Path) -> None:
        assert self.config.setup is not None
        setup_path = self.pipeline_dir / self.config.setup
        if not setup_path.exists():
            raise SetupError(str(setup_path), -1, "Setup script not found")

        logger.info("Running setup: %s", setup_path)
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
    ) -> PipelineResult:
        result = PipelineResult(
            session_id=session_id,
            agents_completed=agents_completed,
            total_agents=total_agents,
            gate_rejected_at=gate_rejected_at,
            output_dir=output_dir,
            status=status,
            checkpoint_agent=checkpoint_agent,
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
