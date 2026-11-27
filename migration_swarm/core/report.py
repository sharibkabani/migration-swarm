"""report.json + report.md for a run. Escape counts are a generic dict supplied by the pack."""

from __future__ import annotations

import collections
import datetime as dt
import json
import time

from migration_swarm.core.context import RunContext


def build(ctx: RunContext, pack, state: dict) -> dict:
    a = state.get("analysis") or {"files": [], "loc": {}, "total_loc": 0, "cycles": []}
    units = state.get("units") or {}
    unit_files = state.get("unit_files") or {}
    final = state.get("final") or {}
    loc = a.get("loc", {})
    kloc = max(a.get("total_loc", 0), 1) / 1000
    escalation_n = int(ctx.cfg["retries_before_escalation"]) + 1

    files = []
    status_count: collections.Counter = collections.Counter()
    escapes: collections.Counter = collections.Counter()
    escapes_degraded: collections.Counter = collections.Counter()
    retries = []
    escalated = 0
    for uid, u in sorted(units.items()):
        st = u["status"]
        if st == "pending":
            st = "stopped_mid_retry" if u.get("attempts") else "not_reached"
        status_count[st] += len(unit_files.get(uid, [uid]))
        if st == "accepted":
            escapes.update(u.get("escapes", {}))
        elif st == "degraded":
            escapes_degraded.update(u.get("escapes", {}))
        attempts = u.get("attempts", 0)
        retries.append(max(attempts - 1, 0))
        if attempts > escalation_n:
            escalated += 1
        last = u["history"][-1] if u.get("history") else {}
        for f in unit_files.get(uid, [uid]):
            files.append({
                "path": f, "unit": uid, "cyclic_unit": len(unit_files.get(uid, [])) > 1, "status": st,
                "attempts": attempts, "escalated": attempts > escalation_n, "degraded": st == "degraded",
                "loc": loc.get(f, 0), "model": u.get("model"), "escapes": u.get("escapes", {}),
                "last_reasons": [] if st in ("accepted", "degraded") else last.get("reasons", [])[:12],
            })

    cost = ctx.meter.snapshot()
    wall = time.time() - state["started_at"] if state.get("started_at") else None
    n_units = max(len(units), 1)
    return {
        "run_id": ctx.run_id,
        "migration": ctx.migration,
        "source_repo": ctx.source_repo,
        "workdir": str(ctx.workdir),
        "branch": final.get("branch"),
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "stop_reason": state.get("stop_reason"),
        "complete": not state.get("stop_reason"),
        "wall_clock_s": round(wall, 1) if wall else None,
        "repo": {"files": len(a.get("files", [])), "loc": a.get("total_loc", 0), "cycles": a.get("cycles", []),
                 "units": len(units), "layers": len(state.get("layers") or [])},
        "setup": {k: v for k, v in (state.get("setup") or {}).items() if k not in ("protected",)},
        "result": {
            "files_by_status": dict(status_count),
            "escalation_rate": round(escalated / n_units, 3),
            "retries_per_unit": round(sum(retries) / n_units, 3),
            "degraded_files": status_count.get("degraded", 0),
            "escapes": {k: escapes.get(k, 0) for k in pack.ESCAPE_KEYS},
            "escapes_per_kloc": {k: round(escapes.get(k, 0) / kloc, 3) for k in pack.ESCAPE_KEYS},
            "escapes_degraded": {k: escapes_degraded.get(k, 0) for k in pack.ESCAPE_KEYS},
        },
        "final": final,
        "cost": {**cost, "usd_per_kloc": round(cost["spent_usd"] / kloc, 4)},
        "steward": state.get("steward_log", []),
        "files": files,
    }


def render_md(r: dict) -> str:
    res, fin, cost = r["result"], r.get("final") or {}, r["cost"]
    lines = [
        f"# migration-swarm report: {r['run_id']}",
        "",
        f"- Migration: `{r['migration']}`  ·  source: `{r['source_repo']}`",
        f"- Outcome: **{'complete' if r['complete'] else 'stopped: ' + str(r['stop_reason'])}**"
        + (f"  ·  branch `{r['branch']}` in `{r['workdir']}`" if r.get("branch") else ""),
        f"- Repo: {r['repo']['files']} files, {r['repo']['loc']} LOC, {r['repo']['units']} units in {r['repo']['layers']} layers, "
        f"{len(r['repo']['cycles'])} import cycles",
        f"- Cost: **${cost['spent_usd']:.4f}** of ${cost['budget_usd']:.2f} budget  ·  {cost['calls']} LLM calls  ·  "
        f"${cost['usd_per_kloc']:.4f}/kLOC  ·  wall clock {r['wall_clock_s']}s",
        "",
        "## Files",
        "",
        "| status | files |", "|---|---:|",
        *[f"| {k} | {v} |" for k, v in sorted(res["files_by_status"].items())],
        "",
        f"Retries per unit: {res['retries_per_unit']}  ·  escalation rate: {res['escalation_rate']}  ·  degraded files: {res['degraded_files']}",
        "",
    ]
    if fin and "tsc_errors" in fin:
        t = fin["tests"]
        lines += ["## Final checks", "",
                  f"- strict `tsc`: **{fin['tsc_errors']} errors**  ·  allowJs: {fin['allow_js']}  ·  files left as JS: {len(fin['remaining_js'])}",
                  f"- tests on the migrated code: passed {t['passed']}, failed {t['failed']}, skipped {t['skipped']}  ·  "
                  f"matches baseline: **{fin['tests_match_baseline']}**",
                  f"- protected test files changed: {fin['protected_changed'] or 'none'}", ""]
    deg = res.get("escapes_degraded", {})
    lines += ["## Escapes", "", "Per kLOC is over the whole repo. Degraded files had some budgets lifted and are counted separately.", "",
              "| escape | accepted files | per kLOC | degraded files |", "|---|---:|---:|---:|",
              *[f"| {k} | {v} | {res['escapes_per_kloc'][k]} | {deg.get(k, 0)} |" for k, v in res["escapes"].items()], ""]
    problem = [f for f in r["files"] if f["status"] not in ("accepted",)]
    if problem:
        lines += ["## Files needing attention", ""]
        for f in problem:
            lines.append(f"- `{f['path']}`: **{f['status']}** after {f['attempts']} attempts")
            for reason in f["last_reasons"][:4]:
                lines.append(f"  - {reason[:300]}")
        lines.append("")
    return "\n".join(lines)


def write(ctx: RunContext, pack, state: dict) -> dict:
    r = build(ctx, pack, state)
    (ctx.run_dir / "report.json").write_text(json.dumps(r, indent=1))
    (ctx.run_dir / "report.md").write_text(render_md(r))
    return r
