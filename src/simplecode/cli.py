"""CLI for simplecode — orchestrate multi-agent pipelines."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import click

from simplecode.errors import PipelineConfigError, SimpleCodeError, StoreError
from simplecode.pipeline import Pipeline, PipelineConfig, PipelineResult
from simplecode.progress import CLIProgressCallback
from simplecode.registry import (
    ensure_global_pipelines_dir,
    ensure_project_pipelines_dir,
    list_pipelines,
    resolve_pipeline,
)
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


def _echo_result(result: PipelineResult, *, resumed: bool = False) -> None:
    click.echo()
    if result.status == "checkpointed":
        verb = "re-checkpointed" if resumed else "checkpointed"
        click.echo(f"Pipeline {verb} at gate: {result.checkpoint_agent}")
        click.echo(f"Session: {result.session_id}")
        click.echo()
        click.echo("Resume with:")
        click.echo(f"  simplecode resume -s {result.session_id} --approve")
        click.echo(f'  simplecode resume -s {result.session_id} --feedback "your feedback"')
    elif result.gate_rejected_at:
        click.echo(f"Pipeline stopped at gate: {result.gate_rejected_at}")
    else:
        click.echo("Pipeline complete.")
    click.echo(f"Agents completed: {result.agents_completed}/{result.total_agents}")
    click.echo(f"Output: {result.output_dir}")


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
@click.argument("pipeline", required=False, default=None)
@click.argument("inputs", nargs=-1)
@click.option(
    "-f",
    "--file",
    "pipeline_file",
    default=None,
    help="Deprecated: pass the path as PIPELINE instead",
)
@click.option(
    "-r",
    "--runtime",
    type=click.Choice(SUPPORTED_RUNTIMES),
    default="claude",
    help="Agent runtime",
)
@click.option("-w", "--working-dir", default=".", help="Repo the agent works in")
@click.option("-o", "--output", default="./pipeline-output", help="Output directory")
@click.option("-t", "--timeout", default=86400, type=int, help="Timeout per agent (seconds)")
@click.option("-m", "--model", default=None, help="Default model")
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
    help="Extra input file(s); or pass files as trailing args",
)
@click.option(
    "--gate-mode",
    type=click.Choice(["stdin", "file", "checkpoint"]),
    default="stdin",
    help="Gate mode: stdin, file, or checkpoint",
)
@click.option(
    "--gate-dir",
    default=None,
    type=click.Path(),
    help="Gate artifacts dir (--gate-mode file)",
)
@click.option(
    "--database-url",
    default=None,
    envvar=_ENV_VAR,
    help=f"Database URL (or set {_ENV_VAR})",
)
@click.option(
    "--code-context",
    default=None,
    help="CocoIndex query; search working-dir and inject hits as an input file",
)
@click.option(
    "--code-refresh",
    is_flag=True,
    help="With --code-context: refresh index before search",
)
def run(
    pipeline: str | None,
    inputs: tuple[str, ...],
    pipeline_file: str | None,
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
    code_context: str | None,
    code_refresh: bool,
) -> None:
    """Run a pipeline by name or path.

    \b
      simplecode run feature-dev
      simplecode run feature-dev TASK.md
      simplecode run triage "Fix login timeout for SSO users"
      simplecode run path/to/pipeline.yaml TASK.md
    """
    # ponytail: -f kept so old scripts don't break; prefer positional PIPELINE
    target = pipeline_file or pipeline
    if not target:
        click.echo(
            'Usage: simplecode run <name-or-path> [TASK.md | "inline text" ...]',
            err=True,
        )
        sys.exit(1)

    try:
        ref = resolve_pipeline(target, start=Path(working_dir))
    except PipelineConfigError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    try:
        resolved_inputs = (
            _resolve_run_inputs(
                working_dir,
                inputs,
                input_files,
                blob_dir=Path(output),
            )
            or []
        )
    except click.ClickException as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if code_context:
        from simplecode.cocoindex import CocoIndexError, write_context_file

        ctx_path = Path(output) / "_cocoindex_context.md"
        try:
            write_context_file(
                Path(working_dir).resolve(),
                code_context,
                dest=ctx_path,
                refresh=code_refresh,
            )
        except CocoIndexError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        resolved_inputs = [*resolved_inputs, str(ctx_path)]
        click.echo(f"Code context: {ctx_path}")

    try:
        pipeline_path = ref.yaml_path
        config = PipelineConfig.load(pipeline_path)
        pipeline_dir = ref.path
        pipeline_obj = Pipeline(config, pipeline_dir, pipeline_yaml_path=pipeline_path)
    except SimpleCodeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    store = _get_store(database_url, require=(gate_mode == "checkpoint"))

    click.echo(f"Pipeline: {config.name} ({ref.scope})")
    click.echo(f"Source:   {pipeline_path}")
    click.echo(f"Agents:   {len(config.agents)}")
    click.echo(f"Runtime:  {runtime}")
    click.echo(f"Model:    {model or 'default'}")
    if resolved_inputs:
        click.echo(f"Inputs:   {', '.join(resolved_inputs)}")
    click.echo(f"Output:   {output}/")
    if gate_mode == "checkpoint" and store:
        click.echo(f"Store:    {store.backend}")
    click.echo()

    for i, agent in enumerate(config.agents, 1):
        gate_marker = f" [gate: {agent.gate}]" if agent.gate else ""
        agent_model = agent.model or model or "default"
        agent_perm = agent.permissions or permissions
        click.echo(
            f"  {i}. {agent.id} (model={agent_model}, perm={agent_perm}){gate_marker}"
        )
    click.echo()

    try:
        result = pipeline_obj.run(
            runtime=runtime,
            working_dir=working_dir,
            input_files=list[str | Path](resolved_inputs) if resolved_inputs else None,
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

    _echo_result(result)


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

    _echo_result(result, resumed=True)


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
@click.option("-w", "--working-dir", default=".", help="Repo to index")
def sync(working_dir: str) -> None:
    """Index the working directory with cocoindex-code (ccc index)."""
    from simplecode.cocoindex import CocoIndexError, index_codebase

    root = Path(working_dir).resolve()
    click.echo(f"Indexing {root} …")
    try:
        index_codebase(root)
    except CocoIndexError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo("Done.")


@main.command("search")
@click.argument("query")
@click.option("-w", "--working-dir", default=".", help="Repo to search")
@click.option("--refresh", is_flag=True, help="Refresh index before search")
@click.option("--limit", default=10, type=int, help="Max results")
def search_cmd(query: str, working_dir: str, refresh: bool, limit: int) -> None:
    """Semantic code search via cocoindex-code."""
    from simplecode.cocoindex import CocoIndexError, search_codebase

    try:
        out = search_codebase(
            Path(working_dir).resolve(),
            query,
            refresh=refresh,
            limit=limit,
        )
    except CocoIndexError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(out, nl=False)


@main.command()
@click.argument("name", default="my-pipeline")
@click.option("--agents", "num_agents", default=3, type=int, help="Number of agents to scaffold")
@click.option(
    "--global",
    "as_global",
    is_flag=True,
    help="Scaffold into ~/.simplecode/pipelines/<name>/",
)
@click.option(
    "--path",
    "custom_path",
    default=None,
    type=click.Path(),
    help="Scaffold into an explicit directory (legacy / escape hatch)",
)
def init(name: str, num_agents: int, as_global: bool, custom_path: str | None) -> None:
    """Scaffold a pipeline into .simplecode/pipelines/ (project or global).

    \b
    Examples:
      simplecode init feature-dev
      simplecode init feature-dev --global
      simplecode init feature-dev --path ./legacy-dir
    """
    if custom_path and as_global:
        click.echo("Error: --global and --path are mutually exclusive", err=True)
        sys.exit(1)

    if custom_path:
        pipeline_dir = Path(custom_path)
        scope = "path"
    elif as_global:
        pipeline_dir = ensure_global_pipelines_dir() / name
        scope = "global"
    else:
        pipeline_dir = ensure_project_pipelines_dir() / name
        scope = "project"

    pipeline_dir.mkdir(parents=True, exist_ok=True)
    agent_ids = _default_agent_ids(num_agents)
    _scaffold_pipeline(pipeline_dir, name, agent_ids)

    pipeline_yaml = pipeline_dir / "pipeline.yaml"
    click.echo(f"Scaffolded {scope} pipeline: {name}")
    click.echo(f"  {pipeline_dir}/")
    click.echo()
    for agent_id in agent_ids:
        click.echo(f"  prompts/{agent_id}/1.md")
        click.echo(f"  prompts/{agent_id}/2.md")
    click.echo()
    click.echo("Edit prompts, then run:")
    if scope == "path":
        click.echo(f"  simplecode run {pipeline_yaml} TASK.md")
    else:
        click.echo(f"  simplecode run {name} TASK.md")


@main.command("list")
@click.argument("pipeline", required=False, default=None)
@click.option(
    "-f",
    "--file",
    "pipeline_file",
    default=None,
    help="Path to pipeline.yaml (show detail for one pipeline)",
)
@click.option(
    "-w",
    "--working-dir",
    default=".",
    help="Directory used to locate project .simplecode (default: cwd)",
)
def list_pipeline(pipeline: str | None, pipeline_file: str | None, working_dir: str) -> None:
    """List discovered pipelines, or show detail for one.

    \b
    Examples:
      simplecode list
      simplecode list feature-dev
      simplecode list -f path/to/pipeline.yaml
    """
    target = pipeline_file or pipeline
    if target is None:
        refs = list_pipelines(start=Path(working_dir))
        if not refs:
            click.echo("No pipelines found.")
            click.echo()
            click.echo("Create one:")
            click.echo("  simplecode init feature-dev")
            click.echo("  simplecode init feature-dev --global")
            return

        click.echo(f"{'NAME':<28} {'SCOPE':<10} {'PATH'}")
        click.echo("-" * 80)
        for ref in refs:
            click.echo(f"{ref.name:<28} {ref.scope:<10} {ref.path}")
        return

    try:
        ref = resolve_pipeline(target, start=Path(working_dir))
    except PipelineConfigError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    try:
        config = PipelineConfig.load(ref.yaml_path)
        pipeline_dir = ref.path

        click.echo(f"Pipeline: {config.name} ({ref.scope})")
        click.echo(f"Path:     {ref.yaml_path}")
        if config.description:
            click.echo(f"Description: {config.description}")
        if config.setup:
            click.echo(f"Setup: {config.setup}")
        click.echo()

        for i, agent in enumerate(config.agents, 1):
            prompts = discover_prompts(pipeline_dir / agent.prompts)
            gate_marker = f" [gate: {agent.gate}]" if agent.gate else ""
            model_marker = f" model={agent.model}" if agent.model else ""
            perm = agent.permissions or "write"
            click.echo(
                f"  {i}. {agent.id}: {len(prompts)} prompts, "
                f"perm={perm}{model_marker}{gate_marker}"
            )
    except SimpleCodeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


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
    help='JSON mapping of keywords to pipeline dirs, '
    'e.g. \'{"triage":"examples/github-issue-triage"}\'',
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
@click.option(
    "-w",
    "--working-dir",
    default=None,
    help="Working directory (if omitted, repos are auto-cloned)",
)
@click.option("-o", "--output", default="./pipeline-output", help="Output directory")
@click.option(
    "-t", "--timeout", default=86400, type=int, help="Default timeout per agent (seconds)"
)
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
@click.option(
    "--repos",
    default=None,
    help='Comma-separated repos to poll for @simplecode mentions (e.g. "owner/repo1,owner/repo2")',
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
    repos: str | None,
) -> None:
    """Start a webhook server that triggers pipelines from GitHub events."""
    from simplecode.webhook import _REPOS_BASE, WebhookConfig, run_server

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
    poll_repos = [r.strip() for r in repos.split(",") if r.strip()] if repos else []

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
    if poll_repos:
        click.echo(f"Polling {', '.join(poll_repos)} every 30s (webhook optional).")
    click.echo()
    click.echo("Trigger pipelines by commenting on an issue:")
    for keyword in parsed_map:
        click.echo(f"  @simplecode {keyword}")
    click.echo()
    click.echo(f"Webhook (optional): http://<your-host>:{port}/webhook")
    click.echo()

    run_server(config, poll_repos=poll_repos or None)


def _resolve_run_inputs(
    working_dir: str,
    positional: tuple[str, ...],
    flagged: tuple[str, ...],
    *,
    blob_dir: Path,
) -> list[str] | None:
    """Resolve file paths and inline text blobs into input file paths.

    Existing files (cwd or working_dir) stay files. Anything else is written
    under *blob_dir* as ``_input_N.md``. If nothing is passed, use TASK.md or
    ISSUE.md in the working dir when present.
    """
    resolved: list[str] = []
    blob_i = 0

    for item in [*positional, *flagged]:
        path = Path(item)
        candidates = [path] if path.is_absolute() else [Path(working_dir) / item, path]
        file_hit = next((c for c in candidates if c.is_file()), None)
        if file_hit is not None:
            resolved.append(str(file_hit.resolve()))
            continue
        if any(c.exists() and c.is_dir() for c in candidates):
            raise click.ClickException(f"Input path is a directory, not a file: {item}")

        blob_i += 1
        dest = blob_dir / f"_input_{blob_i}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = item if item.endswith("\n") else f"{item}\n"
        dest.write_text(text, encoding="utf-8")
        resolved.append(str(dest.resolve()))

    if resolved:
        return resolved

    # ponytail: convention over -i; drop when people hate auto task files
    for name in ("TASK.md", "ISSUE.md"):
        task = Path(working_dir) / name
        if task.is_file():
            return [str(task.resolve())]
    return None


def _default_agent_ids(n: int) -> list[str]:
    defaults = ["planner", "executor", "reviewer", "review-tasks", "fixer"]
    if n <= len(defaults):
        return defaults[:n]
    return defaults + [f"agent-{i}" for i in range(len(defaults) + 1, n + 1)]


def _scaffold_pipeline(pipeline_dir: Path, name: str, agent_ids: list[str]) -> None:
    """Write pipeline.yaml, setup.sh, and starter prompts into *pipeline_dir*."""
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
            f"description: Auto-generated pipeline with {len(agent_ids)} agents\n\n"
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
