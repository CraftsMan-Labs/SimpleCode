"""Validate the unattended factory-ticket-delivery example."""

from __future__ import annotations

from pathlib import Path

from simplecode.pipeline import PipelineConfig
from simplecode.runner import discover_prompts

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "factory-ticket-delivery"

EXPECTED_AGENTS = (
    ("explorer", "read-only"),
    ("builder", "write"),
    ("adversarial", "read-only"),
    ("fixer", "write"),
    ("qa", "read-only"),
)

PROMPT_COUNTS = {
    "explorer": 2,
    "builder": 2,
    "adversarial": 2,
    "fixer": 2,
    "qa": 2,
}

VERDICT_PASS = "FACTORY_VERDICT: PASS"
VERDICT_BLOCKED = "FACTORY_VERDICT: BLOCKED"


def _load_config() -> PipelineConfig:
    config = PipelineConfig.load(EXAMPLE_DIR / "pipeline.yaml")
    config.validate()
    return config


def _prompt_text(agent_id: str) -> str:
    prompts_dir = EXAMPLE_DIR / "prompts" / agent_id
    return "\n".join(path.read_text() for path in discover_prompts(prompts_dir))


def _last_prompt_text(agent_id: str) -> str:
    prompts = discover_prompts(EXAMPLE_DIR / "prompts" / agent_id)
    return prompts[-1].read_text()


def test_factory_pipeline_structure() -> None:
    config = _load_config()

    assert config.name == "factory-ticket-delivery"
    assert [agent.id for agent in config.agents] == [item[0] for item in EXPECTED_AGENTS]
    assert (EXAMPLE_DIR / "README.md").is_file()
    assert (EXAMPLE_DIR / "TICKET.md").is_file()
    assert (EXAMPLE_DIR / "README.md").read_text().strip()
    assert (EXAMPLE_DIR / "TICKET.md").read_text().strip()

    for agent, (agent_id, _) in zip(config.agents, EXPECTED_AGENTS, strict=True):
        prompts_dir = EXAMPLE_DIR / agent.prompts
        prompts = discover_prompts(prompts_dir)
        assert len(prompts) == PROMPT_COUNTS[agent_id]
        assert [prompt.name for prompt in prompts] == [
            f"{index}.md" for index in range(1, len(prompts) + 1)
        ]
        assert all(prompt.read_text().strip() for prompt in prompts)


def test_factory_pipeline_permissions_and_no_gates() -> None:
    config = _load_config()
    raw_yaml = (EXAMPLE_DIR / "pipeline.yaml").read_text()

    assert "gate:" not in raw_yaml
    assert all(agent.gate is None for agent in config.agents)

    for agent, (agent_id, permissions) in zip(config.agents, EXPECTED_AGENTS, strict=True):
        assert agent.id == agent_id
        assert agent.permissions == permissions


def test_builder_and_fixer_must_test_and_commit() -> None:
    for agent_id in ("builder", "fixer"):
        text = _prompt_text(agent_id).lower()
        assert "run" in text and "test" in text
        assert "commit" in text
        assert "commit hash" in text


def test_adversarial_output_is_actionable() -> None:
    text = _prompt_text("adversarial").lower()
    last = _last_prompt_text("adversarial").lower()

    assert "actionable" in last or "must-fix" in last
    assert "file path" in text or "exact file path" in last
    assert "verify" in last
    assert "change" in last
    assert "ADVERSARIAL STATUS: CLEAR" in _prompt_text("adversarial")
    assert "ADVERSARIAL STATUS: FINDINGS" in _prompt_text("adversarial")


def test_final_qa_prompt_verdict_contract() -> None:
    last = _last_prompt_text("qa")
    last_stripped = last.strip()

    assert VERDICT_PASS in last
    assert VERDICT_BLOCKED in last
    assert "last line" in last.lower()
    assert "exactly" in last.lower()
    assert "evidence" in last.lower()
    assert "QA VERDICT:" not in last
    assert f"\n{VERDICT_PASS}\n" in last
    assert f"\n{VERDICT_BLOCKED}\n" in last

    # The contract is on the final QA prompt, not buried only in earlier rubric text.
    assert VERDICT_PASS not in _prompt_text("explorer")
    assert VERDICT_PASS in last_stripped
