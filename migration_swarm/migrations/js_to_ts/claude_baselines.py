"""Claude Code baselines: `claude -p` in a locked-down container (core/agent_sandbox), same $ budget.

  claude-single     one agent, one prompt.
  claude-subagents  same prompt + the `parallel-ts-migration` skill and a Haiku `ts-file-migrator`
                    subagent (mirrors the swarm's cheap-worker / stronger-coordinator split).

Both get the same rules as the swarm's workers and the same self-check the evaluator applies
(claude/check.py runs the same judge script). Everything runs in Docker.

Credentials (never the engine's key):
  llm_backend api   MSW_BASELINE_ANTHROPIC_API_KEY, a separate spend-limited key (`--bare` mode).
  llm_backend plan  CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`: runs on your Claude plan, not
                    billed. `--bare` is dropped because bare mode never reads OAuth credentials. When
                    the plan's usage limit stops the agent, we wait for the window to reset and resume
                    the same session (`--resume`), up to plan.max_wait_s; that wait is excluded from wall time.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

from migration_swarm.core import agent_sandbox
from migration_swarm.core.budget import Usage, price_usage
from migration_swarm.core.evals import SystemOutput
from migration_swarm.core.sandbox import Mount
from migration_swarm.migrations.js_to_ts import baselines, evaluate as ev, tools
from migration_swarm.migrations.js_to_ts.prompts import MODULE_RULES, ts_name

HERE = Path(__file__).parent / "claude"

TASK = """You are migrating this repository from JavaScript to strict TypeScript. Work autonomously until the job is done: nobody will answer questions, and nothing you write to the user is read.

## Scope
Migrate the {n} JavaScript source files listed in /opt/msw-run/files.txt (under {dirs}): rename each to .ts (.mjs -> .mts, .cjs -> .cts) and give it real, precise types. You may also add type-only files, edit tsconfig/package.json, and install @types packages with the repo's package manager (the npm registry is reachable). Leave everything else alone.

## Rules (a deterministic evaluator checks every one)
1. Test files and test configuration are read-only: {tests}. Do not modify, rename, move or delete them. Do not rename test files to .ts.
2. Runtime behaviour must not change. Only add types. Do not change logic, control flow, values, strings, error messages, export shapes or statement order; do not delete code; never rename identifiers or introduce new variables. The evaluator compiles each of your .ts files and compares the emitted JavaScript token by token with the original file's compiled output.
3. `strict: true` must report zero errors. Forbidden: `any` (explicit, or implicit from a missing annotation), `@ts-ignore`, `@ts-expect-error`, `@ts-nocheck`, `as unknown as`. At most {as_cast} `as` casts and {nn} non-null `!` assertions per file.
4. Module syntax:
{module_rules}

## How you are scored
The evaluator compiles your .ts files with its own strict tsconfig (TypeScript 6.0.3), then runs the unchanged test command `{test}` against the emitted JavaScript placed at the original file paths. The original code passes {baseline} tests; yours must too. Success also requires every listed file migrated with zero strict errors and no rule violations.

Run `python3 /opt/msw-run/check.py` at any time: it runs exactly these checks on /work and prints type errors, escape-budget violations, runtime changes and test results. Use it often, and before you stop make sure it reports no problems (or that you cannot fix the rest).

