"""simplecode — multi-agent pipeline orchestrator for AI coding agents."""

from importlib.metadata import version

from simplecode.factory import Factory, FactoryItemResult
from simplecode.factory_config import FactoryConfig
from simplecode.factory_progress import FactoryProgressTracker
from simplecode.gate import GateResult
from simplecode.pipeline import AgentConfig, Pipeline, PipelineConfig, PipelineResult
from simplecode.plane import PlaneClient
from simplecode.progress import (
    CompositeProgressCallback,
    LifecycleProgressCallback,
    ProgressCallback,
    compose_progress,
)
from simplecode.registry import PipelineRef, list_pipelines, resolve_pipeline
from simplecode.runner import AgentResult, AgentRunner
from simplecode.store import SessionRow, SessionStore
from simplecode.webhook import Poller, WebhookConfig, WebhookHandler
from simplecode.workspace import WorkspaceInfo, open_workspace

__version__ = version("simplecode")

__all__ = [
    "AgentConfig",
    "AgentResult",
    "AgentRunner",
    "Factory",
    "FactoryConfig",
    "FactoryItemResult",
    "FactoryProgressTracker",
    "GateResult",
    "Pipeline",
    "PipelineConfig",
    "PipelineRef",
    "PipelineResult",
    "PlaneClient",
    "CompositeProgressCallback",
    "LifecycleProgressCallback",
    "ProgressCallback",
    "SessionRow",
    "SessionStore",
    "Poller",
    "WebhookConfig",
    "WebhookHandler",
    "WorkspaceInfo",
    "compose_progress",
    "list_pipelines",
    "open_workspace",
    "resolve_pipeline",
]
