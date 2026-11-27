"""migration-swarm CLI."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

import typer
import yaml

from migration_swarm import migrations
from migration_swarm.core import config as config_mod, graph, runs
from migration_swarm.core.context import RepoSpec
from migration_swarm.core.llm import LLM

app = typer.Typer(add_completion=False, invoke_without_command=True,
                  help="Migrate codebases with a swarm of agents and a deterministic judge. Run with no command for the interactive mode.")


@app.callback()
def _root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _load_dotenv()
        from migration_swarm import tui
        raise typer.Exit(tui.main())


def _load_dotenv() -> None:
    """Env vars from ./.env, then ~/.migration-swarm/.env (existing env vars always win)."""
    for env in (Path(".env"), runs.HOME_DIR / ".env"):
        if env.exists():
            for line in env.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    if v.strip():
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _manifest_entry(manifest: Path, name: str) -> dict:
    entries = {r["name"]: r for r in yaml.safe_load(manifest.read_text())["repos"]}
    if name not in entries:
        raise typer.BadParameter(f"{name} not in {manifest}; have {', '.join(entries)}")
    return entries[name]


@app.command("list-migrations")
def list_migrations() -> None:
    """Show the available migrations."""
    for name in migrations.MIGRATIONS:
        pack = migrations.load(name)
        typer.echo(f"{name}\t{pack.DESCRIPTION}")


@app.command()
def run(
    repo_path: str = typer.Argument(..., help="Local path or git URL of the repo to migrate (it is cloned; never modified)."),
    migration: str = typer.Option(..., "--migration", "-m"),
    budget: Optional[float] = typer.Option(None, help="Hard budget in USD (default from config: 5)."),
    concurrency: Optional[int] = typer.Option(None, help="Max parallel workers (default 8)."),
    config: Optional[Path] = typer.Option(None, help="YAML config overriding config/default.yaml."),
    ref: Optional[str] = typer.Option(None, help="Commit/branch to migrate (default: HEAD)."),
    image: Optional[str] = typer.Option(None, help="Docker image for install/tests."),
    install: Optional[str] = typer.Option(None), test: Optional[str] = typer.Option(None),
    test_runner: Optional[str] = typer.Option(None),
    source_dir: Optional[list[str]] = typer.Option(None, help="Source dir(s) to migrate (repeatable)."),
    test_path: Optional[list[str]] = typer.Option(None, help="Protected test paths/globs (repeatable)."),
    manifest: Optional[Path] = typer.Option(None, help="Take repo settings from a benchmark manifest..."),
    name: Optional[str] = typer.Option(None, help="...entry with this name."),
    run_id: Optional[str] = typer.Option(None),
    provider: Optional[str] = typer.Option(None, help="Model provider: claude | gemini | openai (default claude)."),
    backend: Optional[str] = typer.Option(None, help="LLM backend: 'api' (ANTHROPIC_API_KEY, billed) or 'plan' (Claude Agent SDK on your Claude plan login, not billed)."),
) -> None:
    """Start a migration run. Output: a branch in runs/<run-id>/work plus runs/<run-id>/report.{json,md}."""
    _load_dotenv()
    pack = migrations.load(migration)
    cfg = config_mod.load(config, budget_usd=budget, concurrency=concurrency, llm_backend=backend, provider=provider)
    if manifest and name:
        e = _manifest_entry(manifest, name)
        repo_path = repo_path if repo_path != "-" else e["url"]
        spec = RepoSpec(e["image"], e["install"], e["test"], e["test_runner"], e["source_dirs"], e.get("test_paths", []))
        ref = ref or e["pre_migration_sha"]
    else:
        local = Path(repo_path)
        spec = pack.detect_repo_spec(local, source_dir) if local.exists() else None
        if spec is None:
            raise typer.BadParameter("for a remote repo, pass --image/--install/--test/--test-runner/--source-dir")
    spec = RepoSpec(image or spec.image, install or spec.install, test or spec.test, test_runner or spec.test_runner,
                    list(source_dir) if source_dir else spec.source_dirs, list(test_path) if test_path else spec.test_paths)
    from migration_swarm.core.providers import PROVIDERS
    prov = PROVIDERS[cfg["provider"]]
    if cfg.get("llm_backend", "api") == "api" and not prov.key():
        typer.echo(f"{' or '.join(prov.env_keys)} is not set (env var or .env)."
                   + (" Or use --backend plan to run on your Claude plan." if prov.name == "claude" else ""), err=True)
        raise typer.Exit(2)
    ctx = runs.create(repo_path, migration, spec, cfg, run_id=run_id, ref=ref)
    typer.echo(f"run id: {ctx.run_id}  (dir: {ctx.run_dir})")
    _execute(ctx, pack, resume=False)


def _execute(ctx, pack, resume: bool) -> None:
    ctx.llm = LLM(ctx.cfg, ctx.meter, ctx.run_id, ctx.migration, log=ctx.log, emit=ctx.emit)
    asyncio.run(graph.execute(ctx, pack, resume=resume))
    typer.echo((ctx.run_dir / "report.md").read_text() if (ctx.run_dir / "report.md").exists() else "no report written")


@app.command()
def resume(run_id: str, budget: Optional[float] = typer.Option(None, help="Raise the budget (USD) before resuming."),
           backend: Optional[str] = typer.Option(None, help="LLM backend: 'api' (ANTHROPIC_API_KEY, billed) or 'plan' (Claude Agent SDK on your Claude plan login, not billed).")) -> None:
    """Resume a crashed, cancelled, out-of-budget or plan-limited run from its checkpoints."""
    _load_dotenv()
    ctx = runs.load(run_id)
    if backend:
        ctx.cfg["llm_backend"] = backend
        (ctx.run_dir / "config.json").write_text(json.dumps(ctx.cfg, indent=1))
    if budget is not None:
        ctx.cfg["budget_usd"] = budget
        ctx.meter.budget_usd = budget
        (ctx.run_dir / "config.json").write_text(json.dumps(ctx.cfg, indent=1))
    cancel = ctx.run_dir / "CANCEL"
    if cancel.exists():
        cancel.unlink()
    _execute(ctx, migrations.load(ctx.migration), resume=True)


@app.command()
def status(run_id: str) -> None:
    """Show a run's progress and spend."""
    p = runs.RUNS_DIR / run_id / "status.json"
    if not p.exists():
        typer.echo(f"no such run: {run_id}", err=True)
        raise typer.Exit(1)
    s = json.loads(p.read_text())
    c = s.get("cost", {})
    typer.echo(json.dumps({k: v for k, v in s.items() if k != "cost"}, indent=1))
    typer.echo(f"spent ${c.get('spent_usd', 0):.4f} of ${c.get('budget_usd', 0):.2f} in {c.get('calls', 0)} calls")


