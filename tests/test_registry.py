"""Tests for .simplecode project/global pipeline registry."""

from __future__ import annotations

from pathlib import Path

import pytest

import simplecode.registry as registry
from simplecode.errors import PipelineConfigError
from simplecode.registry import (
    ensure_project_pipelines_dir,
    find_project_root,
    list_pipelines,
    resolve_pipeline,
)


def _write_pipeline(dir_path: Path, name: str = "demo") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    yaml_path = dir_path / "pipeline.yaml"
    yaml_path.write_text(
        f"name: {name}\n"
        "description: test\n"
        "agents:\n"
        "  - id: planner\n"
        "    prompts: prompts/planner/\n"
    )
    (dir_path / "prompts" / "planner").mkdir(parents=True, exist_ok=True)
    (dir_path / "prompts" / "planner" / "1.md").write_text("hello\n")
    return yaml_path


def test_ensure_project_pipelines_dir(tmp_path: Path) -> None:
    pipelines = ensure_project_pipelines_dir(tmp_path)
    assert pipelines == tmp_path / ".simplecode" / "pipelines"
    assert pipelines.is_dir()
    assert find_project_root(tmp_path) == tmp_path


def test_find_project_root_walks_up(tmp_path: Path) -> None:
    ensure_project_pipelines_dir(tmp_path)
    nested = tmp_path / "src" / "app"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == tmp_path


def test_resolve_project_before_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_root = tmp_path / "home" / ".simplecode"
    monkeypatch.setattr(registry, "GLOBAL_ROOT", global_root)

    project = tmp_path / "repo"
    project.mkdir()
    _write_pipeline(project / ".simplecode" / "pipelines" / "feature-dev", "project-one")
    _write_pipeline(global_root / "pipelines" / "feature-dev", "global-one")

    ref = resolve_pipeline("feature-dev", start=project)
    assert ref.scope == "project"
    assert ref.path == (project / ".simplecode" / "pipelines" / "feature-dev").resolve()


def test_resolve_falls_back_to_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_root = tmp_path / "home" / ".simplecode"
    monkeypatch.setattr(registry, "GLOBAL_ROOT", global_root)

    project = tmp_path / "repo"
    project.mkdir()
    (project / ".simplecode" / "pipelines").mkdir(parents=True)
    _write_pipeline(global_root / "pipelines" / "shared", "shared")

    ref = resolve_pipeline("shared", start=project)
    assert ref.scope == "global"
    assert ref.name == "shared"


def test_resolve_explicit_yaml_path(tmp_path: Path) -> None:
    yaml_path = _write_pipeline(tmp_path / "legacy" / "my-pipe")
    ref = resolve_pipeline(str(yaml_path))
    assert ref.scope == "path"
    assert ref.yaml_path == yaml_path.resolve()


def test_resolve_explicit_dir(tmp_path: Path) -> None:
    pipe_dir = tmp_path / "legacy" / "my-pipe"
    _write_pipeline(pipe_dir)
    ref = resolve_pipeline(str(pipe_dir))
    assert ref.scope == "path"
    assert ref.path == pipe_dir.resolve()


def test_resolve_missing_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "GLOBAL_ROOT", tmp_path / "home" / ".simplecode")
    project = tmp_path / "repo"
    project.mkdir()
    ensure_project_pipelines_dir(project)

    with pytest.raises(PipelineConfigError, match="not found"):
        resolve_pipeline("missing", start=project)


def test_list_pipelines_merges_scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_root = tmp_path / "home" / ".simplecode"
    monkeypatch.setattr(registry, "GLOBAL_ROOT", global_root)

    project = tmp_path / "repo"
    project.mkdir()
    _write_pipeline(project / ".simplecode" / "pipelines" / "local-only")
    _write_pipeline(global_root / "pipelines" / "global-only")

    refs = list_pipelines(start=project)
    names = {r.name: r.scope for r in refs}
    assert names["local-only"] == "project"
    assert names["global-only"] == "global"
