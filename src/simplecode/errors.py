"""Exception hierarchy for simplecode pipeline orchestration."""

from __future__ import annotations


class SimpleCodeError(Exception):
    """Base exception for all simplecode errors."""


class PipelineConfigError(SimpleCodeError):
    """Invalid pipeline configuration (bad YAML, missing prompts directory, etc.)."""


class SetupError(SimpleCodeError):
    """Pipeline setup script (setup.sh) failed."""

    def __init__(self, script: str, exit_code: int, stderr: str) -> None:
        self.script = script
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(f"Setup script '{script}' failed with exit code {exit_code}:\n{stderr}")


class AgentError(SimpleCodeError):
    """Agent CLI invocation failed."""

    def __init__(self, runtime: str, cmd: list[str], exit_code: int, stderr: str) -> None:
        self.runtime = runtime
        self.cmd = cmd
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(
            f"Agent runtime '{runtime}' failed (exit {exit_code}): {' '.join(cmd)}\n{stderr}"
        )


class GateRejectedError(SimpleCodeError):
    """Human rejected the pipeline at a gate checkpoint."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"Pipeline rejected by human at gate after agent '{agent_id}'")


class RuntimeNotFoundError(SimpleCodeError):
    """Agent CLI binary not found on PATH."""

    def __init__(self, binary: str) -> None:
        self.binary = binary
        super().__init__(
            f"Runtime binary '{binary}' not found on PATH. Install it or check your environment."
        )


class CheckpointNotFoundError(SimpleCodeError):
    """No checkpoint found for the given session."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"No waiting checkpoint found for session '{session_id}'")


class CheckpointError(SimpleCodeError):
    """Checkpoint is in an unexpected state or pipeline has changed."""

    def __init__(self, session_id: str, reason: str) -> None:
        self.session_id = session_id
        self.reason = reason
        super().__init__(f"Checkpoint error (session '{session_id}'): {reason}")


class StoreError(SimpleCodeError):
    """Database connection or query failure."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
