"""CLI for simplecode — orchestrate multi-agent pipelines."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import click

from simplecode.errors import SimpleCodeError, StoreError
from simplecode.pipeline import Pipeline, PipelineConfig
from simplecode.progress import CLIProgressCallback
from simplecode.runner import discover_prompts
from simplecode.runtime import SUPPORTED_RUNTIMES
from simplecode.store import SQLITE_DEFAULT_PATH, SessionStore

logger = logging.getLogger("simplecode")

_ENV_VAR = "SIMPLECODE_DATABASE_URL"


def _resolve_database_url(explicit_url: str | None) -> str | None:
    """Resolve the database URL from flag, env var, or None."""
    return explicit_url or os.environ.get(_ENV_VAR)


def _get_store(database_url: str | None, require: bool = False) -> SessionStore | None:
    """Create a SessionStore, with fallback prompt on PostgreSQL failure.

    If require=True and no URL is available, returns a SQLite store at the default path.
    """
    url = _resolve_database_url(database_url)

    if url and url.startswith(("postgres://", "postgresql://")):
        try:
            store = SessionStore(url)
            store.ensure_schema()
            return store
        except StoreError as e:
            click.echo(f"\nPostgreSQL connection failed: {e}\n", err=True)
            if click.confirm(
                f"  Fall back to SQLite at {SQLITE_DEFAULT_PATH}?",
                default=True,
            ):
                url = str(SQLITE_DEFAULT_PATH)
            else:
                click.echo("Aborted.", err=True)
                sys.exit(1)

    if url:
        store = SessionStore(url)
        store.ensure_schema()
        return store

    if require:
        url = str(SQLITE_DEFAULT_PATH)
        click.echo(f"No database URL set. Using SQLite at {url}")
        store = SessionStore(url)
        store.ensure_schema()
        return store

    return None


@click.group()
@click.version_option(package_name="simplecode")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
def main(verbose: bool) -> None:
    """simplecode — orchestrate AI coding agents in repeatable pipelines."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command()
@click.option(
    "-f",
    "--file",
    "pipeline_file",
    required=True,
    help="Path to pipeline.yaml",
)
@click.option(
    "-r",
    "--runtime",
    type=click.Choice(SUPPORTED_RUNTIMES),
    default="claude",
    help="Default CLI runtime for all agents",
)
@click.option("-w", "--working-dir", default=".", help="Working directory for the agent CLI")
@click.option("-o", "--output", default="./pipeline-output", help="Output directory")
@click.option("-t", "--timeout", default=300, type=int, help="Default timeout per agent (seconds)")
@click.option("-m", "--model", default=None, help="Default model (overridable per-agent in YAML)")
@click.option(
    "--permissions",
    type=click.Choice(["read-only", "write", "full"]),
    default="write",
    help="Default permission level",
)
@click.option(
    "-i",
    "--input",
    "input_files",
    multiple=True,
    help="Input file(s) injected as context to the first agent (repeatable)",
)
@click.option(
    "--gate-mode",
    type=click.Choice(["stdin", "file", "checkpoint"]),
    default="stdin",
    help="Gate mode: stdin (interactive), file (cloud/CI), or checkpoint (save & exit)",
)
@click.option(
    "--gate-dir",
    default=None,
    type=click.Path(),
    help="Directory for file-based gate artifacts (used with --gate-mode file)",
)
@click.option(
    "--database-url",
    default=None,
    envvar=_ENV_VAR,
    help=f"Database URL (or set {_ENV_VAR}). PostgreSQL or SQLite path.",
)
def run(
    pipeline_file: str,
    runtime: str,
    working_dir: str,
    output: str,
    timeout: int,
    model: str | None,
    permissions: str,
    input_files: tuple[str, ...],
    gate_mode: str,
    gate_dir: str | None,
    database_url: str | None,
) -> None:
    """Run a multi-agent pipeline from a YAML file."""
    pipeline_path = Path(pipeline_file).resolve()
    config = PipelineConfig.load(pipeline_path)
    pipeline_dir = pipeline_path.parent

    pipeline = Pipeline(config, pipeline_dir, pipeline_yaml_path=pipeline_path)

    store = _get_store(database_url, require=(gate_mode == "checkpoint"))

    click.echo(f"Pipeline: {config.name}")
    click.echo(f"Agents:   {len(config.agents)}")
    click.echo(f"Runtime:  {runtime}")
    click.echo(f"Model:    {model or 'default'}")
    click.echo(f"Output:   {output}/")
    if gate_mode == "checkpoint" and store:
        click.echo(f"Store:    {store.backend}")
    click.echo()

    for i, agent in enumerate(config.agents, 1):
        gate_marker = f" [gate: {agent.gate}]" if agent.gate else ""
        agent_model = agent.model or model or "default"
        click.echo(
            f"  {i}. {agent.id} (model={agent_model}, perm={agent.permissions}){gate_marker}"
        )
    click.echo()

    try:
        result = pipeline.run(
            runtime=runtime,
            working_dir=working_dir,
            input_files=list(input_files) or None,
            gate_mode=gate_mode,
            output_dir=output,
            timeout=timeout,
            model=model,
            permissions=permissions,
            gate_dir=Path(gate_dir) if gate_dir else None,
            progress=CLIProgressCallback(),
            heartbeat_interval=30,
            store=store,
        )
    except SimpleCodeError as e:
        click.echo(f"\nError: {e}", err=True)
        sys.exit(1)

    click.echo()
    if result.status == "checkpointed":
        click.echo(f"Pipeline checkpointed at gate: {result.checkpoint_agent}")
        click.echo(f"Session: {result.session_id}")
        click.echo()
        click.echo("Resume with:")
        click.echo(f"  simplecode resume -s {result.session_id} --approve")
        click.echo(f"  simplecode resume -s {result.session_id} --feedback \"your feedback\"")
    elif result.gate_rejected_at:
        click.echo(f"Pipeline stopped at gate: {result.gate_rejected_at}")
    else:
        click.echo("Pipeline complete.")
    click.echo(f"Agents completed: {result.agents_completed}/{result.total_agents}")
    click.echo(f"Output: {result.output_dir}")


