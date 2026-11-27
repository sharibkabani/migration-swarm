"""The run graph (LangGraph). Core orchestration only; every migration-specific step is a pack call.

analyze -> plan -> setup -> [dispatch -> worker x N (parallel, Send) -> judge]* per layer -> steward
        -> ... next layer ... -> finalize -> report

Retry/escalation ladder per unit (attempt index a, N = retries_before_escalation):
  a <= N      worker model (initial try + N retries), with the exact rejection reasons
  a == N + 1  escalation model
  a == N + 2  escalation model in "degraded" mode (pack lifts some escape budgets)
  then        unit is marked failed and left untouched (still JS); never silently dropped.
"""

from __future__ import annotations

import asyncio
import json
import operator
import time
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from migration_swarm.core import planner, report as report_mod, runs
from migration_swarm.core.budget import BudgetExceeded, TokenCapExceeded
from migration_swarm.core.context import RunContext

RESET = {"__reset__": True}


def merge_dict(a: dict | None, b: dict | None) -> dict:
    if b == RESET:
        return {}
    return {**(a or {}), **(b or {})}


def merge_list(a: list | None, b: list | None) -> list:
    if b and b[0] == RESET:
        return []
    return [*(a or []), *(b or [])]



def first_set(a: Any, b: Any) -> Any:
    return a if a else b


class RunState(TypedDict, total=False):
    analysis: dict
    layers: list[list[str]]
    unit_files: dict[str, list[str]]
    units: dict[str, dict]
    layer_idx: int
    setup: dict
    candidates: Annotated[dict, merge_dict]
    steward_requests: Annotated[list, merge_list]
    steward_log: Annotated[list, operator.add]
    stop_reason: Annotated[str | None, first_set]
    final: dict
    started_at: float


def ladder(attempt: int, n: int) -> tuple[str, bool] | None:
    """(model_role, degraded) for an attempt index, or None when the unit has exhausted the ladder."""
    if attempt <= n:
        return "worker", False
    if attempt == n + 1:
        return "escalation", False
    if attempt == n + 2:
        return "escalation", True
    return None


