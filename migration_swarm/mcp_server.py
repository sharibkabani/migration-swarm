"""MCP server (stdio): drive migration-swarm from Claude Code or any MCP client.

Runs are long (minutes to hours), so tools never block on them: start_migration launches a detached
`migration-swarm run` process and returns its run id; the client polls get_status.

By default a run stops after analysis and planning (nothing spent) with phase `awaiting_approval`;
show the plan to the user and call approve_plan to start it. Start with: `migration-swarm mcp`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from migration_swarm import migrations
from migration_swarm.core import plansummary, runs
from migration_swarm.core.providers import PROVIDERS

INSTRUCTIONS = """migration-swarm migrates codebases with parallel LLM agents and a deterministic judge (strict type-check,
compiled-output equivalence, the repo's own tests in Docker). Supported migrations: see list_migrations.
Workflow: start_migration (returns run_id; analysis + planning run in the background, nothing is spent) ->
poll get_status until phase is awaiting_approval -> show the user the plan and ask them -> approve_plan ->
poll get_status (every minute or so) -> get_report. The user's repo is never modified: results go to a branch
in a local clone (get_report says where). Needs Docker running."""

server = MCPServer("migration-swarm", instructions=INSTRUCTIONS, version="0.1.0")
LOGS = runs.RUNS_DIR / ".mcp-logs"


def _spawn(args: list[str], run_id: str) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = open(LOGS / f"{run_id}.log", "ab")
    p = subprocess.Popen([sys.executable, "-m", "migration_swarm.cli", *args], stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    (LOGS / f"{run_id}.pid").write_text(str(p.pid))
    return p.pid


def _alive(run_id: str) -> bool:
    pid_file = LOGS / f"{run_id}.pid"
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text())
        os.kill(pid, 0)
        # reap if it is our zombie child
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
            return done == 0
        except ChildProcessError:
            return True
    except (ProcessLookupError, ValueError):
        return False


def _log_tail(run_id: str, n: int = 25) -> str:
    p = LOGS / f"{run_id}.log"
    return "\n".join(p.read_text(errors="replace").splitlines()[-n:]) if p.exists() else ""


def _claude_plan_signed_in() -> bool:
    from migration_swarm.tui import claude_plan_status
    return claude_plan_status() is not None


def _run_dir(run_id: str) -> Path:
    d = runs.RUNS_DIR / run_id
    if not d.is_dir() or "/" in run_id or run_id.startswith("."):
        raise ValueError(f"no such run: {run_id}")
    return d


@server.tool()
def list_migrations() -> list[dict]:
    """The migrations migration-swarm supports, with what each does."""
    out = []
    for name in migrations.MIGRATIONS:
        pack = migrations.load(name)
        out.append({"name": name, "description": pack.DESCRIPTION, "details": getattr(pack, "INTENT_HINT", "")})
    return out


@server.tool()
def start_migration(repo_path: str, migration: str = "js-to-ts", provider: str = "claude", auth: str = "auto",
                    budget_usd: float = 5.0, concurrency: int = 8, source_dirs: list[str] | None = None,
                    review_plan: bool = True) -> dict:
    """Start migrating a local git repo (its last commit is cloned; the repo itself is never modified).

    provider: claude | gemini | openai. auth (Claude only): "plan" uses the user's Claude plan login (not billed,
    counts against plan limits), "api" uses ANTHROPIC_API_KEY, "auto" prefers the plan login when signed in.
    budget_usd is a hard stop. source_dirs: directories to migrate (default: detected, e.g. src/ or lib/).
    review_plan (default true): stop after planning, before anything is spent, so the user can approve.
    Returns the run_id; poll get_status.
    """
    repo = Path(repo_path).expanduser().resolve()
    if not (repo / ".git").exists():
        return {"error": f"{repo} is not a git repository (commit the code first; the run clones the last commit)"}
    if migration not in migrations.MIGRATIONS:
        return {"error": f"unknown migration {migration!r}", "supported": list(migrations.MIGRATIONS)}
    if provider not in PROVIDERS:
        return {"error": f"unknown provider {provider!r}", "supported": list(PROVIDERS)}
    backend = "api"
    if provider == "claude":
        signed_in = _claude_plan_signed_in()
        if auth == "plan" or (auth == "auto" and signed_in):
            if not signed_in:
                return {"error": "not signed in to a Claude plan; run `claude auth login`, or use auth='api'"}
            backend = "plan"
    if backend == "api" and not PROVIDERS[provider].key():
        return {"error": f"{' or '.join(PROVIDERS[provider].env_keys)} is not set (env or ~/.migration-swarm/.env)"}
    run_id = runs.new_run_id()
    args = ["run", str(repo), "-m", migration, "--run-id", run_id, "--provider", provider, "--backend", backend,
            "--budget", str(budget_usd), "--concurrency", str(concurrency)]
    for d in source_dirs or []:
        args += ["--source-dir", d]
    if review_plan:
        args.append("--plan-only")
    _spawn(args, run_id)
    return {"run_id": run_id, "provider": provider, "auth": backend,
            "next": "poll get_status; analysis installs dependencies in Docker and can take a few minutes"
                    + ("; it will stop at phase awaiting_approval with the plan" if review_plan else "")}


@server.tool()
def approve_plan(run_id: str) -> dict:
    """Start a run that is waiting at phase awaiting_approval (after the user approved its plan)."""
    d = _run_dir(run_id)
    st = json.loads((d / "status.json").read_text()) if (d / "status.json").exists() else {}
    if st.get("phase") not in ("awaiting_approval", "declined"):
        return {"error": f"run is in phase {st.get('phase')!r}, not awaiting approval"}
    if _alive(run_id):
        return {"error": "the planning process is still running; wait for awaiting_approval"}
    _spawn(["approve", run_id], run_id)
    return {"run_id": run_id, "started": True, "next": "poll get_status (every minute or so)"}


@server.tool()
def get_status(run_id: str) -> dict:
    """Phase, progress, spend and recent agent activity of a run (plus the plan when awaiting approval)."""
    if not (runs.RUNS_DIR / run_id).is_dir() and (LOGS / f"{run_id}.pid").exists():
        alive = _alive(run_id)
        out = {"run_id": run_id, "phase": "starting", "process_running": alive}
        if not alive:
            out["warning"] = "the run process exited before creating the run"
            out["log_tail"] = _log_tail(run_id)
        return out
    d = _run_dir(run_id)
    st = json.loads((d / "status.json").read_text()) if (d / "status.json").exists() else {"phase": "starting"}
    alive = _alive(run_id)
    out = {"run_id": run_id, "process_running": alive, **{k: v for k, v in st.items() if k != "cost"}}
    cost = st.get("cost") or {}
    out["spent_usd"], out["budget_usd"] = cost.get("spent_usd"), cost.get("budget_usd")
    if st.get("phase") == "awaiting_approval":
        out["plan"] = plansummary.load(d)
    ev_path = d / "events.jsonl"
    if ev_path.exists():
        lines = ev_path.read_text().splitlines()[-200:]
        recent = []
        for ln in lines:
            ev = json.loads(ln)
            if ev["kind"] == "verdict":
                recent.append(f"{ev['status']}: {', '.join(ev['files'])}"
                              + (f" ({str(ev['reasons'][0])[:160]})" if ev.get("reasons") and ev["status"] != "accepted" else ""))
            elif ev["kind"] in ("phase", "layer", "steward", "plan_wait"):
                recent.append(f"{ev['kind']}: " + ", ".join(f"{k}={v}" for k, v in ev.items() if k not in ("t", "kind")))
        out["recent"] = recent[-15:]
    if not alive and st.get("phase") not in ("done", "awaiting_approval", "declined"):
        out["warning"] = "the run process is not running and the run is not finished"
        out["log_tail"] = _log_tail(run_id)
        out["hint"] = f"continue it with `migration-swarm resume {run_id}` or start a new run"
    return out


@server.tool()
def get_report(run_id: str) -> dict:
    """The final (or partial) report of a run: outcome, files, type errors, tests, cost, and where the branch is."""
    d = _run_dir(run_id)
    if not (d / "report.md").exists():
        return {"error": "no report yet; the run hasn't finished (see get_status)"}
    r = json.loads((d / "report.json").read_text())
    return {"report_markdown": (d / "report.md").read_text(), "complete": r["complete"], "stop_reason": r["stop_reason"],
            "branch": r.get("branch"), "workdir": r.get("workdir"),
            "bring_into_repo": (f"git fetch {r['workdir']} {r['branch']}:{r['branch']}" if r.get("branch") else None)}


@server.tool()
def cancel(run_id: str) -> dict:
    """Ask a run to stop cleanly; it finishes the current step and writes a partial report (resumable)."""
    d = _run_dir(run_id)
    st = json.loads((d / "status.json").read_text()) if (d / "status.json").exists() else {}
    if st.get("phase") == "awaiting_approval":
        runs_ctx = runs.load(run_id)
        runs.write_status(runs_ctx, phase="declined")
        return {"run_id": run_id, "cancelled": True, "note": "the plan was not approved; nothing was spent"}
    (d / "CANCEL").write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
    return {"run_id": run_id, "cancel_requested": True, "process_running": _alive(run_id)}


@server.tool()
def list_runs(limit: int = 10) -> list[dict]:
    """Recent runs, newest first, with their phase and spend."""
    out = []
    if not runs.RUNS_DIR.exists():
        return out
    dirs = sorted((p for p in runs.RUNS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")),
                  key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    for d in dirs:
        st = json.loads((d / "status.json").read_text()) if (d / "status.json").exists() else {}
        meta = json.loads((d / "run.json").read_text()) if (d / "run.json").exists() else {}
        out.append({"run_id": d.name, "migration": meta.get("migration"), "repo": meta.get("source_repo"),
                    "phase": st.get("phase"), "stop_reason": st.get("stop_reason"),
                    "spent_usd": (st.get("cost") or {}).get("spent_usd")})
    return out


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
