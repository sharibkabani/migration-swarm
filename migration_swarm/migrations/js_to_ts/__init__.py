"""js-to-ts pack: the functions the core calls. All JS/TS knowledge lives in this package."""

from __future__ import annotations

import json
from pathlib import Path

from migration_swarm.core.context import RepoSpec, RunContext
from migration_swarm.migrations.js_to_ts import analyzer, finalize as finalize_mod, judge as judge_mod, prompts
from migration_swarm.migrations.js_to_ts import setup_step as setup_mod

NAME = "js-to-ts"
DESCRIPTION = "JavaScript -> strict TypeScript (in place, file by file, tests unchanged)"
INTENT_HINT = ("Converts a Node/JS repo's JavaScript source files (CommonJS or ESM) to .ts with real types under "
               "`strict: true`, no `any`/@ts-ignore, runtime behaviour unchanged, test files untouched. Covers "
               "requests like 'convert to TypeScript', 'add types', 'migrate src/ to TS'. Needs package.json and a test suite.")
ESCAPE_KEYS = ["explicit_any", "implicit_any", "ts_ignore", "ts_expect_error", "ts_nocheck", "as_unknown_as",
               "non_null_assertion", "as_cast", "runtime_removed_tokens", "runtime_added_tokens"]


def detect_repo_spec(repo: Path, source_dirs: list[str] | None = None) -> RepoSpec:
    """Defaults when the CLI isn't told how to install/test the repo."""
    pkg = json.loads((repo / "package.json").read_text())
    node = "22"
    nvm = repo / ".nvmrc"
    if nvm.exists():
        node = nvm.read_text().strip().lstrip("v").split(".")[0] or node
    install = ("yarn install --frozen-lockfile" if (repo / "yarn.lock").exists()
               else "npm ci" if (repo / "package-lock.json").exists() else "npm install")
    dev = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    script = (pkg.get("scripts") or {}).get("test", "")
    runner = ("node-test" if "node --test" in script else
              next((r for r in ("jest", "mocha", "jasmine") if r in script), None) or
              next((r for r in ("jest", "mocha", "jasmine") if r in dev), "other"))
    dirs = source_dirs or [d for d in ("src", "lib") if (repo / d).is_dir()][:1] or ["."]
    # Protected (read-only, hashed) test files: common test dirs and test config, on top of the
    # test-file name pattern the setup step always protects.
    tests = [f"{d}/**" for d in ("test", "tests", "__tests__", "spec") if (repo / d).is_dir()]
    tests += [f for f in ("jest.config.js", "jest.config.cjs", "jest.config.mjs", ".mocharc.js", ".mocharc.json",
                          ".mocharc.yml", "jasmine.json") if (repo / f).exists()]
    if "jest" in pkg:
        tests.append("package.json#jest")
    return RepoSpec(image=f"node:{node}-bookworm", install=install, test="npm test", test_runner=runner,
                    source_dirs=dirs, test_paths=tests)


def analyze(ctx: RunContext) -> dict:
    a = analyzer.analyze(ctx)
    return {"files": a.files, "edges": a.edges, "loc": a.loc, "total_loc": a.total_loc, "cycles": a.cycles,
            "shared": a.shared}


def _analysis(ctx: RunContext) -> analyzer.Analysis:
    return analyzer.load(ctx.run_dir / "analysis.json")


def setup(ctx: RunContext) -> dict:
    return setup_mod.setup(ctx, _analysis(ctx))


def worker_prompt(ctx: RunContext, setup_info: dict, unit_files: list[str], retry: dict | None, degraded: bool) -> tuple[str, str]:
    a = _analysis(ctx)
    deps = sorted({d for f in unit_files for d in a.edges.get(f, []) if d not in unit_files})
    dependents = sorted({f for f, ds in a.edges.items() for d in ds if d in unit_files and f not in unit_files})
    shared = ctx.workdir / setup_info["shared_types_path"]
    return prompts.worker_messages(
        style=setup_info["module_style"], shared_path=setup_info["shared_types_path"],
        files={f: ctx.git("show", f"{setup_info['setup_commit']}:{f}") + "\n" for f in unit_files},
        deps_dts=judge_mod.deps_declarations(ctx, deps),
        shared_types=shared.read_text() if shared.exists() else None,
        dependents=dependents, retry=retry, degraded=degraded,
        npm=({p: setup_info["npm_exports"].get(p, {"unresolved": True})
              for f in unit_files for p in a.file_externals.get(f, [])} if "npm_exports" in setup_info else None))


def parse_worker_output(text: str, unit_files: list[str], previous: dict[str, str] | None = None):
    return prompts.parse_worker_output(text, unit_files, previous)