def build(ctx: RunContext, pack) -> StateGraph:
    cfg = ctx.cfg
    n_retries = int(cfg["retries_before_escalation"])

    def status(**kw):
        runs.write_status(ctx, **kw)

    def cancelled() -> bool:
        return (ctx.run_dir / "CANCEL").exists()

    # ---- nodes ---------------------------------------------------------------------------
    def analyze_node(state: RunState) -> dict:
        status(phase="analyze")
        ctx.log("analyze")
        a = pack.analyze(ctx)
        ctx.log(f"analyze: {len(a['files'])} files, {a['total_loc']} LOC, {len(a['cycles'])} cycles")
        return {"analysis": a, "started_at": time.time()}

    def plan_node(state: RunState) -> dict:
        a = state["analysis"]
        layers = planner.plan_layers(a["files"], {k: set(v) for k, v in a["edges"].items()})
        unit_files = {u.id: list(u.files) for layer in layers for u in layer}
        units = {uid: {"status": "pending", "attempts": 0, "history": [], "layer": k}
                 for k, layer in enumerate(layers) for uid in (u.id for u in layer)}
        ctx.log(f"plan: {len(unit_files)} units in {len(layers)} layers: " + ", ".join(str(len(l)) for l in layers))
        (ctx.run_dir / "plan.json").write_text(json.dumps({"layers": [[u.id for u in l] for l in layers],
                                                           "unit_files": unit_files}, indent=1))
        status(phase="plan", layers=len(layers), units=len(unit_files))
        return {"layers": [[u.id for u in l] for l in layers], "unit_files": unit_files, "units": units, "layer_idx": 0}

    def setup_node(state: RunState) -> dict:
        status(phase="setup")
        try:
            info = pack.setup(ctx)
        except Exception as e:  # setup failure: stop before spending anything
            ctx.log(f"setup failed: {e}")
            return {"setup": {"error": str(e)}, "stop_reason": "setup_failed"}
        return {"setup": info}

    def dispatch_node(state: RunState) -> dict:
        k = state["layer_idx"]
        status(phase="migrate", layer=k, layer_count=len(state["layers"]))
        return {"candidates": RESET}

    def fan_out(state: RunState):
        if state.get("stop_reason"):
            return "finalize"
        k = state["layer_idx"]
        sends = []
        for uid in state["layers"][k]:
            u = state["units"][uid]
            if u["status"] != "pending":
                continue
            step = ladder(u["attempts"], n_retries)
            last = u["history"][-1] if u["history"] else None
            retry = {"reasons": last["reasons"], "previous": last.get("output_files")} if last else None
            sends.append(Send("worker", {"uid": uid, "files": state["unit_files"][uid], "attempt": u["attempts"],
                                         "model_role": step[0], "degraded": step[1], "retry": retry,
                                         "setup": state["setup"]}))
        return sends or "judge"

    async def worker_node(payload: dict) -> dict:
        uid, files = payload["uid"], payload["files"]
        if cancelled():
            return {"candidates": {uid: {"error": "cancelled"}}, "stop_reason": "cancelled"}
        role = "worker" if payload["model_role"] == "worker" else "escalation"
        system, user = await asyncio.to_thread(pack.worker_prompt, ctx, payload["setup"], files, payload["retry"],
                                               payload["degraded"])
        try:
            size = sum((ctx.workdir / f).stat().st_size for f in files if (ctx.workdir / f).exists())
            r = await ctx.llm.complete(role, system, user, key=uid, model_role=payload["model_role"],
                                       expected_output_chars=size or None)
        except BudgetExceeded as e:
            ctx.log(f"budget: {e}")
            return {"candidates": {uid: {"error": "budget"}}, "stop_reason": "budget"}
        except TokenCapExceeded as e:
            return {"candidates": {uid: {"error": "token_cap", "detail": str(e)}}}
        except Exception as e:  # API failure after SDK retries: count as a failed attempt, keep going
            ctx.log(f"worker {uid}: LLM error {type(e).__name__}: {e}")
            return {"candidates": {uid: {"error": "llm_error", "detail": f"{type(e).__name__}: {e}"[:500],
                                         "attempt": payload["attempt"]}}}
        out_files, requests, problems = pack.parse_worker_output(r.text, files)
        if r.stop_reason == "max_tokens":
            problems.append("the reply was cut off at the output token limit")
        runs.write_status(ctx)
        return {"candidates": {uid: {"files": out_files, "problems": problems, "requests": requests,
                                     "model": r.model, "cost": r.cost_usd, "attempt": payload["attempt"],
                                     "degraded": payload["degraded"]}}}

    def judge_node(state: RunState) -> dict:
        k = state["layer_idx"]
        units = {uid: dict(u) for uid, u in state["units"].items()}
        cands = state.get("candidates") or {}
        to_judge: dict[str, dict] = {}
        requests: list = []
        for uid, c in cands.items():
            u = units[uid]
            if c.get("error") in ("budget", "cancelled"):
                continue  # not an attempt; the unit stays pending (reported as not reached)
            if c.get("error") == "token_cap":
                u["status"] = "failed"
                u["history"] = [*u["history"], {"attempt": u["attempts"], "reasons": [c["detail"]]}]
                continue
            if c.get("error") == "llm_error":
                u["attempts"] += 1
                u["history"] = [*u["history"], {"attempt": c["attempt"], "reasons": [c["detail"]]}]
                continue
            if c["problems"] and not c["files"]:
                u["attempts"] += 1
                u["history"] = [*u["history"], {"attempt": c["attempt"], "model": c["model"], "reasons": c["problems"],
                                                "output_files": None}]
                continue
            to_judge[uid] = c
        accepted_units = [uid for uid, u in units.items() if u["status"] in ("accepted", "degraded")]
        verdicts = pack.judge(ctx, state["setup"], state["unit_files"], to_judge, accepted_units, k) if to_judge else {}
        for uid, v in verdicts.items():
            c, u = to_judge[uid], units[uid]
            reasons = [*c["problems"], *v["reasons"]] if not v["accepted"] else v["reasons"]
            u["history"] = [*u["history"], {"attempt": c["attempt"], "model": c["model"], "degraded": c["degraded"],
                                            "reasons": reasons, "escapes": v["escapes"],
                                            "output_files": c["files"] if not v["accepted"] else None}]
            u["attempts"] += 1
            if v["accepted"]:
                u["status"] = "degraded" if c["degraded"] else "accepted"
                u["escapes"] = v["escapes"]
                u["model"] = c["model"]
                requests += [{**r, "from": uid} for r in c.get("requests", [])]
        for uid in state["layers"][k]:
            u = units[uid]
            if u["status"] == "pending" and ladder(u["attempts"], n_retries) is None:
                u["status"] = "failed"
                ctx.log(f"unit {uid}: failed after {u['attempts']} attempts; left as JavaScript")
        done = sum(1 for u in units.values() if u["status"] != "pending")
        status(phase="migrate", layer=k, units_done=done,
               units_by_status={s: sum(1 for u in units.values() if u["status"] == s)
                                for s in ("pending", "accepted", "degraded", "failed")})
        return {"units": units, "steward_requests": requests}

    def after_judge(state: RunState) -> str:
        if state.get("stop_reason"):
            return "finalize"
        k = state["layer_idx"]
        if any(state["units"][uid]["status"] == "pending" for uid in state["layers"][k]):
            return "dispatch"
        return "steward"

    async def steward_node(state: RunState) -> dict:
        reqs = state.get("steward_requests") or []
        log: list = []
        if reqs and not state.get("stop_reason"):
            reasons = None
            for attempt in range(2):
                system, user = pack.steward_prompt(ctx, state["setup"], reqs, reasons)
                try:
                    r = await ctx.llm.complete("steward", system, user, key="__steward__")
                except BudgetExceeded as e:
                    ctx.log(f"budget: {e}")
                    return {"stop_reason": "budget", "steward_requests": [RESET]}
                reasons = await asyncio.to_thread(pack.apply_steward, ctx, state["setup"], r.text)
                log.append({"layer": state["layer_idx"], "attempt": attempt, "requests": len(reqs), "rejected": reasons})
                if not reasons:
                    ctx.log(f"steward: merged {len(reqs)} shared-type requests")
                    break
            else:
                ctx.log(f"steward: gave up on {len(reqs)} requests: {reasons[:3]}")
        return {"steward_requests": [RESET], "steward_log": log, "layer_idx": state["layer_idx"] + 1}

    def after_steward(state: RunState) -> str:
        if state.get("stop_reason") or state["layer_idx"] >= len(state["layers"]):
            return "finalize"
        return "dispatch"

    def finalize_node(state: RunState) -> dict:
        status(phase="finalize")
        if state.get("stop_reason") == "setup_failed":
            return {"final": {"skipped": "setup failed"}}
        ctx.log("finalize")
        return {"final": pack.finalize(ctx, state["setup"])}

    def report_node(state: RunState) -> dict:
        report_mod.write(ctx, pack, state)
        status(phase="done", stop_reason=state.get("stop_reason"))
        return {}

    g = StateGraph(RunState)
    g.add_node("analyze", analyze_node)
    g.add_node("plan", plan_node)
    g.add_node("setup", setup_node)
    g.add_node("dispatch", dispatch_node)
    g.add_node("worker", worker_node)
    g.add_node("judge", judge_node)
    g.add_node("steward", steward_node)
    g.add_node("finalize", finalize_node)
    g.add_node("report", report_node)
    g.add_edge(START, "analyze")
    g.add_edge("analyze", "plan")
    g.add_edge("plan", "setup")
    g.add_conditional_edges("setup", lambda s: "finalize" if s.get("stop_reason") else "dispatch", ["finalize", "dispatch"])
    g.add_conditional_edges("dispatch", fan_out, ["worker", "judge", "finalize"])
    g.add_edge("worker", "judge")
    g.add_conditional_edges("judge", after_judge, ["dispatch", "steward", "finalize"])
    g.add_conditional_edges("steward", after_steward, ["dispatch", "finalize"])
    g.add_edge("finalize", "report")
    g.add_edge("report", END)
    return g


