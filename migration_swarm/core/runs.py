"""Run directories: runs/<run-id>/{work (local clone), run.json, status.json, report.*, ...}."""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import subprocess
from pathlib import Path

from migration_swarm.core.budget import CostMeter
from migration_swarm.core.context import RepoSpec, RunContext

_CHECKOUT = Path(__file__).resolve().parents[2]
HOME_DIR = Path(os.environ.get("MSW_HOME", Path.home() / ".migration-swarm"))
# A source checkout keeps runs next to the code; an installed package keeps them in ~/.migration-swarm/runs.
RUNS_DIR = Path(os.environ.get("MSW_RUNS_DIR") or (_CHECKOUT / "runs" if (_CHECKOUT / "pyproject.toml").exists()
                                                    else HOME_DIR / "runs"))


def new_run_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def create(source_repo: str, migration: str, repo: RepoSpec, cfg: dict, run_id: str | None = None,
           ref: str | None = None, base: Path | None = None) -> RunContext:
    run_id = run_id or new_run_id()
    run_dir = (base or RUNS_DIR) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    work = run_dir / "work"
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", source_repo, str(work)], check=True)
    if ref:
        subprocess.run(["git", "checkout", "-q", "--detach", ref], cwd=work, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", f"migration-swarm/{run_id}"], cwd=work, check=True)
    for k, v in (("user.name", "migration-swarm"), ("user.email", "migration-swarm@localhost")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    meter = CostMeter(cfg["budget_usd"], cfg["prices"], cfg.get("per_file_token_cap"))
    ctx = RunContext(run_id, migration, run_dir, work, source_repo, repo, cfg, meter)
    ctx.save_meta()
    (run_dir / "config.json").write_text(json.dumps(cfg, indent=1))
    return ctx


def load(run_id: str) -> RunContext:
    run_dir = RUNS_DIR / run_id
    meta = json.loads((run_dir / "run.json").read_text())
    cfg = json.loads((run_dir / "config.json").read_text())
    meter = CostMeter(cfg["budget_usd"], cfg["prices"], cfg.get("per_file_token_cap"))
    cost = run_dir / "cost.json"
    if cost.exists():
        meter.restore(json.loads(cost.read_text()))
    return RunContext(meta["run_id"], meta["migration"], run_dir, Path(meta["workdir"]), meta["source_repo"],
                      RepoSpec(**meta["repo"]), cfg, meter)


def write_status(ctx: RunContext, **fields) -> None:
    p = ctx.run_dir / "status.json"
    cur = json.loads(p.read_text()) if p.exists() else {}
    cur.update(fields)
    cur["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    cur["cost"] = ctx.meter.snapshot()
    p.write_text(json.dumps(cur, indent=1))
    (ctx.run_dir / "cost.json").write_text(json.dumps(ctx.meter.snapshot()))