def judge(ctx: RunContext, setup_info: dict, unit_files: dict[str, list[str]], candidates: dict[str, dict],
          accepted_units: list[str], layer: int) -> dict[str, dict]:
    verdicts = judge_mod.judge_round(ctx, setup_info=setup_info, unit_files=unit_files, candidates=candidates,
                                     accepted_units=accepted_units, layer=layer)
    return {uid: v.to_json() for uid, v in verdicts.items()}


def steward_prompt(ctx: RunContext, setup_info: dict, requests: list[dict], retry_reasons: list[str] | None) -> tuple[str, str]:
    p = ctx.workdir / setup_info["shared_types_path"]
    return prompts.steward_messages(path=setup_info["shared_types_path"], current=p.read_text() if p.exists() else None,
                                    requests=requests, retry_reasons=retry_reasons)


def apply_steward(ctx: RunContext, setup_info: dict, text: str) -> list[str]:
    content = prompts.parse_steward_output(text, setup_info["shared_types_path"])
    if content is None:
        return ["no ```ts block in the reply"]
    return judge_mod.judge_steward(ctx, setup_info, content)


def finalize(ctx: RunContext, setup_info: dict) -> dict:
    return finalize_mod.finalize(ctx, setup_info, _analysis(ctx))


# ---- eval harness surface -----------------------------------------------------------------------
from migration_swarm.migrations.js_to_ts import evaluate  # noqa: E402

from migration_swarm.migrations.js_to_ts import ground_truth  # noqa: E402

EVAL_DEFINITIONS = evaluate.DEFINITIONS + "\n\nGround truth (gt_*), for repos with a human migration:\n" + ground_truth.DEFINITION
# (column label, metric key, format) for the comparison table; every metric is also in the CSVs.
EVAL_TABLE = [
    ("success", "success", "pct"),
    ("tests equal", "tests_equal", "pct"),
    ("migrated", "migrated_share", "pct"),
    ("strict-clean files", "strict_clean_share", "pct"),
    ("strict errors", "strict_errors", "num"),
    ("test violations", "test_violations", "num"),
    ("any /kLOC", "esc_explicit_any_per_kloc", "num"),
    ("ts-ignore etc /kLOC", "ts_directives_per_kloc", "num"),
    ("as casts /kLOC", "esc_as_cast_per_kloc", "num"),
    ("non-null /kLOC", "esc_non_null_assertion_per_kloc", "num"),
    ("runtime tokens changed", "runtime_removed_tokens", "num"),
    ("GT types equivalent", "gt_equivalent_share", "pct"),
    ("GT types identical", "gt_identical_share", "pct"),
    ("cost", "cost_usd", "usd"),
    ("$/kLOC", "usd_per_kloc", "usd"),
    ("wall s", "wall_s", "num"),
    ("retries/unit", "x_retries_per_unit", "num"),
    ("escalation rate", "x_escalation_rate", "num"),
    ("degraded files", "x_degraded_files", "num"),
]


def eval_prepare(entry: dict, clone_src: Path, prep_dir: Path) -> dict:
    return evaluate.prepare(entry, clone_src, prep_dir)


def eval_score(entry: dict, prep: dict, tree: Path, eval_dir: Path) -> dict:
    return evaluate.evaluate(entry, prep, tree, eval_dir)


def eval_flatten(m: dict, prep: dict) -> dict:
    kloc = max(prep["total_loc"], 1) / 1000
    e = m["escapes"]
    flat = {k: m[k] for k in ("success", "tests_equal", "migrated_share", "strict_clean_share", "strict_errors",
                              "migrated_files", "source_files", "tests_passed", "tests_baseline_passed")}
    flat["test_violations"] = len(m["test_violations"])
    flat["test_violation_list"] = "; ".join(m["test_violations"])[:500]
    for k in ESCAPE_KEYS:
        flat[f"esc_{k}"] = e.get(k, 0)
        flat[f"esc_{k}_per_kloc"] = round(e.get(k, 0) / kloc, 3)
    flat["ts_directives_per_kloc"] = round(sum(e.get(k, 0) for k in ("ts_ignore", "ts_expect_error", "ts_nocheck")) / kloc, 3)
    flat["runtime_removed_tokens"] = e.get("runtime_removed_tokens", 0)
    for k, v in m.items():
        if k.startswith("gt_"):
            flat[k] = v
    return flat


from migration_swarm.migrations.js_to_ts import baselines  # noqa: E402

from migration_swarm.migrations.js_to_ts import claude_baselines  # noqa: E402

BASELINES = {"ts-migrate": baselines.run_ts_migrate, "claude-single": claude_baselines.run_single,
             "claude-subagents": claude_baselines.run_subagents}
