"""Eval harness (core): every system on every repo, N runs each, scored by the pack's uniform evaluator.

Output: evals/<migration>/<date>/
    results.md, results.csv      one row per (system, repo): mean and spread over runs
    runs.csv                     one row per run (nothing is dropped or cherry-picked)
    work/                        (gitignored) prepared repos, each run's tree, raw metrics, logs

The pack supplies: eval_prepare(entry, clone_src, prep_dir), eval_score(entry, prep, tree, eval_dir),
BASELINES {name: fn(entry, prep, clone_src, run_dir, cfg) -> SystemOutput}, EVAL_METRICS (metric keys
to aggregate) and EVAL_DEFINITIONS (text for the report). The swarm systems are built in here.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import json
import math
import statistics
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from migration_swarm.core import config as config_mod, graph, runs
from migration_swarm.core.llm import LLM

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class SystemOutput:
    final_tree: Path
    wall_s: float
    cost_usd: float | None
    tokens: dict | None
    extra: dict = field(default_factory=dict)
    error: str | None = None


def swarm_system(concurrency: int):
    def run(entry: dict, prep: dict, clone_src: Path, run_dir: Path, cfg: dict, pack=None) -> SystemOutput:
        c = {**cfg, "concurrency": concurrency}
        spec = pack.evaluate.spec_from_entry(entry)
        ctx = runs.create(str(clone_src), pack.NAME, spec, c, run_id="swarm", ref=entry["pre_migration_sha"],
                          base=run_dir)
        ctx.llm = LLM(c, ctx.meter, ctx.run_id, ctx.migration, log=ctx.log, emit=ctx.emit)
        start = time.monotonic()
        asyncio.run(graph.execute(ctx, pack))
        rep = json.loads((ctx.run_dir / "report.json").read_text())
        # wall time excludes time spent paused on Claude plan usage limits (reported separately)
        return SystemOutput(final_tree=ctx.workdir, wall_s=time.monotonic() - start - ctx.llm.paused_s, cost_usd=rep["cost"]["spent_usd"],
                            tokens=rep["cost"]["tokens"],
                            extra={"stop_reason": rep["stop_reason"], "files_by_status": rep["result"]["files_by_status"],
                                   "retries_per_unit": rep["result"]["retries_per_unit"],
                                   "escalation_rate": rep["result"]["escalation_rate"],
                                   "degraded_files": rep["result"]["degraded_files"],
                                   "plan_limit_wait_s": round(ctx.llm.paused_s, 1)})
    return run


def systems_for(pack) -> dict:
    return {"swarm": swarm_system(8), "swarm-c1": swarm_system(1), **pack.BASELINES}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else (1.0 if v is True else 0.0 if v is False else None)


def aggregate(rows: list[dict], keys: list[str]) -> list[dict]:
    groups: dict = {}
    for r in rows:
        groups.setdefault((r["system"], r["repo"]), []).append(r)
    out = []
    for (system, repo), rs in sorted(groups.items()):
        agg = {"system": system, "repo": repo, "runs": len(rs), "errors": sum(1 for r in rs if r.get("error"))}
        for k in keys:
            vals = [_num(r.get(k)) for r in rs if _num(r.get(k)) is not None]
            if vals:
                agg[k] = round(statistics.mean(vals), 4)
                agg[k + "_sd"] = round(statistics.stdev(vals), 4) if len(vals) > 1 else 0.0
                agg[k + "_min"], agg[k + "_max"] = min(vals), max(vals)
        out.append(agg)
    return out


def _fmt(a: dict, k: str, pct=False, money=False) -> str:
    if k not in a:
        return "-"
    m, sd = a[k], a.get(k + "_sd", 0)
    f = (lambda x: f"{x * 100:.0f}%") if pct else (lambda x: f"${x:.2f}") if money else (lambda x: f"{x:.3g}")
    return f(m) + (f" ± {f(sd)}" if a["runs"] > 1 and sd else "")


def render_md(migration: str, aggs: list[dict], pack, meta: dict) -> str:
    cols = pack.EVAL_TABLE
    lines = [f"# Eval: {migration} ({meta['date']})", "",
             f"Systems: {', '.join(meta['systems'])}  ·  repos: {', '.join(meta['repos'])}  ·  runs per cell: {meta['runs']}"
             f"  ·  budget per run: ${meta['budget']}", "",
             "Values are mean ± sample standard deviation over runs. Every run is in `runs.csv`.", "",
             "| system | repo | runs | " + " | ".join(c[0] for c in cols) + " |",
             "|---|---|---:|" + "---:|" * len(cols)]
    for a in aggs:
        lines.append(f"| {a['system']} | {a['repo']} | {a['runs']}{' (' + str(a['errors']) + ' errored)' if a['errors'] else ''} | "
                     + " | ".join(_fmt(a, k, pct=fmt == 'pct', money=fmt == 'usd') for _, k, fmt in cols) + " |")
    speed = []
    by = {(a["system"], a["repo"]): a for a in aggs}
    for repo in meta["repos"]:
        s8, s1 = by.get(("swarm", repo)), by.get(("swarm-c1", repo))
        if s8 and s1 and s8.get("wall_s") and s1.get("wall_s"):
            speed.append(f"| {repo} | {s1['wall_s']:.0f}s | {s8['wall_s']:.0f}s | {s1['wall_s'] / s8['wall_s']:.2f}x |")
    if speed:
        lines += ["", "## Parallel speedup (swarm, concurrency 1 vs 8)", "", "| repo | c=1 | c=8 | speedup |",
                  "|---|---:|---:|---:|", *speed]
    lines += ["", "## Metric definitions", "", "```", pack.EVAL_DEFINITIONS, "```", ""]
    return "\n".join(lines)


def run_eval(migration: str, manifest: Path, systems: list[str], *, runs_per_cell: int = 3, repos: list[str] | None = None,
             budget: float | None = None, out_dir: Path | None = None, config: Path | None = None,
             backend: str | None = None, provider: str | None = None) -> Path:
    from migration_swarm import migrations as migrations_mod

    pack = migrations_mod.load(migration)
    cfg = config_mod.load(config, budget_usd=budget, llm_backend=backend, provider=provider)
    entries = [e for e in yaml.safe_load(manifest.read_text())["repos"] if not repos or e["name"] in repos]
    all_systems = systems_for(pack)
    if systems == ["all"]:
        systems = list(all_systems)
    unknown = [s for s in systems if s not in all_systems]
    if unknown:
        raise ValueError(f"unknown systems {unknown}; available: {list(all_systems)}")
    date = dt.date.today().isoformat()
    out_dir = out_dir or ROOT / "evals" / migration / date
    work = out_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    rows_path = work / "rows.jsonl"
    rows = [json.loads(l) for l in rows_path.read_text().splitlines()] if rows_path.exists() else []
    done = {(r["system"], r["repo"], r["run"]) for r in rows}
    meta = {"date": date, "systems": systems, "repos": [e["name"] for e in entries], "runs": runs_per_cell,
            "budget": cfg["budget_usd"], "manifest": str(manifest),
            "llm_backend": cfg.get("llm_backend", "api")}

    for e in entries:
        clone_src = ROOT / ".bench_cache" / "clones" / e["name"]
        if not clone_src.exists():
            clone_src.parent.mkdir(parents=True, exist_ok=True)
            import subprocess
            subprocess.run(["git", "clone", "-q", "--no-checkout", e["url"], str(clone_src)], check=True)
        prep = pack.eval_prepare(e, clone_src, work / "prep" / e["name"])
        for i in range(1, runs_per_cell + 1):
            for s in systems:
                if (s, e["name"], i) in done:
                    continue
                run_dir = work / "runs" / e["name"] / s / str(i)
                print(f"[eval] {e['name']} / {s} / run {i}", flush=True)
                row = {"system": s, "repo": e["name"], "run": i, "loc": prep["total_loc"]}
                try:
                    if run_dir.exists():
                        import shutil
                        shutil.rmtree(run_dir)
                    run_dir.mkdir(parents=True)
                    fn = all_systems[s]
                    out = fn(e, prep, clone_src, run_dir, cfg, pack=pack) if s.startswith("swarm") else fn(e, prep, clone_src, run_dir, cfg)
                    m = pack.eval_score(e, prep, out.final_tree, run_dir / "eval")
                    row.update({"wall_s": round(out.wall_s, 1), "cost_usd": out.cost_usd,
                                "usd_per_kloc": round(out.cost_usd / (prep["total_loc"] / 1000), 4) if out.cost_usd is not None else None,
                                "tokens_in": (out.tokens or {}).get("input"), "tokens_out": (out.tokens or {}).get("output"),
                                "error": out.error, **{f"x_{k}": v for k, v in out.extra.items()}})
                    row.update(pack.eval_flatten(m, prep))
                except Exception as ex:  # a crashed system run is recorded, never silently dropped
                    row.update({"error": f"{type(ex).__name__}: {ex}"[:500], "success": False})
                    (run_dir / "harness_error.txt").write_text(traceback.format_exc())
                rows.append(row)
                with rows_path.open("a") as f:
                    f.write(json.dumps(row) + "\n")
                print(f"[eval]   -> success={row.get('success')} cost={row.get('cost_usd')} wall={row.get('wall_s')} error={row.get('error')}", flush=True)

    keys = [k for _, k, _ in pack.EVAL_TABLE] + ["tokens_in", "tokens_out"]
    aggs = aggregate([r for r in rows if r["system"] in systems and r["repo"] in meta["repos"]], keys)
    fields = sorted({k for r in rows for k in r})
    with (out_dir / "runs.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    afields = sorted({k for a in aggs for k in a}, key=lambda k: (k not in ("system", "repo", "runs", "errors"), k))
    with (out_dir / "results.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=afields)
        w.writeheader()
        w.writerows(aggs)
    (out_dir / "results.md").write_text(render_md(migration, aggs, pack, meta))
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=1))
    return out_dir
