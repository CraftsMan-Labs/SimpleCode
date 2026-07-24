"""Checks for lazy run-input resolution."""

from __future__ import annotations

from pathlib import Path

from simplecode.cli import _resolve_run_inputs


def test_positional_wins(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("x")
    other = tmp_path / "OTHER.md"
    other.write_text("y")
    assert _resolve_run_inputs(str(tmp_path), (str(other),), ()) == [str(other)]


def test_auto_task_md(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("x")
    assert _resolve_run_inputs(str(tmp_path), (), ()) == [str(task)]


def test_none_without_task(tmp_path: Path) -> None:
    assert _resolve_run_inputs(str(tmp_path), (), ()) is None