Your budget is about ${budget:.2f} of API usage; work efficiently.
"""

SUBAGENT_ADDENDUM = """
## How to work
Use the `parallel-ts-migration` skill: split the work across parallel `ts-file-migrator` subagents, layer by layer through the import graph, and verify each layer with check.py before the next.
"""

MIGRATOR_PROMPT = """You convert the JavaScript files you are assigned to strict TypeScript, and touch no other file.
Follow the task rules you are given exactly: only add types (no runtime changes, no renames, no new variables), zero strict errors, no `any`, no `@ts-ignore`/`@ts-expect-error`/`@ts-nocheck`, no `as unknown as`, few casts. Read the already-migrated .ts files you import to reuse their types. Rename each file (.js -> .ts), write the full typed file, then type-check with: /opt/msw-check/node /opt/msw-check/typescript/bin/tsc --noEmit -p /work/tsconfig.json (or the repo's tsconfig if that does not exist) and fix errors in your files. Report which files you finished and any problems you could not fix."""


def _check_bundle(entry: dict, prep: dict, cfg: dict, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(HERE / "check.py", dest / "check.py")
    shutil.copy(tools.NODE_DIR / "judge.mjs", dest / "judge.mjs")
    shutil.copytree(Path(prep["prep_dir"]) / "orig_norm", dest / "orig_norm")
    (dest / "files.txt").write_text("\n".join(prep["files"]) + "\n")
    esm_bundled = prep["module_style"] == "esm-bundled"
    from migration_swarm.migrations.js_to_ts import setup_step
    tsconfig = {"compilerOptions": {
        "target": setup_step._target(prep["node_major"]), "module": "preserve" if esm_bundled else "nodenext",
        "moduleResolution": "bundler" if esm_bundled else "nodenext", "strict": True, "allowJs": True, "checkJs": False,
        "esModuleInterop": True, "resolveJsonModule": True, "skipLibCheck": True, "useDefineForClassFields": True,
        "rootDir": "/work"}}
    (dest / "check.json").write_text(json.dumps({
        "files": prep["files"], "source_dirs": entry["source_dirs"], "test": entry["test"], "tsconfig": tsconfig,
        "escape_budgets": cfg["escape_budgets"], "baseline_passed": prep["baseline"]["passed"]}, indent=1))


def _repriced(model_usage: dict, prices: dict) -> float | None:
    """Price Claude Code's per-model token usage with the same table the swarm uses (apples to apples)."""
    total, ok = 0.0, False
    for model, u in (model_usage or {}).items():
        key = next((k for k in prices if model.startswith(k)), None)
        if key is None:
            return None
        usage = Usage(input_tokens=u.get("inputTokens", 0), output_tokens=u.get("outputTokens", 0),
                      cache_read=u.get("cacheReadInputTokens", 0), cache_write_5m=u.get("cacheCreationInputTokens", 0))
        total += price_usage(prices, key, usage)
        ok = True
    return round(total, 6) if ok else None


def _auth(cfg: dict) -> tuple[dict, list[str]]:
    if cfg.get("llm_backend") == "plan":
        tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if not tok:
            raise RuntimeError("llm_backend plan: set CLAUDE_CODE_OAUTH_TOKEN in .env (create it with `claude setup-token`)")
        return {"CLAUDE_CODE_OAUTH_TOKEN": tok}, []
    key = os.environ.get("MSW_BASELINE_ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("MSW_BASELINE_ANTHROPIC_API_KEY is not set (separate, spend-limited key for the Claude Code "
                           "baselines), or run with --backend plan")
    return {"ANTHROPIC_API_KEY": key}, ["--bare"]


LIMIT_WORDS = ("usage limit", "rate limit", "hit your limit", "limit reached")


def _parse_stream(output: str) -> tuple[dict | None, dict | None]:
    """Last result message and last rejected rate-limit info from `--output-format stream-json` output."""
    result = rejected = None
    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("type") == "result":
            result = m
        elif m.get("type") == "rate_limit_event" and (m.get("rate_limit_info") or {}).get("status") == "rejected":
            rejected = m["rate_limit_info"]
    return result, rejected


def _limited(result: dict, rejected: dict | None) -> bool:
    if not result.get("is_error"):
        return False
    text = (str(result.get("result") or "") + " ".join(result.get("errors") or [])).lower()
    return rejected is not None or result.get("api_error_status") == 429 or any(w in text for w in LIMIT_WORDS)


def _sum_usage(results: list[dict]) -> tuple[dict, dict]:
    usage: dict = {}
    model_usage: dict = {}
    for r in results:
        for k, v in (r.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                usage[k] = usage.get(k, 0) + v
        for m, mu in (r.get("modelUsage") or {}).items():
            acc = model_usage.setdefault(m, {})
            for k, v in mu.items():
                if isinstance(v, (int, float)):
                    acc[k] = acc.get(k, 0) + v
    return usage, model_usage


def _run(entry: dict, prep: dict, clone_src: Path, run_dir: Path, cfg: dict, *, subagents: bool) -> SystemOutput:
    auth_env, auth_flags = _auth(cfg)
    tree = baselines.fresh_tree(entry, prep, clone_src, run_dir / "tree")
    bundle = run_dir / "msw-run"
    _check_bundle(entry, prep, cfg, bundle)
    b = cfg["escape_budgets"]
    task = TASK.format(n=len(prep["files"]), dirs=", ".join(entry["source_dirs"]),
                       tests=", ".join(entry.get("test_paths", [])) + " (and any test/spec/__tests__ files)",
                       as_cast=b["as_cast"], nn=b["non_null_assertion"], module_rules=MODULE_RULES[prep["module_style"]],
                       test=entry["test"], baseline=prep["baseline"]["passed"], budget=cfg["budget_usd"])
    model = cfg["models"]["escalation"].split(":", 1)[-1]
    flags = [*auth_flags, "--output-format", "stream-json", "--verbose", "--model", model,
             "--permission-mode", "bypassPermissions"]
    # Claude Code's own state (sessions) lives in a per-run dir so a plan-limited run can be resumed.
    claude_home = run_dir / "claude-config"
    claude_home.mkdir(parents=True, exist_ok=True)
    mounts = [Mount(bundle, "/opt/msw-run"), Mount(claude_home, "/opt/msw-claude", read_only=False)]
    if subagents:
        task += SUBAGENT_ADDENDUM
        agents = {"ts-file-migrator": {
            "description": "Converts the JavaScript files it is assigned to strict TypeScript following the migration rules; edits only those files.",
            "prompt": MIGRATOR_PROMPT, "model": cfg["models"]["worker"].split(":", 1)[-1]}}
        (bundle / "agents.json").write_text(json.dumps(agents, indent=1))
        mounts.append(Mount(HERE / "skill", "/opt/msw-skill"))
        flags += ["--agents", "/opt/msw-run/agents.json", "--add-dir", "/opt/msw-skill"]
    (bundle / "prompt.txt").write_text(task)
    (bundle / "continue.txt").write_text("You were paused by a usage limit. Continue the task where you left off.")
    plan_cfg = cfg.get("plan") or {}
    max_wait, poll = float(plan_cfg.get("max_wait_s", 6 * 3600)), float(plan_cfg.get("poll_s", 900))
    session_id = str(uuid.uuid4())
    results: list[dict] = []
    waited = 0.0
    log = []
    start = time.monotonic()
    while True:
        spent = sum(r.get("total_cost_usd") or 0 for r in results)
        remaining = max(cfg["budget_usd"] - spent, 0.01)
        head = (f"claude -p \"$(cat /opt/msw-run/prompt.txt)\" --session-id {session_id}" if not results else
                f"claude -p \"$(cat /opt/msw-run/continue.txt)\" --resume {session_id}")
        cmd = f"cd /work && {head} {' '.join(flags)} --max-budget-usd {remaining:.2f}"
        res = agent_sandbox.run_agent(tree, entry["image"], cmd, auth_env=auth_env,
                                      timeout_s=int(cfg.get("baseline_timeout_s", 7200)), mounts=mounts,
                                      env={"CLAUDE_CONFIG_DIR": "/opt/msw-claude"})
        log.append(res.output)
        result, rejected = _parse_stream(res.output)
        if result is None:
            break
        results.append(result)
        if cfg.get("llm_backend") != "plan" or not _limited(result, rejected):
            break
        resets = (rejected or {}).get("resetsAt")
        wait = max((resets + 30 - time.time()) if resets else poll, 60)
        if waited + wait > max_wait:
            break
        time.sleep(wait)
        waited += wait
    wall = time.monotonic() - start - waited
    (run_dir / "claude.log").write_text("\n\n===== resumed =====\n\n".join(log))
    if not results:
        return SystemOutput(tree, wall, None, None, {"exit_code": res.exit_code}, error=f"no result JSON (exit {res.exit_code})")
    last = results[-1]
    usage, model_usage = _sum_usage(results)
    extra = {"num_turns": sum(r.get("num_turns") or 0 for r in results), "terminal_reason": last.get("terminal_reason"),
             "is_error": last.get("is_error"), "claude_reported_cost_usd": round(sum(r.get("total_cost_usd") or 0 for r in results), 6),
             "repriced_cost_usd": _repriced(model_usage, cfg["prices"]),
             "subagents_spawned": sum((r.get("subagent_stats") or {}).get("spawned") or 0 for r in results),
             "subagents_completed": sum((r.get("subagent_stats") or {}).get("completed") or 0 for r in results),
             "plan_limit_wait_s": round(waited, 1), "sessions": len(results)}
    (run_dir / "claude_result.json").write_text(json.dumps(results, indent=1))
    error = None
    if last.get("is_error"):
        error = ("plan usage limit (gave up waiting)" if _limited(last, None) else
                 f"claude error: {last.get('terminal_reason') or last.get('subtype')}")
    return SystemOutput(tree, wall, extra["claude_reported_cost_usd"],
                        {"input": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                         + usage.get("cache_creation_input_tokens", 0), "output": usage.get("output_tokens", 0)},
                        extra, error=error)


def run_single(entry, prep, clone_src, run_dir, cfg) -> SystemOutput:
    return _run(entry, prep, clone_src, run_dir, cfg, subagents=False)


def run_subagents(entry, prep, clone_src, run_dir, cfg) -> SystemOutput:
    return _run(entry, prep, clone_src, run_dir, cfg, subagents=True)
