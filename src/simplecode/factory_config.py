"""Factory YAML configuration: Plane connection, states, and allowlisted repos.

Secrets never belong in YAML. The file stores the *name* of the environment
variable that holds the Plane API key; the key itself is resolved at runtime.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import overload
from urllib.parse import urlparse

import yaml

from simplecode.errors import SimpleCodeError

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")
_DURATION_RE = re.compile(r"^[1-9][0-9]*(?:s|m|h|d)$")

_DEFAULT_STATES = {
    "ready": "Ready for Agent",
    "in_progress": "In Progress",
    "in_review": "In Review",
    "done": "Done",
    "needs_attention": "Needs Attention",
}

_SECRET_PLANE_KEYS = frozenset({"api_key", "token", "secret", "password"})


class FactoryConfigError(SimpleCodeError):
    """Invalid factory configuration (bad YAML, missing fields, failed validation)."""


@dataclass(frozen=True)
class PlaneConnectionConfig:
    """How the factory reaches Plane. ``api_key_env`` is an env-var *name*."""

    base_url: str
    workspace_slug: str
    api_key_env: str


@dataclass(frozen=True)
class FactoryStateNames:
    """Plane workflow state names the factory moves work items through."""

    ready: str
    in_progress: str
    in_review: str
    needs_attention: str
    done: str = "Done"


@dataclass(frozen=True)
class FactoryEventsConfig:
    """Durable webhook intake and worker settings."""

    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8091
    plane_secret_env: str = "PLANE_WEBHOOK_SECRET"
    github_secret_env: str = "GITHUB_WEBHOOK_SECRET"
    reconcile_interval: int = 300
    task_lease_seconds: int = 60
    max_attempts: int = 5
    retry_base_seconds: int = 10

    def resolve_secret(self, source: str, environ: Mapping[str, str] | None = None) -> str:
        env = os.environ if environ is None else environ
        name = self.plane_secret_env if source == "plane" else self.github_secret_env
        value = env.get(name, "").strip()
        if not value:
            raise FactoryConfigError(f"Environment variable {name!r} is missing or empty")
        return value


@dataclass(frozen=True)
class CoderWorkspaceConfig:
    """Connection and safety limits for ephemeral Coder workspaces."""

    url: str
    token_env: str
    template: str
    organization: str | None = None
    provision_timeout: int = 600
    cleanup_timeout: int = 600
    stop_after: str = "2h"
    max_bundle_mb: int = 512

    def resolve_token(self, environ: Mapping[str, str] | None = None) -> str:
        env = os.environ if environ is None else environ
        value = env.get(self.token_env, "").strip()
        if not value:
            raise FactoryConfigError(f"Environment variable {self.token_env!r} is missing or empty")
        return value


@dataclass(frozen=True)
class FactoryWorkspaceConfig:
    """Workspace provider selected for factory delivery attempts."""

    provider: str = "local"
    coder: CoderWorkspaceConfig | None = None


@dataclass(frozen=True)
class FactoryProjectMapping:
    """Allowlisted Plane project identifier or id → GitHub repository."""

    key: str
    repo: str
    base_branch: str | None = None


@dataclass(frozen=True)
class FactoryConfig:
    """Validated factory configuration loaded from YAML or constructed in Python."""

    plane: PlaneConnectionConfig
    poll_interval: int
    states: FactoryStateNames
    pipeline: Path
    projects: tuple[FactoryProjectMapping, ...]
    output_dir: Path | None = None
    runtime: str | None = None
    model: str | None = None
    timeout: int | None = None
    docker_image: str | None = None
    events: FactoryEventsConfig = FactoryEventsConfig()
    workspace: FactoryWorkspaceConfig = FactoryWorkspaceConfig()

    @classmethod
    def load(cls, yaml_path: Path) -> FactoryConfig:
        if not yaml_path.exists():
            raise FactoryConfigError(f"Factory config file not found: {yaml_path}")

        try:
            data: object = yaml.safe_load(yaml_path.read_text())
        except yaml.YAMLError as exc:
            raise FactoryConfigError(f"Invalid factory YAML: {exc}") from exc
        config = cls.from_dict(data)
        base_dir = yaml_path.resolve().parent
        return replace(
            config,
            pipeline=_resolve_path(config.pipeline, base_dir),
            output_dir=(
                _resolve_path(config.output_dir, base_dir)
                if config.output_dir is not None
                else None
            ),
        )

    @classmethod
    def from_dict(cls, data: object) -> FactoryConfig:
        raw = _require_dict(data, "factory config")
        _reject_secret_keys(raw, "factory config")
        plane = _parse_plane(_plane_section(raw))
        states = _parse_states(raw.get("states"))
        pipeline = _parse_pipeline(raw)
        projects = _parse_projects(raw)
        poll_interval = _optional_positive_int(
            raw.get("poll_interval"), "poll_interval", default=30
        )
        timeout = _optional_positive_int(raw.get("timeout"), "timeout", default=None)
        output_dir = _optional_path(raw.get("output_dir"), "output_dir")
        runtime = _optional_nonempty(raw.get("runtime"), "runtime")
        model = _optional_nonempty(raw.get("model"), "model")
        docker_image = _optional_nonempty(raw.get("docker_image"), "docker_image")
        events = _parse_events(raw.get("events"))
        workspace = _parse_workspace(raw.get("workspace"))
        if workspace.provider == "coder" and docker_image is not None:
            raise FactoryConfigError(
                "docker_image cannot be combined with workspace.provider='coder'"
            )
        return cls(
            plane=plane,
            poll_interval=poll_interval,
            states=states,
            pipeline=pipeline,
            projects=projects,
            output_dir=output_dir,
            runtime=runtime,
            model=model,
            timeout=timeout,
            docker_image=docker_image,
            events=events,
            workspace=workspace,
        )

    def resolve_api_key(self, environ: Mapping[str, str] | None = None) -> str:
        """Return the Plane API key from the environment. Never logs the value."""
        env = os.environ if environ is None else environ
        raw = env.get(self.plane.api_key_env)
        if raw is None or not str(raw).strip():
            raise FactoryConfigError(
                f"Environment variable {self.plane.api_key_env!r} is missing or empty"
            )
        return str(raw).strip()

    def mapping_for(
        self,
        key: str | None = None,
        *,
        project_id: str | None = None,
        identifier: str | None = None,
    ) -> FactoryProjectMapping | None:
        """Look up an allowlisted project by YAML key, Plane id, or identifier."""
        candidates = [value for value in (key, project_id, identifier) if value]
        if not candidates:
            return None
        by_key = {item.key: item for item in self.projects}
        for candidate in candidates:
            found = by_key.get(candidate)
            if found is not None:
                return found
        lowered = {item.key.lower(): item for item in self.projects}
        for candidate in candidates:
            found = lowered.get(candidate.lower())
            if found is not None:
                return found
        return None

    def require_mapping(
        self,
        key: str | None = None,
        *,
        project_id: str | None = None,
        identifier: str | None = None,
    ) -> FactoryProjectMapping:
        found = self.mapping_for(key, project_id=project_id, identifier=identifier)
        if found is None:
            tried = [value for value in (key, project_id, identifier) if value]
            raise FactoryConfigError(
                "Plane project is not in the factory allowlist: " + ", ".join(tried)
            )
        return found


def normalize_base_url(url: str) -> str:
    """Strip whitespace, query, fragment, trailing slash, and a trailing API prefix."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise FactoryConfigError(
            "base_url must be an absolute http(s) URL with a host (no credentials)"
        )
    if parsed.username or parsed.password:
        raise FactoryConfigError("base_url must not contain credentials")
    path = parsed.path.rstrip("/")
    for suffix in ("/api/v2", "/api/v1", "/api"):
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _normalize_url(value: object, field: str) -> str:
    text = _require_str(value, field)
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise FactoryConfigError(f"{field} must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise FactoryConfigError(f"{field} must not contain credentials, query, or fragment")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def _plane_section(raw: dict[str, object]) -> dict[str, object]:
    if "plane" in raw:
        return _require_dict(raw["plane"], "plane")
    return raw


def _parse_plane(raw: dict[str, object]) -> PlaneConnectionConfig:
    _reject_secret_keys(raw, "Plane")

    base_url = normalize_base_url(_require_str(raw.get("base_url"), "plane.base_url"))
    workspace = raw.get("workspace_slug", raw.get("workspace"))
    workspace_slug = _require_str(workspace, "plane.workspace_slug")
    if not _SLUG_RE.fullmatch(workspace_slug):
        raise FactoryConfigError(
            "plane.workspace_slug must be a Plane workspace slug "
            "(letters, digits, hyphen, underscore)"
        )

    api_key_env = _require_str(
        raw.get("api_key_env", raw.get("api_key_env_var")),
        "plane.api_key_env",
    )
    if not _ENV_NAME_RE.fullmatch(api_key_env) or _looks_like_plane_token(api_key_env):
        raise FactoryConfigError(
            "plane.api_key_env must be an environment variable name, not a secret"
        )
    return PlaneConnectionConfig(
        base_url=base_url,
        workspace_slug=workspace_slug,
        api_key_env=api_key_env,
    )


def _reject_secret_keys(raw: Mapping[str, object], section: str) -> None:
    leaked = sorted(_SECRET_PLANE_KEYS.intersection(raw))
    if leaked:
        raise FactoryConfigError(
            f"{section} API secrets must not appear in YAML; set api_key_env instead "
            f"(found {', '.join(leaked)})"
        )


def _parse_states(value: object) -> FactoryStateNames:
    if value is None:
        data: dict[str, object] = {}
    else:
        data = _require_dict(value, "states")

    unknown = set(data) - set(_DEFAULT_STATES)
    if unknown:
        raise FactoryConfigError("states contains unknown keys: " + ", ".join(sorted(unknown)))

    resolved: dict[str, str] = {}
    for key, default in _DEFAULT_STATES.items():
        if key not in data or data[key] is None:
            resolved[key] = default
            continue
        resolved[key] = _require_str(data[key], f"states.{key}")
    return FactoryStateNames(**resolved)


def _parse_events(value: object) -> FactoryEventsConfig:
    if value is None:
        return FactoryEventsConfig()
    data = _require_dict(value, "events")
    allowed = {
        "enabled",
        "bind",
        "port",
        "plane_secret_env",
        "github_secret_env",
        "reconcile_interval",
        "task_lease_seconds",
        "max_attempts",
        "retry_base_seconds",
    }
    unknown = set(data) - allowed
    if unknown:
        raise FactoryConfigError("events contains unknown keys: " + ", ".join(sorted(unknown)))
    enabled = data.get("enabled", False)
    if not isinstance(enabled, bool):
        raise FactoryConfigError("events.enabled must be a boolean")
    bind = _optional_nonempty(data.get("bind"), "events.bind") or "127.0.0.1"
    if bind not in {"127.0.0.1", "::1", "localhost"}:
        raise FactoryConfigError("events.bind must be a loopback address")
    port = _optional_positive_int(data.get("port"), "events.port", default=8091)
    assert port is not None
    if port > 65535:
        raise FactoryConfigError("events.port must be at most 65535")
    plane_env = (
        _optional_nonempty(data.get("plane_secret_env"), "events.plane_secret_env")
        or "PLANE_WEBHOOK_SECRET"
    )
    github_env = (
        _optional_nonempty(data.get("github_secret_env"), "events.github_secret_env")
        or "GITHUB_WEBHOOK_SECRET"
    )
    for field, name in (
        ("events.plane_secret_env", plane_env),
        ("events.github_secret_env", github_env),
    ):
        if not _ENV_NAME_RE.fullmatch(name):
            raise FactoryConfigError(f"{field} must be an environment variable name")
    return FactoryEventsConfig(
        enabled=enabled,
        bind=bind,
        port=port,
        plane_secret_env=plane_env,
        github_secret_env=github_env,
        reconcile_interval=_required_positive(data, "reconcile_interval", 300),
        task_lease_seconds=_required_positive(data, "task_lease_seconds", 60),
        max_attempts=_required_positive(data, "max_attempts", 5),
        retry_base_seconds=_required_positive(data, "retry_base_seconds", 10),
    )


def _parse_workspace(value: object) -> FactoryWorkspaceConfig:
    if value is None:
        return FactoryWorkspaceConfig()
    data = _require_dict(value, "workspace")
    allowed = {"provider", "coder"}
    unknown = set(data) - allowed
    if unknown:
        raise FactoryConfigError("workspace contains unknown keys: " + ", ".join(sorted(unknown)))
    provider = _optional_nonempty(data.get("provider"), "workspace.provider") or "local"
    if provider not in {"local", "coder"}:
        raise FactoryConfigError("workspace.provider must be 'local' or 'coder'")
    coder_value = data.get("coder")
    if provider == "local":
        if coder_value is not None:
            raise FactoryConfigError("workspace.coder requires workspace.provider='coder'")
        return FactoryWorkspaceConfig()

    coder = _require_dict(coder_value, "workspace.coder")
    _reject_secret_keys(coder, "Coder")
    coder_allowed = {
        "url",
        "token_env",
        "template",
        "organization",
        "provision_timeout",
        "cleanup_timeout",
        "stop_after",
        "max_bundle_mb",
    }
    coder_unknown = set(coder) - coder_allowed
    if coder_unknown:
        raise FactoryConfigError(
            "workspace.coder contains unknown keys: " + ", ".join(sorted(coder_unknown))
        )
    token_env = _require_str(coder.get("token_env"), "workspace.coder.token_env")
    if not _ENV_NAME_RE.fullmatch(token_env):
        raise FactoryConfigError("workspace.coder.token_env must be an environment variable name")
    stop_after = _optional_nonempty(coder.get("stop_after"), "workspace.coder.stop_after") or "2h"
    if not _DURATION_RE.fullmatch(stop_after):
        raise FactoryConfigError(
            "workspace.coder.stop_after must be a positive duration such as '2h'"
        )
    resolved = CoderWorkspaceConfig(
        url=_normalize_url(coder.get("url"), "workspace.coder.url"),
        token_env=token_env,
        template=_require_str(coder.get("template"), "workspace.coder.template"),
        organization=_optional_nonempty(coder.get("organization"), "workspace.coder.organization"),
        provision_timeout=_workspace_positive(coder, "provision_timeout", 600),
        cleanup_timeout=_workspace_positive(coder, "cleanup_timeout", 600),
        stop_after=stop_after,
        max_bundle_mb=_workspace_positive(coder, "max_bundle_mb", 512),
    )
    return FactoryWorkspaceConfig(provider="coder", coder=resolved)


def _workspace_positive(data: Mapping[str, object], key: str, default: int) -> int:
    value = _optional_positive_int(data.get(key), f"workspace.coder.{key}", default=default)
    assert value is not None
    return value


def _required_positive(data: Mapping[str, object], key: str, default: int) -> int:
    value = _optional_positive_int(data.get(key), f"events.{key}", default=default)
    assert value is not None
    return value


def _parse_pipeline(raw: dict[str, object]) -> Path:
    value = raw.get("pipeline", raw.get("pipeline_path"))
    return Path(_require_str(value, "pipeline"))


def _parse_projects(raw: dict[str, object]) -> tuple[FactoryProjectMapping, ...]:
    value = raw.get("projects", raw.get("project_map"))
    if value is None:
        raise FactoryConfigError("projects mapping is required and must not be empty")
    data = _require_dict(value, "projects")
    if not data:
        raise FactoryConfigError("projects mapping is required and must not be empty")

    mappings: list[FactoryProjectMapping] = []
    seen: set[str] = set()
    for key_obj, spec in data.items():
        key = str(key_obj).strip()
        if not key:
            raise FactoryConfigError("projects keys must be nonempty Plane identifiers or ids")
        lowered = key.lower()
        if lowered in seen:
            raise FactoryConfigError(f"Duplicate project mapping key: {key}")
        seen.add(lowered)
        mappings.append(_parse_project_mapping(key, spec))
    return tuple(mappings)


def _parse_project_mapping(key: str, spec: object) -> FactoryProjectMapping:
    if isinstance(spec, str):
        return FactoryProjectMapping(key=key, repo=_validate_repo(spec, f"projects.{key}"))

    data = _require_dict(spec, f"projects.{key}")
    repo_value = data.get("repo", data.get("github_repo", data.get("repository")))
    repo = _validate_repo(repo_value, f"projects.{key}.repo")
    branch_raw = data.get("base_branch", data.get("branch"))
    base_branch = _optional_nonempty(branch_raw, f"projects.{key}.base_branch")
    if base_branch is not None and not _valid_branch(base_branch):
        raise FactoryConfigError(f"projects.{key}.base_branch must be a valid git branch name")
    return FactoryProjectMapping(key=key, repo=repo, base_branch=base_branch)


def _validate_repo(value: object, field: str) -> str:
    repo = _require_str(value, field)
    if not _REPO_RE.fullmatch(repo) or repo.startswith("/") or repo.endswith("/"):
        raise FactoryConfigError(f"{field} must be a GitHub owner/repo path (e.g. acme/api)")
    owner, name = repo.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        raise FactoryConfigError(f"{field} must be a GitHub owner/repo path (e.g. acme/api)")
    return repo


def _require_dict(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise FactoryConfigError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FactoryConfigError(f"{field} is required")
    return value.strip()


def _optional_nonempty(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise FactoryConfigError(f"{field} must be a nonempty string when set")
    return value.strip()


def _optional_path(value: object, field: str) -> Path | None:
    text = _optional_nonempty(value, field)
    return Path(text) if text is not None else None


def _resolve_path(path: Path, base_dir: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else (base_dir / expanded).resolve()


def _valid_branch(value: str) -> bool:
    parts = value.split("/")
    return (
        bool(_BRANCH_RE.fullmatch(value))
        and not value.startswith(("-", ".", "/"))
        and not value.endswith((".", "/"))
        and ".." not in value
        and "//" not in value
        and all(part and not part.startswith(".") and not part.endswith(".lock") for part in parts)
    )


def _looks_like_plane_token(value: str) -> bool:
    """Reject pasted Plane PATs while allowing names like ``PLANE_API_KEY``."""
    return value.lower().startswith("plane_api_") and not value.isupper()


@overload
def _optional_positive_int(value: object, field: str, default: int) -> int: ...


@overload
def _optional_positive_int(value: object, field: str, default: None) -> int | None: ...


def _optional_positive_int(value: object, field: str, default: int | None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FactoryConfigError(f"{field} must be a positive integer")
    return value
