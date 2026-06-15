"""simplecode — multi-agent pipeline orchestrator for AI coding agents."""

from importlib.metadata import version

from simplecode.gate import GateResult
from simplecode.pipeline import AgentConfig, Pipeline, PipelineConfig, PipelineResult
from simplecode.progress import ProgressCallback
from simplecode.runner import AgentResult, AgentRunner
from simplecode.store import SessionRow, SessionStore
from simplecode.webhook import Poller, WebhookConfig, WebhookHandler

__version__ = version("simplecode")

__all__ = [
    "AgentConfig",
    "AgentResult",
    "AgentRunner",
    "GateResult",
    "Pipeline",
    "PipelineConfig",
    "PipelineResult",
    "ProgressCallback",
    "SessionRow",
    "SessionStore",
    "Poller",
    "WebhookConfig",
    "WebhookHandler",
]
