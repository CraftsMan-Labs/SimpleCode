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