@main.command()
@click.option(
    "-s",
    "--session",
    "session_id",
    required=True,
    help="Session ID to resume",
)
@click.option(
    "--database-url",
    default=None,
    envvar=_ENV_VAR,
    help=f"Database URL (or set {_ENV_VAR})",
)
@click.option("--feedback", default=None, help="Feedback text to send to the gated agent")
@click.option(
    "--feedback-file",
    default=None,
    type=click.Path(exists=True),
    help="File containing feedback text",
)
@click.option("--approve", is_flag=True, help="Approve the gate and proceed (no feedback)")
@click.option("--reject", is_flag=True, help="Reject the gate and stop the pipeline")
@click.option(
    "--gate-mode",
    type=click.Choice(["stdin", "file", "checkpoint"]),
    default="stdin",
    help="Gate mode for subsequent gates",
)
@click.option(
    "--gate-dir",
    default=None,
    type=click.Path(),
    help="Directory for file-based gate artifacts",
)
def resume(
    session_id: str,
    database_url: str | None,
    feedback: str | None,
    feedback_file: str | None,
    approve: bool,
    reject: bool,
    gate_mode: str,
    gate_dir: str | None,
) -> None:
    """Resume a checkpointed pipeline."""
    if feedback and feedback_file:
        click.echo("Error: --feedback and --feedback-file are mutually exclusive", err=True)
        sys.exit(1)

    if reject and (feedback or feedback_file or approve):
        click.echo("Error: --reject cannot be combined with --feedback or --approve", err=True)
        sys.exit(1)

    if feedback_file:
        feedback = Path(feedback_file).read_text().strip()

    store = _get_store(database_url, require=True)
    assert store is not None

    try:
        session = store.load_session(session_id)
    except SimpleCodeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    pipeline_yaml_path = Path(session.pipeline_yaml)
    if not pipeline_yaml_path.exists():
        click.echo(
            f"Error: Pipeline YAML not found at {pipeline_yaml_path}",
            err=True,
        )
        sys.exit(1)

    config = PipelineConfig.load(pipeline_yaml_path)
    pipeline = Pipeline(config, pipeline_yaml_path.parent, pipeline_yaml_path=pipeline_yaml_path)

    click.echo(f"Resuming session: {session_id}")
    click.echo(f"Pipeline: {session.pipeline_name}")
    click.echo(f"Gated at: {session.gated_agent_id}")
    click.echo(f"Gate round: {session.gate_round}")
    click.echo()

    try:
        result = pipeline.resume(
            session_id=session_id,
            store=store,
            feedback=feedback,
            approve=approve,
            reject=reject,
            gate_mode=gate_mode,
            gate_dir=Path(gate_dir) if gate_dir else None,
            progress=CLIProgressCallback(),
        )
    except SimpleCodeError as e:
        click.echo(f"\nError: {e}", err=True)
        sys.exit(1)

    click.echo()
    if result.status == "checkpointed":
        click.echo(f"Pipeline re-checkpointed at gate: {result.checkpoint_agent}")
        click.echo(f"Session: {result.session_id}")
        click.echo()
        click.echo("Resume with:")
        click.echo(f"  simplecode resume -s {result.session_id} --approve")
        click.echo(f"  simplecode resume -s {result.session_id} --feedback \"your feedback\"")
    elif result.gate_rejected_at:
        click.echo(f"Pipeline stopped at gate: {result.gate_rejected_at}")
    else:
        click.echo("Pipeline complete.")
    click.echo(f"Agents completed: {result.agents_completed}/{result.total_agents}")
    click.echo(f"Output: {result.output_dir}")