RESUMABLE_STOPS = ("cancelled", "budget")


async def execute(ctx: RunContext, pack, *, resume: bool = False) -> dict:
    """Run, or resume. Resume after a crash continues from the last checkpoint. Resume after a clean
    stop (cancel / budget) rewinds to the last checkpoint before the stop and continues from there."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(ctx.run_dir / "checkpoints.sqlite")) as saver:
        app = build(ctx, pack).compile(checkpointer=saver)
        config = {"configurable": {"thread_id": ctx.run_id}, "max_concurrency": int(ctx.cfg["concurrency"]),
                  "recursion_limit": 100000}
        if not resume:
            return await app.ainvoke({}, config=config)
        snap = await app.aget_state(config)
        if snap.next:  # crashed / killed mid-run: just continue
            ctx.log(f"resume: continuing at {snap.next}")
            return await app.ainvoke(None, config=config)
        if snap.values.get("stop_reason") not in RESUMABLE_STOPS:
            ctx.log(f"resume: run already finished ({snap.values.get('stop_reason') or 'complete'}); nothing to do")
            return snap.values
        async for past in app.aget_state_history(config):
            if past.next and not set(past.next) & {"finalize", "report"} and not past.values.get("stop_reason"):
                ctx.log(f"resume: rewinding past '{snap.values['stop_reason']}' stop to {past.next} in layer {past.values.get('layer_idx')}")
                fork = {**config, "configurable": {**config["configurable"], **past.config["configurable"]}}
                return await app.ainvoke(None, config=fork)
        raise RuntimeError("no checkpoint to resume from")
