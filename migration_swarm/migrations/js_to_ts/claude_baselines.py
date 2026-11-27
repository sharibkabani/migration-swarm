"""Claude Code baselines: `claude -p` in a locked-down container (core/agent_sandbox), same $ budget.

  claude-single     one agent, one prompt.
  claude-subagents  same prompt + the `parallel-ts-migration` skill and a Haiku `ts-file-migrator`
                    subagent (mirrors the swarm's cheap-worker / stronger-coordinator split).

Both get the same rules as the swarm's workers and the same self-check the evaluator applies
(claude/check.py runs the same judge script). Everything runs in Docker; the key used is
MSW_BASELINE_ANTHROPIC_API_KEY (a separate, spend-limited key), never the engine's key.
"""

from __future__ import annotations

import json
import os
import shutil
import time
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


def _run(entry: dict, prep: dict, clone_src: Path, run_dir: Path, cfg: dict, *, subagents: bool) -> SystemOutput:
    key = os.environ.get("MSW_BASELINE_ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("MSW_BASELINE_ANTHROPIC_API_KEY is not set (separate, spend-limited key for the Claude Code baselines)")
    tree = baselines.fresh_tree(entry, prep, clone_src, run_dir / "tree")
    bundle = run_dir / "msw-run"
    _check_bundle(entry, prep, cfg, bundle)
    b = cfg["escape_budgets"]
    task = TASK.format(n=len(prep["files"]), dirs=", ".join(entry["source_dirs"]),
                       tests=", ".join(entry.get("test_paths", [])) + " (and any test/spec/__tests__ files)",
                       as_cast=b["as_cast"], nn=b["non_null_assertion"], module_rules=MODULE_RULES[prep["module_style"]],
                       test=entry["test"], baseline=prep["baseline"]["passed"], budget=cfg["budget_usd"])
    model = cfg["models"]["escalation"].split(":", 1)[-1]
    flags = ["--bare", "--output-format", "json", "--model", model, "--max-budget-usd", str(cfg["budget_usd"]),
             "--permission-mode", "bypassPermissions"]
    mounts = [Mount(bundle, "/opt/msw-run")]
    if subagents:
        task += SUBAGENT_ADDENDUM
        agents = {"ts-file-migrator": {
            "description": "Converts the JavaScript files it is assigned to strict TypeScript following the migration rules; edits only those files.",
            "prompt": MIGRATOR_PROMPT, "model": cfg["models"]["worker"].split(":", 1)[-1]}}
        (bundle / "agents.json").write_text(json.dumps(agents, indent=1))
        mounts.append(Mount(HERE / "skill", "/opt/msw-skill"))
        flags += ["--agents", "/opt/msw-run/agents.json", "--add-dir", "/opt/msw-skill"]
    (bundle / "prompt.txt").write_text(task)
    cmd = "cd /work && claude -p \"$(cat /opt/msw-run/prompt.txt)\" " + " ".join(flags)
    start = time.monotonic()
    res = agent_sandbox.run_agent(tree, entry["image"], cmd, api_key=key, timeout_s=int(cfg.get("baseline_timeout_s", 7200)),
                                  mounts=mounts)
    wall = time.monotonic() - start
    (run_dir / "claude.log").write_text(res.output)
    result = None
    for line in reversed(res.output.splitlines()):
        if line.startswith("{") and '"type":"result"' in line.replace(" ", ""):
            result = json.loads(line)
            break
    if result is None:
        return SystemOutput(tree, wall, None, None, {"exit_code": res.exit_code}, error=f"no result JSON (exit {res.exit_code})")
    u = result.get("usage", {})
    extra = {"num_turns": result.get("num_turns"), "terminal_reason": result.get("terminal_reason"),
             "is_error": result.get("is_error"), "claude_reported_cost_usd": result.get("total_cost_usd"),
             "repriced_cost_usd": _repriced(result.get("modelUsage"), cfg["prices"]),
             "subagents_spawned": (result.get("subagent_stats") or {}).get("spawned"),
             "subagents_completed": (result.get("subagent_stats") or {}).get("completed")}
    (run_dir / "claude_result.json").write_text(json.dumps(result, indent=1))
    return SystemOutput(tree, wall, result.get("total_cost_usd"),
                        {"input": u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
                         "output": u.get("output_tokens", 0)}, extra,
                        error=f"claude error: {result.get('terminal_reason')}" if result.get("is_error") else None)


def run_single(entry, prep, clone_src, run_dir, cfg) -> SystemOutput:
    return _run(entry, prep, clone_src, run_dir, cfg, subagents=False)


def run_subagents(entry, prep, clone_src, run_dir, cfg) -> SystemOutput:
    return _run(entry, prep, clone_src, run_dir, cfg, subagents=True)
