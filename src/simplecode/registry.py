"""Discover and resolve pipelines from project and global ``.simplecode`` dirs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from simplecode.errors import PipelineConfigError

DIR_NAME = ".simplecode"
PIPELINES_SUBDIR = "pipelines"
GLOBAL_ROOT = Path.home() / DIR_NAME


@dataclass(frozen=True)
class PipelineRef:
    """A discovered pipeline directory with a ``pipeline.yaml``."""

    name: str
    path: Path
    scope: str  # "project" | "global"
    yaml_path: Path

    @property
    def description(self) -> str | None:
        try:
            from simplecode.pipeline import PipelineConfig

            return PipelineConfig.load(self.yaml_path).description
        except Exception:
            return None


def global_pipelines_dir() -> Path:
    return GLOBAL_ROOT / PIPELINES_SUBDIR


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk upward from *start* looking for a ``.simplecode`` directory."""
    current = (start or Path.cwd()).resolve()
    if not current.is_dir():
        current = current.parent

    for candidate in [current, *current.parents]:
        if (candidate / DIR_NAME).is_dir():
            return candidate
    return None


def project_pipelines_dir(start: Path | None = None) -> Path | None:
    root = find_project_root(start)
    if root is None:
        return None
    return root / DIR_NAME / PIPELINES_SUBDIR


def ensure_project_pipelines_dir(start: Path | None = None) -> Path:
    """Create ``.simplecode/pipelines`` under *start* (or cwd) and return it."""
    base = (start or Path.cwd()).resolve()
    if not base.is_dir():
        base = base.parent
    pipelines = base / DIR_NAME / PIPELINES_SUBDIR
    pipelines.mkdir(parents=True, exist_ok=True)
    return pipelines


def ensure_global_pipelines_dir() -> Path:
    pipelines = global_pipelines_dir()
    pipelines.mkdir(parents=True, exist_ok=True)
    return pipelines


def _scan_pipelines_dir(pipelines_dir: Path, scope: str) -> list[PipelineRef]:
    if not pipelines_dir.is_dir():
        return []

    found: list[PipelineRef] = []
    for child in sorted(pipelines_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        yaml_path = child / "pipeline.yaml"
        if yaml_path.is_file():
            found.append(
                PipelineRef(
                    name=child.name,
                    path=child.resolve(),
                    scope=scope,
                    yaml_path=yaml_path.resolve(),
                )
            )
    return found


def list_pipelines(start: Path | None = None) -> list[PipelineRef]:
    """List pipelines. Project entries win over global when names collide in resolve."""
    project: list[PipelineRef] = []
    project_dir = project_pipelines_dir(start)
    if project_dir is not None:
        project = _scan_pipelines_dir(project_dir, "project")

    global_list = _scan_pipelines_dir(global_pipelines_dir(), "global")
    return [*project, *global_list]


def resolve_pipeline(
    name_or_path: str,
    *,
    start: Path | None = None,
) -> PipelineRef:
    """Resolve a pipeline by name, path to ``pipeline.yaml``, or pipeline directory.

    Resolution order for bare names:
    1. Project ``.simplecode/pipelines/<name>/`` (walk up from *start*)
    2. Global ``~/.simplecode/pipelines/<name>/``
    """
    raw = Path(name_or_path)

    # Explicit path to yaml or directory
    if raw.exists() or "/" in name_or_path or name_or_path.endswith(".yaml"):
        path = raw.expanduser().resolve()
        if path.is_file():
            if path.name != "pipeline.yaml":
                raise PipelineConfigError(
                    f"Expected a pipeline.yaml file, got: {path}"
                )
            return PipelineRef(
                name=path.parent.name,
                path=path.parent,
                scope="path",
                yaml_path=path,
            )
        if path.is_dir():
            yaml_path = path / "pipeline.yaml"
            if not yaml_path.is_file():
                raise PipelineConfigError(f"No pipeline.yaml in directory: {path}")
            return PipelineRef(
                name=path.name,
                path=path,
                scope="path",
                yaml_path=yaml_path.resolve(),
            )
        raise PipelineConfigError(f"Pipeline path not found: {path}")

    name = name_or_path
    project_dir = project_pipelines_dir(start)
    if project_dir is not None:
        candidate = project_dir / name
        yaml_path = candidate / "pipeline.yaml"
        if yaml_path.is_file():
            return PipelineRef(
                name=name,
                path=candidate.resolve(),
                scope="project",
                yaml_path=yaml_path.resolve(),
            )

    global_candidate = global_pipelines_dir() / name
    global_yaml = global_candidate / "pipeline.yaml"
    if global_yaml.is_file():
        return PipelineRef(
            name=name,
            path=global_candidate.resolve(),
            scope="global",
            yaml_path=global_yaml.resolve(),
        )

    searched = []
    if project_dir is not None:
        searched.append(str(project_dir))
    searched.append(str(global_pipelines_dir()))
    raise PipelineConfigError(
        f"Pipeline '{name}' not found.\n"
        f"Searched:\n  - " + "\n  - ".join(searched) + "\n"
        f"Create one with: simplecode init {name}"
    )