@app.command()
def report(run_id: str, json_out: bool = typer.Option(False, "--json")) -> None:
    """Print a run's report."""
    d = runs.RUNS_DIR / run_id
    p = d / ("report.json" if json_out else "report.md")
    if not p.exists():
        typer.echo(f"no report for {run_id} yet (see `migration-swarm status {run_id}`)", err=True)
        raise typer.Exit(1)
    typer.echo(p.read_text())


@app.command()
def cancel(run_id: str) -> None:
    """Ask a running run to stop cleanly (it still writes a partial report)."""
    d = runs.RUNS_DIR / run_id
    if not d.exists():
        raise typer.Exit(1)
    (d / "CANCEL").write_text("cancel requested\n")
    typer.echo("cancel requested")


@app.command("eval")
def eval_cmd(
    migration: str = typer.Option(..., "--migration", "-m"),
    manifest: Path = typer.Option(..., help="Benchmark manifest (YAML)."),
    systems: str = typer.Option("all", help="Comma-separated systems, or 'all'."),
    runs: int = typer.Option(3, help="Runs per (system, repo)."),
    repos: Optional[str] = typer.Option(None, help="Comma-separated repo names (default: all in the manifest)."),
    budget: Optional[float] = typer.Option(None, help="USD budget per run (swarm and Claude baselines)."),
    out: Optional[Path] = typer.Option(None, help="Output dir (default evals/<migration>/<date>)."),
    config: Optional[Path] = typer.Option(None),
    provider: Optional[str] = typer.Option(None, help="Model provider: claude | gemini | openai (default claude)."),
    backend: Optional[str] = typer.Option(None, help="LLM backend: 'api' (ANTHROPIC_API_KEY, billed) or 'plan' (Claude Agent SDK on your Claude plan login, not billed)."),
) -> None:
    """Run every system on every repo and write one comparison table (markdown + CSV)."""
    from migration_swarm.core import evals

    _load_dotenv()
    out_dir = evals.run_eval(migration, manifest, [s.strip() for s in systems.split(",")], runs_per_cell=runs,
                             repos=[r.strip() for r in repos.split(",")] if repos else None, budget=budget,
                             out_dir=out, config=config, backend=backend, provider=provider)
    typer.echo((out_dir / "results.md").read_text())


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
