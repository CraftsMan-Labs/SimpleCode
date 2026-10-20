"""Tests for factory YAML configuration. No network."""

from __future__ import annotations

from pathlib import Path

import pytest

from simplecode.factory_config import (
    FactoryConfig,
    FactoryConfigError,
    normalize_base_url,
)


def _write(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


def _valid_yaml(**overrides: str) -> str:
    plane = overrides.get(
        "plane",
        """\
plane:
  base_url: http://localhost:8180
  workspace_slug: craftsman
  api_key_env: PLANE_API_KEY
""",
    )
    rest = overrides.get(
        "rest",
        """\
poll_interval: 45
states:
  ready: Ready for Agent
  in_progress: In Progress
  in_review: In Review
  needs_attention: Needs Attention
pipeline: examples/ticket-delivery
output_dir: ./pipeline-output
runtime: claude
model: opus
timeout: 86400
docker_image: simplecode-agent:latest
projects:
  ENG:
    repo: acme/api
    base_branch: main
  4af68566-94a4-4eb3-94aa-50dc9427067b:
    repo: acme/web
""",
    )
    return plane + rest


def test_load_valid_config(tmp_path: Path) -> None:
    path = _write(tmp_path / "factory.yaml", _valid_yaml())
    config = FactoryConfig.load(path)

    assert config.plane.base_url == "http://localhost:8180"
    assert config.plane.workspace_slug == "craftsman"
    assert config.plane.api_key_env == "PLANE_API_KEY"
    assert config.poll_interval == 45
    assert config.states.ready == "Ready for Agent"
    assert config.states.in_progress == "In Progress"
    assert config.states.in_review == "In Review"
    assert config.states.needs_attention == "Needs Attention"
    assert config.pipeline == (tmp_path / "examples/ticket-delivery").resolve()
    assert config.output_dir == (tmp_path / "pipeline-output").resolve()
    assert config.runtime == "claude"
    assert config.model == "opus"
    assert config.timeout == 86400
    assert config.docker_image == "simplecode-agent:latest"
    assert len(config.projects) == 2
    assert config.mapping_for("ENG") is not None
    assert config.mapping_for("ENG") is not None
    assert config.require_mapping("ENG").repo == "acme/api"
    assert config.require_mapping("ENG").base_branch == "main"
    assert config.require_mapping(project_id="4af68566-94a4-4eb3-94aa-50dc9427067b").repo == (
        "acme/web"
    )


def test_workspace_alias_and_shorthand_repo(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        """\
plane:
  base_url: https://api.plane.so/api/v1/
  workspace: my-team
  api_key_env: PLANE_API_KEY
pipeline: examples/ticket-delivery
projects:
  ENG: acme/api
""",
    )
    config = FactoryConfig.load(path)
    assert config.plane.workspace_slug == "my-team"
    assert config.plane.base_url == "https://api.plane.so"
    assert config.poll_interval == 30
    assert config.pipeline == (tmp_path / "examples/ticket-delivery").resolve()
    assert config.timeout is None
    assert config.output_dir is None
    assert config.require_mapping(identifier="eng").repo == "acme/api"
    assert config.require_mapping(identifier="eng").base_branch is None


def test_default_state_names(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        """\
plane:
  base_url: http://localhost:8180
  workspace_slug: ws
  api_key_env: PLANE_API_KEY
pipeline: ./pipe
projects:
  P: owner/repo
""",
    )
    config = FactoryConfig.load(path)
    assert config.states.ready == "Ready for Agent"
    assert config.states.needs_attention == "Needs Attention"
    assert config.states.done == "Done"
    assert config.events.enabled is False


def test_event_settings_and_secret_resolution(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        _valid_yaml(
            rest="""\
pipeline: ./pipe
events:
  enabled: true
  bind: 127.0.0.1
  port: 9091
  plane_secret_env: PLANE_HOOK
  github_secret_env: GITHUB_HOOK
  reconcile_interval: 120
  task_lease_seconds: 90
  max_attempts: 7
  retry_base_seconds: 4
projects:
  ENG: acme/api
"""
        ),
    )
    config = FactoryConfig.load(path)
    assert config.events.enabled is True
    assert config.events.port == 9091
    assert config.events.reconcile_interval == 120
    assert config.events.task_lease_seconds == 90
    assert config.events.max_attempts == 7
    assert config.events.retry_base_seconds == 4
    assert config.events.resolve_secret("plane", {"PLANE_HOOK": "plane-secret"}) == "plane-secret"
    assert (
        config.events.resolve_secret("github", {"GITHUB_HOOK": "github-secret"}) == "github-secret"
    )


def test_top_level_plane_fields(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        """\
base_url: http://127.0.0.1:8180
workspace_slug: ws
api_key_env: PLANE_API_KEY
pipeline_path: ./pipe
project_map:
  P:
    github_repo: owner/repo
    branch: develop
""",
    )
    config = FactoryConfig.load(path)
    assert config.plane.base_url == "http://127.0.0.1:8180"
    assert config.require_mapping("P").repo == "owner/repo"
    assert config.require_mapping("P").base_branch == "develop"


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FactoryConfigError, match="not found"):
        FactoryConfig.load(tmp_path / "missing.yaml")


