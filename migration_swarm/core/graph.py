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
import collections
import json
import operator
import time
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from migration_swarm.core import planner, report as report_mod, runs
from migration_swarm.core.budget import BudgetExceeded, TokenCapExceeded
from migration_swarm.core import plansummary
from migration_swarm.core.plan_llm import PlanLimitReached
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
        ctx.emit("phase", phase="analyze", detail="installing dependencies and mapping imports")
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
        ctx.emit("phase", phase="plan", detail=f"{len(unit_files)} units in {len(layers)} layers")
        plansummary.write(ctx, plansummary.build(ctx, a, [[u.id for u in l] for l in layers], unit_files))
        return {"layers": [[u.id for u in l] for l in layers], "unit_files": unit_files, "units": units, "layer_idx": 0}

    def setup_node(state: RunState) -> dict:
        status(phase="setup")
        ctx.emit("phase", phase="setup", detail="tsconfig, types, baseline tests")
        try:
            info = pack.setup(ctx)
        except Exception as e:  # setup failure: stop before spending anything
            ctx.log(f"setup failed: {e}")
            ctx.emit("phase", phase="setup_failed", detail=str(e)[:300])
            return {"setup": {"error": str(e)}, "stop_reason": "setup_failed"}
        return {"setup": info}

    def dispatch_node(state: RunState) -> dict:
        k = state["layer_idx"]
        status(phase="migrate", layer=k, layer_count=len(state["layers"]))
        ctx.emit("layer", index=k, count=len(state["layers"]), units=len(state["layers"][k]))
        return {"candidates": RESET}

    # Each unit runs its own ladder (write -> judge -> retry) inside one worker task, so a unit never waits
    # for the slowest unit of its layer between attempts. Judging edits the shared clone, so it is serialized.
    judge_lock = asyncio.Lock()
    status_of: dict[str, str] = {}  # live unit statuses while a layer runs (shared by its worker tasks)

    def fan_out(state: RunState):
        if state.get("stop_reason"):
            return "finalize"
        k = state["layer_idx"]
        statuses = {uid: u["status"] for uid, u in state["units"].items()}
        # statuses travel in the payload: on resume, pending Sends come back from the checkpoint without fan_out
        sends = [Send("worker", {"uid": uid, "unit": state["units"][uid], "layer": k, "unit_files": state["unit_files"],
                                 "setup": state["setup"], "statuses": statuses})
                 for uid in state["layers"][k] if state["units"][uid]["status"] == "pending"]
        return sends or "judge"

    async def attempt(uid: str, files: list[str], setup: dict, n: int, model_role: str, degraded: bool,
                      retry: dict | None) -> dict:
        """One LLM attempt for a unit -> a candidate (or an error marker)."""
        if cancelled():
            return {"error": "cancelled"}
        role = "worker" if model_role == "worker" else "escalation"
        system, user = await asyncio.to_thread(pack.worker_prompt, ctx, setup, files, retry, degraded)
        ctx.emit("agent_start", agent=uid, role=role, unit=uid, files=files, model=ctx.llm.model_for(model_role),
                 attempt=n, degraded=degraded)
        try:
            size = sum((ctx.workdir / f).stat().st_size for f in files if (ctx.workdir / f).exists())
            r = await ctx.llm.complete(role, system, user, key=uid, model_role=model_role,
                                       expected_output_chars=size or None)
        except BudgetExceeded as e:
            ctx.log(f"budget: {e}")
            why = "plan_limit" if isinstance(e, PlanLimitReached) else "budget"
            ctx.emit("agent_done", agent=uid, unit=uid, error=why)
            return {"error": why}
        except TokenCapExceeded as e:
            ctx.emit("agent_done", agent=uid, unit=uid, error="token cap")
            return {"error": "token_cap", "detail": str(e)}
        except Exception as e:  # API failure after SDK retries: count as a failed attempt, keep going
            ctx.log(f"worker {uid}: LLM error {type(e).__name__}: {e}")
            ctx.emit("agent_done", agent=uid, unit=uid, error=f"{type(e).__name__}: {e}"[:200])
            return {"error": "llm_error", "detail": f"{type(e).__name__}: {e}"[:500], "attempt": n}
        runs.write_status(ctx)  # persist spend now, so a crash before judging still counts this call
        out_files, requests, problems = pack.parse_worker_output(r.text, files)
        if r.stop_reason == "max_tokens":
            problems.append("the reply was cut off at the output token limit")
        ctx.emit("agent_done", agent=uid, unit=uid, cost_usd=r.cost_usd, stop_reason=r.stop_reason,
                 tokens=r.usage.prompt_tokens + r.usage.output_tokens, problems=problems[:2])
        return {"files": out_files, "problems": problems, "requests": requests, "model": r.model, "cost": r.cost_usd,
                "attempt": n, "degraded": degraded}

    def settle(uid: str, u: dict, c: dict, unit_files: dict, k: int, accepted_units: list[str]) -> tuple[dict, list]:
        """Apply one candidate to its unit: record errors, or judge it. Returns (unit, shared-type requests)."""
        u = dict(u)
        requests: list = []
        if c.get("error") == "token_cap":
            u["status"] = "failed"
            u["history"] = [*u["history"], {"attempt": u["attempts"], "reasons": [c["detail"]]}]
        elif c.get("error") == "llm_error" or (c["problems"] and not c["files"]):
            u["attempts"] += 1
            u["history"] = [*u["history"], {"attempt": c["attempt"], "model": c.get("model"),
                                            "reasons": [c["detail"]] if c.get("error") else c["problems"],
                                            "output_files": None}]
        else:
            ctx.emit("judge_start", layer=k, units=[uid])
            v = pack.judge(ctx, setup_of["setup"], unit_files, {uid: c}, accepted_units, k)[uid]
            reasons = [*c["problems"], *v["reasons"]] if not v["accepted"] else v["reasons"]
            u["history"] = [*u["history"], {"attempt": c["attempt"], "model": c["model"], "degraded": c["degraded"],
                                            "reasons": reasons, "escapes": v["escapes"],
                                            "output_files": c["files"] if not v["accepted"] else None}]
            u["attempts"] += 1
            if v["accepted"]:
                u["status"] = "degraded" if c["degraded"] else "accepted"
                u["escapes"] = v["escapes"]
                u["model"] = c["model"]
                requests = [{**r, "from": uid} for r in c.get("requests", [])]
                ctx.emit("verdict", unit=uid, files=unit_files[uid], attempt=u["attempts"], status=u["status"],
                         reasons=reasons[:3])
                return u, requests
        if u["status"] == "pending" and ladder(u["attempts"], n_retries) is None:
            u["status"] = "failed"
            ctx.log(f"unit {uid}: failed after {u['attempts']} attempts; left as JavaScript")
        ctx.emit("verdict", unit=uid, files=unit_files[uid], attempt=u["attempts"],
                 status="failed" if u["status"] == "failed" else "rejected",
                 reasons=(u["history"][-1]["reasons"] if u["history"] else [])[:3])
        return u, requests

    setup_of: dict = {}

    async def worker_node(payload: dict) -> dict:
        uid, k, unit_files = payload["uid"], payload["layer"], payload["unit_files"]
        setup_of["setup"] = payload["setup"]
        for x, st in payload["statuses"].items():
            status_of.setdefault(x, st)
        u, requests = dict(payload["unit"]), []
        while u["status"] == "pending" and (step := ladder(u["attempts"], n_retries)) is not None:
            last = u["history"][-1] if u["history"] else None
            retry = {"reasons": last["reasons"], "previous": last.get("output_files")} if last else None
            c = await attempt(uid, unit_files[uid], payload["setup"], u["attempts"], step[0], step[1], retry)
            if c.get("error") in ("budget", "cancelled", "plan_limit"):  # not an attempt; the unit stays pending
                return {"candidates": {uid: {"unit": u, "requests": requests}}, "stop_reason": c["error"]}
            async with judge_lock:
                accepted = [x for x, st in status_of.items() if st in ("accepted", "degraded")]
                u, new = await asyncio.to_thread(settle, uid, u, c, unit_files, k, accepted)
                status_of[uid] = u["status"]
                counts = collections.Counter(status_of.values())
                status(phase="migrate", layer=k, units_done=len(status_of) - counts["pending"],
                       units_by_status={st: counts[st] for st in ("pending", "accepted", "degraded", "failed")})
            requests += new
        return {"candidates": {uid: {"unit": u, "requests": requests}}}

    def judge_node(state: RunState) -> dict:
        """Collect the layer's finished units (each was judged inside its own worker task)."""
        k = state["layer_idx"]
        units = {uid: dict(u) for uid, u in state["units"].items()}
        requests: list = []
        for uid, c in (state.get("candidates") or {}).items():
            units[uid] = c["unit"]
            requests += c["requests"]
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
            ctx.emit("agent_start", agent="__steward__", role="steward", unit="shared types", files=[],
                     model=ctx.llm.model_for("steward"), attempt=0, degraded=False)
            for attempt in range(2):
                system, user = pack.steward_prompt(ctx, state["setup"], reqs, reasons)
                try:
                    r = await ctx.llm.complete("steward", system, user, key="__steward__")
                except BudgetExceeded as e:
                    ctx.log(f"budget: {e}")
                    return {"stop_reason": "plan_limit" if isinstance(e, PlanLimitReached) else "budget",
                            "steward_requests": [RESET]}
                reasons = await asyncio.to_thread(pack.apply_steward, ctx, state["setup"], r.text)
                log.append({"layer": state["layer_idx"], "attempt": attempt, "requests": len(reqs), "rejected": reasons})
                if not reasons:
                    ctx.log(f"steward: merged {len(reqs)} shared-type requests")
                    break
            else:
                ctx.log(f"steward: gave up on {len(reqs)} requests: {reasons[:3]}")
            ctx.emit("agent_done", agent="__steward__", unit="shared types", error=None if not reasons else "rejected")
            ctx.emit("steward", requests=len(reqs), rejected=(reasons or [])[:3])
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
        ctx.emit("phase", phase="finalize", detail="full type-check and test run, commit to branch")
        return {"final": pack.finalize(ctx, state["setup"])}

    def report_node(state: RunState) -> dict:
        report_mod.write(ctx, pack, state)
        status(phase="done", stop_reason=state.get("stop_reason"))
        ctx.emit("phase", phase="done", detail=state.get("stop_reason") or "complete")
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


RESUMABLE_STOPS = ("cancelled", "budget", "plan_limit")


async def execute(ctx: RunContext, pack, *, resume: bool = False, approve=None) -> dict:
    """Run, or resume. Resume after a crash continues from the last checkpoint. Resume after a clean
    stop (cancel / budget) rewinds to the last checkpoint before the stop and continues from there."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(ctx.run_dir / "checkpoints.sqlite")) as saver:
        # approve: async callable(state values after planning) -> bool. The run pauses before setup
        # (nothing spent yet) and continues only if it returns True.
        app = build(ctx, pack).compile(checkpointer=saver, interrupt_before=["setup"] if approve else None)
        config = {"configurable": {"thread_id": ctx.run_id}, "max_concurrency": int(ctx.cfg["concurrency"]),
                  "recursion_limit": 100000}
        if not resume:
            out = await app.ainvoke({}, config=config)
            if approve is None:
                return out
            snap = await app.aget_state(config)
            if not await approve(snap.values):
                runs.write_status(ctx, phase="declined")
                ctx.emit("phase", phase="declined", detail="plan not approved; nothing was spent")
                return snap.values
            return await app.ainvoke(None, config=config)
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
