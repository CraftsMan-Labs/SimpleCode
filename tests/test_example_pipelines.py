"""Validate the factory example pipelines ship with coherent YAML + prompts."""

from __future__ import annotations

from pathlib import Path

import pytest

from simplecode.pipeline import PipelineConfig
from simplecode.runner import discover_prompts

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"

FACTORY_PIPELINES = {
    "spec-to-tickets": {
        "agents": [
            ("spec-interviewer", "read-only", "human"),
            ("ticket-writer", "read-only", None),
        ],
        "prompt_counts": {
            "spec-interviewer": 3,
            "ticket-writer": 2,
        },
    },
    "ticket-delivery": {
        "agents": [
            ("explorer", "read-only", None),
            ("builder", "write", None),
            ("adversarial", "read-only", None),
            ("qa", "read-only", "human"),
        ],
        "prompt_counts": {
            "explorer": 2,
            "builder": 2,
            "adversarial": 1,
            "qa": 2,
        },
    },
}


@pytest.mark.parametrize("name", sorted(FACTORY_PIPELINES))
def test_factory_example_pipeline_shape(name: str) -> None:
    expected = FACTORY_PIPELINES[name]
    pipeline_dir = EXAMPLES_DIR / name
    yaml_path = pipeline_dir / "pipeline.yaml"

    config = PipelineConfig.load(yaml_path)
    config.validate()

    assert config.name == name
    assert [agent.id for agent in config.agents] == [item[0] for item in expected["agents"]]

    for agent, (agent_id, permissions, gate) in zip(
        config.agents, expected["agents"], strict=True
    ):
        assert agent.id == agent_id
        assert agent.permissions == permissions
        assert agent.gate == gate

        prompts_dir = pipeline_dir / agent.prompts
        prompts = discover_prompts(prompts_dir)
        assert len(prompts) == expected["prompt_counts"][agent_id]
        assert [prompt.name for prompt in prompts] == [
            f"{index}.md" for index in range(1, len(prompts) + 1)
        ]
        assert all(prompt.read_text().strip() for prompt in prompts)


def test_factory_example_input_templates_exist() -> None:
    assert (EXAMPLES_DIR / "spec-to-tickets" / "TASK.md").is_file()
    assert (EXAMPLES_DIR / "spec-to-tickets" / "README.md").is_file()
    assert (EXAMPLES_DIR / "ticket-delivery" / "TICKET.md").is_file()
    assert (EXAMPLES_DIR / "ticket-delivery" / "README.md").is_file()