def test_invalid_root_type(tmp_path: Path) -> None:
    path = _write(tmp_path / "factory.yaml", "- just a list\n")
    with pytest.raises(FactoryConfigError, match="must be a mapping"):
        FactoryConfig.load(path)


def test_rejects_secret_in_yaml(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        """\
plane:
  base_url: http://localhost:8180
  workspace_slug: ws
  api_key: plane_api_this_is_a_secret
pipeline: ./pipe
projects:
  P: owner/repo
""",
    )
    with pytest.raises(FactoryConfigError, match="must not appear in YAML"):
        FactoryConfig.load(path)


def test_rejects_secret_at_top_level_with_plane_section(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        _valid_yaml() + "\ntoken: should-not-be-here\n",
    )
    with pytest.raises(FactoryConfigError, match="must not appear in YAML"):
        FactoryConfig.load(path)


def test_rejects_secret_looking_env_name(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        """\
plane:
  base_url: http://localhost:8180
  workspace_slug: ws
  api_key_env: plane_api_looks_like_a_token
pipeline: ./pipe
projects:
  P: owner/repo
""",
    )
    with pytest.raises(FactoryConfigError, match="environment variable name"):
        FactoryConfig.load(path)


@pytest.mark.parametrize(
    "url",
    [
        "localhost:8180",
        "ftp://localhost:8180",
        "http://",
        "http://user:pass@localhost:8180",
        "not-a-url",
        "",
    ],
)
def test_invalid_base_url(tmp_path: Path, url: str) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        f"""\
plane:
  base_url: "{url}"
  workspace_slug: ws
  api_key_env: PLANE_API_KEY
pipeline: ./pipe
projects:
  P: owner/repo
""",
    )
    with pytest.raises(FactoryConfigError, match="base_url"):
        FactoryConfig.load(path)


@pytest.mark.parametrize(
    "repo",
    ["acme", "acme/api/extra", "../etc/passwd", "acme/..", "/acme/api", "acme/api.git/"],
)
def test_invalid_repo(tmp_path: Path, repo: str) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        f"""\
plane:
  base_url: http://localhost:8180
  workspace_slug: ws
  api_key_env: PLANE_API_KEY
pipeline: ./pipe
projects:
  P: "{repo}"
""",
    )
    with pytest.raises(FactoryConfigError, match="owner/repo"):
        FactoryConfig.load(path)


def test_empty_project_map(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        """\
plane:
  base_url: http://localhost:8180
  workspace_slug: ws
  api_key_env: PLANE_API_KEY
pipeline: ./pipe
projects: {}
""",
    )
    with pytest.raises(FactoryConfigError, match="must not be empty"):
        FactoryConfig.load(path)


def test_missing_projects(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        """\
plane:
  base_url: http://localhost:8180
  workspace_slug: ws
  api_key_env: PLANE_API_KEY
pipeline: ./pipe
""",
    )
    with pytest.raises(FactoryConfigError, match="must not be empty"):
        FactoryConfig.load(path)


@pytest.mark.parametrize("field,value", [("poll_interval", 0), ("timeout", -5)])
def test_non_positive_interval_and_timeout(tmp_path: Path, field: str, value: int) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        f"""\
plane:
  base_url: http://localhost:8180
  workspace_slug: ws
  api_key_env: PLANE_API_KEY
pipeline: ./pipe
{field}: {value}
projects:
  P: owner/repo
""",
    )
    with pytest.raises(FactoryConfigError, match="positive integer"):
        FactoryConfig.load(path)


def test_timeout_bool_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        """\
plane:
  base_url: http://localhost:8180
  workspace_slug: ws
  api_key_env: PLANE_API_KEY
pipeline: ./pipe
timeout: true
projects:
  P: owner/repo
""",
    )
    with pytest.raises(FactoryConfigError, match="positive integer"):
        FactoryConfig.load(path)


def test_unknown_state_key(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        """\
plane:
  base_url: http://localhost:8180
  workspace_slug: ws
  api_key_env: PLANE_API_KEY
pipeline: ./pipe
states:
  archived: Archived
projects:
  P: owner/repo
""",
    )
    with pytest.raises(FactoryConfigError, match="unknown keys"):
        FactoryConfig.load(path)


def test_invalid_workspace_slug(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        """\
plane:
  base_url: http://localhost:8180
  workspace_slug: "my team"
  api_key_env: PLANE_API_KEY
pipeline: ./pipe
projects:
  P: owner/repo
""",
    )
    with pytest.raises(FactoryConfigError, match="workspace slug"):
        FactoryConfig.load(path)


