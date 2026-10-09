"""CLI coverage for the Plane-driven factory command."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

import simplecode.cli as cli_module
from simplecode.cli import main
from simplecode.factory import Factory, FactoryItemResult


def _write_config(tmp_path: Path, *, pipeline: str = "pipeline") -> Path:
    pipeline_dir = tmp_path / pipeline
    pipeline_dir.mkdir()
    (pipeline_dir / "pipeline.yaml").write_text(
        """\
name: test
agents:
  - id: qa
    prompts: prompts/qa
    permissions: read-only
"""
    )
    config = tmp_path / "factory.yaml"
    config.write_text(
        f"""\
plane:
  base_url: http://localhost:8180
  workspace_slug: test
  api_key_env: TEST_PLANE_API_KEY
pipeline: {pipeline}
projects:
  ENG:
    repo: acme/api
    base_branch: main
"""
    )
    return config


class _FakeDashboard:
    def __init__(
        self,
        *,
        url: str = "http://127.0.0.1:8090",
        start_error: Exception | None = None,
    ) -> None:
        self.url = url
        self.start_error = start_error
        self.started = False
        self.stopped = False

    def start(self) -> _FakeDashboard:
        if self.start_error is not None:
            raise self.start_error
        self.started = True
        return self

    def stop(self) -> None:
        self.stopped = True


def test_factory_once_reports_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _write_config(tmp_path)

    def fake_run_once(_self: Factory) -> list[FactoryItemResult]:
        return [
            FactoryItemResult(
                project_key="ENG",
                project_id="project-id",
                work_item_id="item-id",
                status="completed",
                pr_url="https://github.com/acme/api/pull/1",
            )
        ]

    monkeypatch.setattr(Factory, "run_once", fake_run_once)
    monkeypatch.setattr(
        cli_module,
        "_create_factory_dashboard",
        lambda *_args: pytest.fail("dashboard must not start in --once mode"),
    )
    result = CliRunner().invoke(
        main,
        [
            "factory",
            "--config",
            str(config),
            "--database-url",
            str(tmp_path / "factory.db"),
            "--once",
        ],
        env={"TEST_PLANE_API_KEY": "plane_api_test"},
    )

    assert result.exit_code == 0, result.output
    assert "Mode: once" in result.output
    assert "item-id: completed (https://github.com/acme/api/pull/1)" in result.output


def test_factory_continuous_starts_default_dashboard_and_stops_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _write_config(tmp_path)
    database = tmp_path / "factory.db"
    dashboard = _FakeDashboard()
    created_with: list[tuple[str, int]] = []

    def create_dashboard(database_url: str, port: int) -> _FakeDashboard:
        created_with.append((database_url, port))
        return dashboard

    monkeypatch.setattr(cli_module, "_create_factory_dashboard", create_dashboard)
    monkeypatch.setattr(Factory, "run_forever", lambda _self: None)
    result = CliRunner().invoke(
        main,
        ["factory", "--config", str(config), "--database-url", str(database)],
        env={"TEST_PLANE_API_KEY": "plane_api_test"},
    )

    assert result.exit_code == 0, result.output
    assert created_with == [(str(database), 8090)]
    assert dashboard.started
    assert dashboard.stopped
    assert "Dashboard: http://127.0.0.1:8090" in result.output


def test_factory_no_dashboard_skips_server(tmp_path: Path, monkeypatch) -> None:
    config = _write_config(tmp_path)
    monkeypatch.setattr(
        cli_module,
        "_create_factory_dashboard",
        lambda *_args: pytest.fail("dashboard must not start with --no-dashboard"),
    )
    monkeypatch.setattr(Factory, "run_forever", lambda _self: None)

    result = CliRunner().invoke(
        main,
        [
            "factory",
            "--config",
            str(config),
            "--database-url",
            str(tmp_path / "factory.db"),
            "--no-dashboard",
        ],
        env={"TEST_PLANE_API_KEY": "plane_api_test"},
    )

    assert result.exit_code == 0, result.output
    assert "Dashboard:" not in result.output


def test_factory_uses_custom_dashboard_port(tmp_path: Path, monkeypatch) -> None:
    config = _write_config(tmp_path)
    dashboard = _FakeDashboard(url="http://127.0.0.1:9123")
    ports: list[int] = []

    def create_dashboard(_database_url: str, port: int) -> _FakeDashboard:
        ports.append(port)
        return dashboard

    monkeypatch.setattr(cli_module, "_create_factory_dashboard", create_dashboard)
    monkeypatch.setattr(Factory, "run_forever", lambda _self: None)
    result = CliRunner().invoke(
        main,
        [
            "factory",
            "--config",
            str(config),
            "--database-url",
            str(tmp_path / "factory.db"),
            "--dashboard-port",
            "9123",
        ],
        env={"TEST_PLANE_API_KEY": "plane_api_test"},
    )

    assert result.exit_code == 0, result.output
    assert ports == [9123]
    assert "Dashboard: http://127.0.0.1:9123" in result.output


@pytest.mark.parametrize("port", ["0", "65536"])
def test_factory_rejects_invalid_dashboard_port(tmp_path: Path, port: str) -> None:
    config = _write_config(tmp_path)
    result = CliRunner().invoke(
        main,
        ["factory", "--config", str(config), "--dashboard-port", port],
        env={"TEST_PLANE_API_KEY": "plane_api_test"},
    )

    assert result.exit_code == 2
    assert "1<=x<=65535" in result.output


def test_factory_dashboard_startup_failure_is_sanitized_and_fail_open(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _write_config(tmp_path)
    dashboard = _FakeDashboard(start_error=OSError("token=do-not-print"))
    ran = False

    def run_forever(_self: Factory) -> None:
        nonlocal ran
        ran = True

    monkeypatch.setattr(cli_module, "_create_factory_dashboard", lambda *_args: dashboard)
    monkeypatch.setattr(Factory, "run_forever", run_forever)
    result = CliRunner().invoke(
        main,
        [
            "factory",
            "--config",
            str(config),
            "--database-url",
            str(tmp_path / "factory.db"),
        ],
        env={"TEST_PLANE_API_KEY": "plane_api_test"},
    )

    assert result.exit_code == 0, result.output
    assert ran
    assert "Warning: dashboard could not start; factory will continue." in result.output
    assert "do-not-print" not in result.output
    assert "Dashboard:" not in result.output
    assert dashboard.stopped


@pytest.mark.parametrize(
    "error",
    [RuntimeError("factory failed"), KeyboardInterrupt()],
    ids=["exception", "keyboard-interrupt"],
)
def test_factory_stops_dashboard_when_factory_exits_abnormally(
    tmp_path: Path,
    monkeypatch,
    error: BaseException,
) -> None:
    config = _write_config(tmp_path)
    dashboard = _FakeDashboard()

    def fail(_self: Factory) -> None:
        raise error

    monkeypatch.setattr(cli_module, "_create_factory_dashboard", lambda *_args: dashboard)
    monkeypatch.setattr(Factory, "run_forever", fail)
    result = CliRunner().invoke(
        main,
        [
            "factory",
            "--config",
            str(config),
            "--database-url",
            str(tmp_path / "factory.db"),
        ],
        env={"TEST_PLANE_API_KEY": "plane_api_test"},
    )

    assert result.exit_code != 0
    assert dashboard.stopped


def test_factory_requires_api_key_environment(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    result = CliRunner().invoke(
        main,
        ["factory", "--config", str(config), "--once"],
        env={"TEST_PLANE_API_KEY": ""},
    )

    assert result.exit_code != 0
    assert "TEST_PLANE_API_KEY" in result.output


def test_factory_validates_pipeline_before_claiming(tmp_path: Path) -> None:
    config = tmp_path / "factory.yaml"
    config.write_text(
        """\
plane:
  base_url: http://localhost:8180
  workspace_slug: test
  api_key_env: TEST_PLANE_API_KEY
pipeline: missing
projects:
  ENG: acme/api
"""
    )
    result = CliRunner().invoke(
        main,
        ["factory", "--config", str(config), "--once"],
        env={"TEST_PLANE_API_KEY": "plane_api_test"},
    )

    assert result.exit_code != 0
    assert "Factory pipeline not found" in result.output


def test_factory_once_returns_error_when_a_job_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _write_config(tmp_path)

    def fake_run_once(_self: Factory) -> list[FactoryItemResult]:
        return [
            FactoryItemResult(
                project_key="ENG",
                project_id="project-id",
                work_item_id="item-id",
                status="failed",
                error="QA blocked",
            )
        ]

    monkeypatch.setattr(Factory, "run_once", fake_run_once)
    result = CliRunner().invoke(
        main,
        [
            "factory",
            "--config",
            str(config),
            "--database-url",
            str(tmp_path / "factory.db"),
            "--once",
        ],
        env={"TEST_PLANE_API_KEY": "plane_api_test"},
    )

    assert result.exit_code != 0
    assert "item-id: failed" in result.output
    assert "One or more factory jobs failed" in result.output
