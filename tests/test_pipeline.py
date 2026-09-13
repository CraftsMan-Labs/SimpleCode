"""Tests for pipeline configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from simplecode.errors import PipelineConfigError
from simplecode.pipeline import AgentConfig, PipelineConfig


def test_pipeline_config_load(tmp_path: Path) -> None:
    yaml_content = """\
name: test-pipeline
description: A test pipeline

agents:
  - id: planner
    prompts: prompts/planner/
    gate: human
    permissions: read-only
    model: opus

  - id: executor
    prompts: prompts/executor/
    permissions: write
"""
    yaml_file = tmp_path / "pipeline.yaml"
    yaml_file.write_text(yaml_content)

    config = PipelineConfig.load(yaml_file)

    assert config.name == "test-pipeline"
    assert config.description == "A test pipeline"
    assert len(config.agents) == 2
    assert config.agents[0].id == "planner"
    assert config.agents[0].gate == "human"
    assert config.agents[0].model == "opus"
    assert config.agents[1].id == "executor"
    assert config.agents[1].gate is None
    assert config.agents[1].model is None
    assert config.agents[0].permissions == "read-only"
    assert config.agents[1].permissions == "write"
    assert config.agents[0].timeout is None
    assert config.agents[1].timeout is None


def test_pipeline_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PipelineConfigError, match="not found"):
        PipelineConfig.load(tmp_path / "missing.yaml")


def test_pipeline_config_no_agents(tmp_path: Path) -> None:
    yaml_file = tmp_path / "pipeline.yaml"
    yaml_file.write_text("name: empty\n")

    with pytest.raises(PipelineConfigError, match="No agents"):
        PipelineConfig.load(yaml_file)


def test_pipeline_config_missing_id(tmp_path: Path) -> None:
    yaml_file = tmp_path / "pipeline.yaml"
    yaml_file.write_text("agents:\n  - prompts: prompts/a/\n")

    with pytest.raises(PipelineConfigError, match="missing required 'id'"):
        PipelineConfig.load(yaml_file)


def test_pipeline_config_duplicate_ids() -> None:
    config = PipelineConfig(
        name="dup",
        agents=[
            AgentConfig(id="a", prompts="p/a/"),
            AgentConfig(id="a", prompts="p/b/"),
        ],
    )
    with pytest.raises(PipelineConfigError, match="Duplicate agent ID"):
        config.validate()


def test_pipeline_config_invalid_gate() -> None:
    config = PipelineConfig(
        name="bad-gate",
        agents=[AgentConfig(id="a", prompts="p/a/", gate="auto")],
    )
    with pytest.raises(PipelineConfigError, match="unsupported gate type"):
        config.validate()


def test_pipeline_config_programmatic() -> None:
    config = PipelineConfig(
        name="programmatic",
        description="Built in Python",
        agents=[
            AgentConfig(id="a", prompts="p/a/", model="opus", gate="human"),
            AgentConfig(id="b", prompts="p/b/", model="sonnet"),
        ],
    )
    config.validate()
    assert config.agents[0].model == "opus"
    assert config.agents[1].model == "sonnet"
    assert config.agents[0].timeout == 300
    assert config.agents[0].permissions == "write"


def test_pipeline_config_branch_strategy(tmp_path: Path) -> None:
    yaml_file = tmp_path / "pipeline.yaml"
    yaml_file.write_text(
        """\
name: isolated
branch_strategy: branch
branch: agent/demo
docker_image: sandcastle:local
agents:
  - id: worker
    prompts: prompts/worker/
"""
    )
    config = PipelineConfig.load(yaml_file)
    config.validate()
    assert config.branch_strategy == "branch"
    assert config.branch == "agent/demo"
    assert config.docker_image == "sandcastle:local"


def test_pipeline_config_branch_requires_name() -> None:
    config = PipelineConfig(
        name="bad",
        branch_strategy="branch",
        agents=[AgentConfig(id="a", prompts="p/a/")],
    )
    with pytest.raises(PipelineConfigError, match="requires a 'branch'"):
        config.validate()


def test_pipeline_config_head_rejects_branch() -> None:
    config = PipelineConfig(
        name="bad",
        branch_strategy="head",
        branch="nope",
        agents=[AgentConfig(id="a", prompts="p/a/")],
    )
    with pytest.raises(PipelineConfigError, match="not valid"):
        config.validate()


def test_yaml_omitted_timeout_and_permissions_are_none(tmp_path: Path) -> None:
    yaml_file = tmp_path / "pipeline.yaml"
    yaml_file.write_text("agents:\n  - id: a\n    prompts: prompts/a/\n")

    config = PipelineConfig.load(yaml_file)
    config.validate()

    assert config.agents[0].timeout is None
    assert config.agents[0].permissions is None


def test_yaml_explicit_timeout_wins_over_run_default(tmp_path: Path) -> None:
    yaml_file = tmp_path / "pipeline.yaml"
    yaml_file.write_text(
        "agents:\n"
        "  - id: a\n"
        "    prompts: prompts/a/\n"
        "    timeout: 120\n"
        "    permissions: read-only\n"
    )

    config = PipelineConfig.load(yaml_file)
    assert config.agents[0].timeout == 120
    assert config.agents[0].permissions == "read-only"
