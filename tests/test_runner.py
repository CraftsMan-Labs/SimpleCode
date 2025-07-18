"""Tests for AgentRunner and prompt discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from simplecode.errors import PipelineConfigError
from simplecode.runner import discover_prompts


def test_discover_prompts_sorted(tmp_path: Path) -> None:
    (tmp_path / "3.md").write_text("third")
    (tmp_path / "1.md").write_text("first")
    (tmp_path / "2.md").write_text("second")

    prompts = discover_prompts(tmp_path)

    assert [p.name for p in prompts] == ["1.md", "2.md", "3.md"]


def test_discover_prompts_ignores_non_numbered(tmp_path: Path) -> None:
    (tmp_path / "1.md").write_text("one")
    (tmp_path / "2.md").write_text("two")
    (tmp_path / "README.md").write_text("ignore me")
    (tmp_path / "notes.txt").write_text("ignore me too")

    prompts = discover_prompts(tmp_path)

    assert [p.name for p in prompts] == ["1.md", "2.md"]


def test_discover_prompts_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(PipelineConfigError, match="not found"):
        discover_prompts(tmp_path / "nonexistent")


def test_discover_prompts_empty_dir(tmp_path: Path) -> None:
    with pytest.raises(PipelineConfigError, match="No numbered prompts"):
        discover_prompts(tmp_path)


def test_discover_prompts_gap_in_numbering(tmp_path: Path) -> None:
    (tmp_path / "1.md").write_text("one")
    (tmp_path / "3.md").write_text("three")

    with pytest.raises(PipelineConfigError, match="Gap in prompt numbering"):
        discover_prompts(tmp_path)