@main.command()
@click.option(
    "--database-url",
    default=None,
    envvar=_ENV_VAR,
    help=f"Database URL (or set {_ENV_VAR})",
)
@click.option(
    "--status",
    "filter_status",
    default=None,
    type=click.Choice(["running", "waiting", "resumed", "completed", "rejected"]),
    help="Filter by session status",
)
def sessions(database_url: str | None, filter_status: str | None) -> None:
    """List pipeline sessions from the database."""
    store = _get_store(database_url, require=True)
    assert store is not None

    try:
        rows = store.list_sessions(status=filter_status)
    except SimpleCodeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if not rows:
        click.echo("No sessions found.")
        return

    click.echo(f"{'SESSION':<12} {'PIPELINE':<25} {'STATUS':<12} {'GATED AT':<15} {'UPDATED'}")
    click.echo("-" * 80)
    for s in rows:
        updated = s.updated_at.strftime("%Y-%m-%d %H:%M") if s.updated_at else ""
        gated = s.gated_agent_id or ""
        click.echo(f"{s.session_id:<12} {s.pipeline_name:<25} {s.status:<12} {gated:<15} {updated}")


@main.command()
@click.argument("name", default="my-pipeline")
@click.option("--agents", "num_agents", default=3, type=int, help="Number of agents to scaffold")
def init(name: str, num_agents: int) -> None:
    """Scaffold a new pipeline directory with numbered prompts."""
    pipeline_dir = Path(name)
    pipeline_dir.mkdir(exist_ok=True)

    agent_ids = _default_agent_ids(num_agents)

    for agent_id in agent_ids:
        prompts_dir = pipeline_dir / "prompts" / agent_id
        prompts_dir.mkdir(parents=True, exist_ok=True)

        p1 = prompts_dir / "1.md"
        if not p1.exists():
            p1.write_text(
                f"You are the '{agent_id}' agent. "
                f"Analyse the project and produce a structured overview.\n\n"
                f"Be thorough but concise.\n"
            )

        p2 = prompts_dir / "2.md"
        if not p2.exists():
            p2.write_text(
                "Based on your analysis, summarise your findings as clean markdown.\n\n"
                "Include key decisions, risks, and next steps.\n"
            )

    agents_yaml = []
    for i, agent_id in enumerate(agent_ids):
        entry = f"  - id: {agent_id}\n    prompts: prompts/{agent_id}/\n"
        if i == 0:
            entry += "    gate: human\n    permissions: read-only\n    timeout: 86400\n"
        else:
            entry += "    permissions: write\n    timeout: 86400\n"
        agents_yaml.append(entry)

    pipeline_yaml = pipeline_dir / "pipeline.yaml"
    if not pipeline_yaml.exists():
        pipeline_yaml.write_text(
            f"name: {name}\n"
            f"description: Auto-generated pipeline with {num_agents} agents\n\n"
            f"# setup: setup.sh\n\n"
            f"agents:\n" + "\n".join(agents_yaml)
        )

    setup_sh = pipeline_dir / "setup.sh"
    if not setup_sh.exists():
        setup_sh.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n\n"
            "# Install any dependencies needed by your pipeline agents.\n"
            "# Examples:\n"
            "#   pip install playwright\n"
            "#   playwright install chromium\n"
            "#   npm install\n"
        )
        setup_sh.chmod(0o755)

    click.echo(f"Scaffolded pipeline in ./{name}/")
    click.echo()
    click.echo(f"  {pipeline_yaml}")
    click.echo(f"  {setup_sh}")
    for agent_id in agent_ids:
        click.echo(f"  {pipeline_dir}/prompts/{agent_id}/1.md")
        click.echo(f"  {pipeline_dir}/prompts/{agent_id}/2.md")
    click.echo()
    click.echo("Run it:")
    click.echo(f"  simplecode run -f {pipeline_yaml} -r claude -w /path/to/target -t 86400")