def test_coder_workspace_config_and_token_resolution(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        """\
plane:
  base_url: http://localhost:8180
  workspace_slug: ws
  api_key_env: PLANE_API_KEY
pipeline: ./pipe
workspace:
  provider: coder
  coder:
    url: https://coder.example.com/
    token_env: CODER_SESSION_TOKEN
    template: simplecode-factory
    organization: engineering
    provision_timeout: 300
    cleanup_timeout: 120
    stop_after: 3h
    max_bundle_mb: 256
projects:
  P: owner/repo
""",
    )

    config = FactoryConfig.load(path)

    assert config.workspace.provider == "coder"
    assert config.workspace.coder is not None
    assert config.workspace.coder.url == "https://coder.example.com"
    assert config.workspace.coder.organization == "engineering"
    assert config.workspace.coder.resolve_token({"CODER_SESSION_TOKEN": " token "}) == "token"
    assert config.workspace.coder.max_bundle_mb == 256


def test_local_workspace_is_default(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        """\
plane:
  base_url: http://localhost:8180
  workspace_slug: ws
  api_key_env: PLANE_API_KEY
pipeline: ./pipe
projects:
  P: owner/repo
""",
    )
    assert FactoryConfig.load(path).workspace.provider == "local"


@pytest.mark.parametrize(
    "workspace,error",
    [
        ("workspace:\n  provider: vm", "provider"),
        ("workspace:\n  provider: coder", "workspace.coder"),
        (
            "workspace:\n  provider: coder\n  coder:\n"
            "    url: https://coder.example.com\n"
            "    token: secret\n"
            "    token_env: CODER_TOKEN\n"
            "    template: t",
            "secrets",
        ),
        (
            "workspace:\n  provider: coder\n  coder:\n"
            "    url: https://coder.example.com\n"
            "    token_env: not-valid-name\n"
            "    template: t",
            "environment variable",
        ),
        (
            "workspace:\n  provider: coder\n  coder:\n"
            "    url: https://coder.example.com\n"
            "    token_env: CODER_TOKEN\n"
            "    template: t\n"
            "    stop_after: forever",
            "positive duration",
        ),
    ],
)
def test_invalid_coder_workspace_config(tmp_path: Path, workspace: str, error: str) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        f"""\
plane:
  base_url: http://localhost:8180
  workspace_slug: ws
  api_key_env: PLANE_API_KEY
pipeline: ./pipe
{workspace}
projects:
  P: owner/repo
""",
    )
    with pytest.raises(FactoryConfigError, match=error):
        FactoryConfig.load(path)


def test_coder_workspace_rejects_docker_image(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        """\
plane:
  base_url: http://localhost:8180
  workspace_slug: ws
  api_key_env: PLANE_API_KEY
pipeline: ./pipe
docker_image: simplecode-agent:latest
workspace:
  provider: coder
  coder:
    url: https://coder.example.com
    token_env: CODER_SESSION_TOKEN
    template: simplecode-factory
projects:
  P: owner/repo
""",
    )
    with pytest.raises(FactoryConfigError, match="docker_image"):
        FactoryConfig.load(path)


@pytest.mark.parametrize(
    "branch",
    ["feat/../secret", ".hidden", "main/", "release.lock", "feat//double"],
)
def test_invalid_base_branch(tmp_path: Path, branch: str) -> None:
    path = _write(
        tmp_path / "factory.yaml",
        f"""\
plane:
  base_url: http://localhost:8180
  workspace_slug: ws
  api_key_env: PLANE_API_KEY
pipeline: ./pipe
projects:
  P:
    repo: owner/repo
    base_branch: "{branch}"
""",
    )
    with pytest.raises(FactoryConfigError, match="base_branch"):
        FactoryConfig.load(path)


def test_api_key_resolved_separately_at_runtime(tmp_path: Path) -> None:
    path = _write(tmp_path / "factory.yaml", _valid_yaml())
    config = FactoryConfig.load(path)
    with pytest.raises(FactoryConfigError, match="PLANE_API_KEY"):
        config.resolve_api_key(environ={})
    with pytest.raises(FactoryConfigError, match="missing or empty"):
        config.resolve_api_key(environ={"PLANE_API_KEY": "   "})
    assert config.resolve_api_key(environ={"PLANE_API_KEY": " plane_api_ok "}) == "plane_api_ok"


def test_require_mapping_unknown() -> None:
    config = FactoryConfig.from_dict(
        {
            "plane": {
                "base_url": "http://localhost:8180",
                "workspace_slug": "ws",
                "api_key_env": "PLANE_API_KEY",
            },
            "pipeline": "./pipe",
            "projects": {"ENG": "owner/repo"},
        }
    )
    with pytest.raises(FactoryConfigError, match="not in the factory allowlist"):
        config.require_mapping("OPS")


def test_normalize_base_url_strips_api_prefix() -> None:
    assert normalize_base_url(" https://app.example/api/v1/ ") == "https://app.example"
    assert normalize_base_url("http://localhost:8180/") == "http://localhost:8180"
