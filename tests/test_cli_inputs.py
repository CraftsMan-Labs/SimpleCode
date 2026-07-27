"""Checks for lazy run-input resolution."""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from simplecode.cli import _resolve_run_inputs


def test_positional_file(tmp_path: Path) -> None:
    other = tmp_path / "OTHER.md"
    other.write_text("y")
    (tmp_path / "TASK.md").write_text("x")
    out = tmp_path / "out"
    assert _resolve_run_inputs(str(tmp_path), (str(other),), (), blob_dir=out) == [
        str(other.resolve())
    ]


def test_relative_file_under_working_dir(tmp_path: Path) -> None:
    issue = tmp_path / "ISSUE.md"
    issue.write_text("bug\n")
    out = tmp_path / "out"
    assert _resolve_run_inputs(str(tmp_path), ("ISSUE.md",), (), blob_dir=out) == [
        str(issue.resolve())
    ]


def test_inline_text_blob(tmp_path: Path) -> None:
    out = tmp_path / "out"
    paths = _resolve_run_inputs(
        str(tmp_path),
        ('Fix login timeout for SSO users\n\nSteps: ...',),
        (),
        blob_dir=out,
    )
    assert paths is not None and len(paths) == 1
    text = Path(paths[0]).read_text()
    assert "Fix login timeout" in text
    assert paths[0].endswith("_input_1.md")


def test_auto_task_md(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("x")
    assert _resolve_run_inputs(str(tmp_path), (), (), blob_dir=tmp_path / "out") == [
        str(task.resolve())
    ]


def test_auto_issue_md(tmp_path: Path) -> None:
    issue = tmp_path / "ISSUE.md"
    issue.write_text("bug\n")
    assert _resolve_run_inputs(str(tmp_path), (), (), blob_dir=tmp_path / "out") == [
        str(issue.resolve())
    ]


def test_none_without_defaults(tmp_path: Path) -> None:
    assert _resolve_run_inputs(str(tmp_path), (), (), blob_dir=tmp_path / "out") is None


def test_directory_rejected(tmp_path: Path) -> None:
    nested = tmp_path / "dir"
    nested.mkdir()
    with pytest.raises(click.ClickException, match="directory"):
        _resolve_run_inputs(str(tmp_path), (str(nested),), (), blob_dir=tmp_path / "out")