@main.command("list")
@click.option(
    "-f",
    "--file",
    "pipeline_file",
    required=True,
    help="Path to pipeline.yaml",
)
def list_pipeline(pipeline_file: str) -> None:
    """Show agents and prompt counts for a pipeline."""
    pipeline_path = Path(pipeline_file).resolve()
    config = PipelineConfig.load(pipeline_path)
    pipeline_dir = pipeline_path.parent

    click.echo(f"Pipeline: {config.name}")
    if config.description:
        click.echo(f"Description: {config.description}")
    if config.setup:
        click.echo(f"Setup: {config.setup}")
    click.echo()

    for i, agent in enumerate(config.agents, 1):
        prompts_dir = pipeline_dir / agent.prompts
        try:
            prompts = discover_prompts(prompts_dir)
            count = len(prompts)
        except Exception:
            count = 0

        gate_marker = f" [gate: {agent.gate}]" if agent.gate else ""
        model_marker = f" model={agent.model}" if agent.model else ""
        click.echo(
            f"  {i}. {agent.id}: {count} prompts, "
            f"perm={agent.permissions}{model_marker}{gate_marker}"
        )


@main.command()
@click.option("--port", default=8080, type=int, help="Port to listen on")
@click.option(
    "--webhook-secret",
    default=None,
    envvar="SIMPLECODE_WEBHOOK_SECRET",
    help="GitHub webhook secret for signature validation",
)
@click.option(
    "--pipeline-map",
    required=True,
    help='JSON mapping of keywords to pipeline dirs, e.g. \'{"triage":"examples/github-issue-triage"}\'',
)
@click.option(
    "--default-pipeline",
    default=None,
    help="Pipeline dir for issue assignment triggers",
)
@click.option(
    "--github-user",
    default=None,
    envvar="GITHUB_USER",
    help="GitHub username for assignment matching",
)
@click.option(
    "-r",
    "--runtime",
    type=click.Choice(SUPPORTED_RUNTIMES),
    default="claude",
    help="Default CLI runtime for all agents",
)
@click.option("-w", "--working-dir", default=None, help="Working directory (if omitted, repos are auto-cloned)")
@click.option("-o", "--output", default="./pipeline-output", help="Output directory")
@click.option("-t", "--timeout", default=86400, type=int, help="Default timeout per agent (seconds)")
@click.option("-m", "--model", default=None, help="Default model (overridable per-agent in YAML)")
@click.option(
    "--permissions",
    type=click.Choice(["read-only", "write", "full"]),
    default="write",
    help="Default permission level",
)
@click.option(
    "--repos-dir",
    default=None,
    type=click.Path(),
    help="Base directory for auto-cloned repos (default: ~/.simplecode/repos/)",
)
@click.option(
    "--database-url",
    default=None,
    envvar=_ENV_VAR,
    help=f"Database URL (or set {_ENV_VAR}). PostgreSQL or SQLite path.",
)
def serve(
    port: int,
    webhook_secret: str | None,
    pipeline_map: str,
    default_pipeline: str | None,
    github_user: str | None,
    runtime: str,
    working_dir: str | None,
    output: str,
    timeout: int,
    model: str | None,
    permissions: str,
    repos_dir: str | None,
    database_url: str | None,
) -> None:
    """Start a webhook server that triggers pipelines from GitHub events."""
    from simplecode.webhook import WebhookConfig, _REPOS_BASE, run_server

    try:
        parsed_map = json.loads(pipeline_map)
    except json.JSONDecodeError as e:
        click.echo(f"Error: --pipeline-map is not valid JSON: {e}", err=True)
        sys.exit(1)

    if not isinstance(parsed_map, dict):
        click.echo("Error: --pipeline-map must be a JSON object", err=True)
        sys.exit(1)

    resolved_url = _resolve_database_url(database_url)
    resolved_repos_dir = Path(repos_dir) if repos_dir else _REPOS_BASE

    config = WebhookConfig(
        pipeline_map=parsed_map,
        default_pipeline=default_pipeline,
        runtime=runtime,
        working_dir=working_dir,
        output_dir=output,
        timeout=timeout,
        model=model,
        permissions=permissions,
        port=port,
        webhook_secret=webhook_secret,
        github_user=github_user,
        database_url=resolved_url,
        repos_dir=resolved_repos_dir,
    )

    click.echo(f"Starting webhook server on port {port}")
    click.echo(f"Pipeline map: {json.dumps(parsed_map, indent=2)}")
    if default_pipeline:
        click.echo(f"Default pipeline: {default_pipeline}")
    if github_user:
        click.echo(f"GitHub user: {github_user}")
    click.echo(f"Runtime: {runtime}")
    if working_dir:
        click.echo(f"Working dir: {working_dir}")
    else:
        click.echo(f"Repos dir: {resolved_repos_dir} (auto-clone)")
    click.echo()
    click.echo("Trigger pipelines by commenting on an issue:")
    for keyword in parsed_map:
        click.echo(f"  @simplecode {keyword}")
    click.echo()
    click.echo("Configure your GitHub webhook:")
    click.echo(f"  URL: http://<your-host>:{port}/webhook")
    click.echo("  Content type: application/json")
    click.echo("  Events: Issues, Issue comments")
    if webhook_secret:
        click.echo("  Secret: (configured)")
    click.echo()

    run_server(config)


