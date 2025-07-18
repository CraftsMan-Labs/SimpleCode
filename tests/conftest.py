"""Shared fixtures for simplecode tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def pipeline_dir(tmp_path: Path) -> Path:
    """Create a minimal pipeline directory with YAML and numbered prompts."""
    prompts_a = tmp_path / "prompts" / "agent-a"
    prompts_a.mkdir(parents=True)
    (prompts_a / "1.md").write_text("Describe the project structure in one paragraph.")
    (prompts_a / "2.md").write_text("List the top 3 files by importance.")

    prompts_b = tmp_path / "prompts" / "agent-b"
    prompts_b.mkdir(parents=True)
    (prompts_b / "1.md").write_text("Based on the context, suggest one improvement.")

    yaml_content = """\
name: test-pipeline
description: A minimal test pipeline

agents:
  - id: agent-a
    prompts: prompts/agent-a/
    permissions: read-only
    timeout: 120

  - id: agent-b
    prompts: prompts/agent-b/
    permissions: read-only
    timeout: 120
"""
    (tmp_path / "pipeline.yaml").write_text(yaml_content)
    return tmp_path


@pytest.fixture()
def gated_pipeline_dir(tmp_path: Path) -> Path:
    """Create a pipeline with a human gate on the first agent."""
    prompts_a = tmp_path / "prompts" / "planner"
    prompts_a.mkdir(parents=True)
    (prompts_a / "1.md").write_text("List all files in this project.")

    prompts_b = tmp_path / "prompts" / "executor"
    prompts_b.mkdir(parents=True)
    (prompts_b / "1.md").write_text("Based on the context, echo back the plan.")

    yaml_content = """\
name: gated-pipeline
description: Pipeline with a human gate

agents:
  - id: planner
    prompts: prompts/planner/
    gate: human
    permissions: read-only
    timeout: 120

  - id: executor
    prompts: prompts/executor/
    permissions: read-only
    timeout: 120
"""
    (tmp_path / "pipeline.yaml").write_text(yaml_content)
    return tmp_path


@pytest.fixture()
def setup_pipeline_dir(tmp_path: Path) -> Path:
    """Create a pipeline with a setup.sh script."""
    prompts = tmp_path / "prompts" / "agent"
    prompts.mkdir(parents=True)
    (prompts / "1.md").write_text("Say hello.")

    setup = tmp_path / "setup.sh"
    setup.write_text("#!/usr/bin/env bash\nset -euo pipefail\necho 'setup done'\n")
    setup.chmod(0o755)

    yaml_content = """\
name: setup-pipeline
description: Pipeline with setup script
setup: setup.sh

agents:
  - id: agent
    prompts: prompts/agent/
    permissions: read-only
    timeout: 120
"""
    (tmp_path / "pipeline.yaml").write_text(yaml_content)
    return tmp_path