@main.command()
@click.option(
    "--repos",
    required=True,
    help='Comma-separated list of repos to poll, e.g. "owner/repo1,owner/repo2"',
)
@click.option("--interval", default=30, type=int, help="Poll interval in seconds (default: 30)")
@click.option(
    "--pipeline-map",
    required=True,
    help='JSON mapping of keywords to pipeline dirs, e.g. \'{"triage":"examples/github-issue-triage"}\'',
)
@click.option("--default-pipeline", default=None, help="Pipeline dir for issue assignment triggers")
@click.option("--github-user", default=None, envvar="GITHUB_USER", help="GitHub username for assignment matching")
@click.option("-r", "--runtime", type=click.Choice(SUPPORTED_RUNTIMES), default="claude", help="Default CLI runtime")
@click.option("-w", "--working-dir", default=None, help="Working directory (if omitted, repos are auto-cloned)")
@click.option("-o", "--output", default="./pipeline-output", help="Output directory")
@click.option("-t", "--timeout", default=86400, type=int, help="Default timeout per agent (seconds)")
@click.option("-m", "--model", default=None, help="Default model (overridable per-agent in YAML)")
@click.option(
    "--permissions",
    type=click.Choice(["read-only", "write", "full"]),
    default="write",
    help="Default permission level",
)
@click.option("--repos-dir", default=None, type=click.Path(), help="Base directory for auto-cloned repos")
@click.option("--database-url", default=None, envvar=_ENV_VAR, help=f"Database URL (or set {_ENV_VAR})")
def poll(
    repos: str,
    interval: int,
    pipeline_map: str,
    default_pipeline: str | None,
    github_user: str | None,
    runtime: str,
    working_dir: str | None,
    output: str,
    timeout: int,
    model: str | None,
    permissions: str,
    repos_dir: str | None,
    database_url: str | None,
) -> None:
    """Poll GitHub for @simplecode mentions. No webhook or tunnel needed."""
    from simplecode.webhook import WebhookConfig, _REPOS_BASE, run_poller

    try:
        parsed_map = json.loads(pipeline_map)
    except json.JSONDecodeError as e:
        click.echo(f"Error: --pipeline-map is not valid JSON: {e}", err=True)
        sys.exit(1)

    if not isinstance(parsed_map, dict):
        click.echo("Error: --pipeline-map must be a JSON object", err=True)
        sys.exit(1)

    repo_list = [r.strip() for r in repos.split(",") if r.strip()]
    if not repo_list:
        click.echo("Error: --repos must contain at least one repo", err=True)
        sys.exit(1)

    resolved_url = _resolve_database_url(database_url)
    resolved_repos_dir = Path(repos_dir) if repos_dir else _REPOS_BASE

    config = WebhookConfig(
        pipeline_map=parsed_map,
        default_pipeline=default_pipeline,
        runtime=runtime,
        working_dir=working_dir,
        output_dir=output,
        timeout=timeout,
        model=model,
        permissions=permissions,
        github_user=github_user,
        database_url=resolved_url,
        repos_dir=resolved_repos_dir,
    )

    click.echo(f"Polling {len(repo_list)} repo(s) every {interval}s")
    for r in repo_list:
        click.echo(f"  - {r}")
    click.echo(f"Pipeline map: {json.dumps(parsed_map, indent=2)}")
    click.echo(f"Runtime: {runtime}")
    if working_dir:
        click.echo(f"Working dir: {working_dir}")
    else:
        click.echo(f"Repos dir: {resolved_repos_dir} (auto-clone)")
    click.echo()
    click.echo("Trigger pipelines by commenting on an issue:")
    for keyword in parsed_map:
        click.echo(f"  @simplecode {keyword}")
    click.echo()

    run_poller(config, repo_list, interval)


def _default_agent_ids(n: int) -> list[str]:
    defaults = ["planner", "executor", "reviewer", "review-tasks", "fixer"]
    if n <= len(defaults):
        return defaults[:n]
    return defaults + [f"agent-{i}" for i in range(len(defaults) + 1, n + 1)]
