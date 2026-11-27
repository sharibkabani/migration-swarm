"""js-to-ts pack: the functions the core calls. All JS/TS knowledge lives in this package."""

from __future__ import annotations

import json
from pathlib import Path

from migration_swarm.core.context import RepoSpec, RunContext
from migration_swarm.migrations.js_to_ts import analyzer, finalize as finalize_mod, judge as judge_mod, prompts
from migration_swarm.migrations.js_to_ts import setup as setup_mod

NAME = "js-to-ts"
DESCRIPTION = "JavaScript -> strict TypeScript (in place, file by file, tests unchanged)"
ESCAPE_KEYS = ["explicit_any", "implicit_any", "ts_ignore", "ts_expect_error", "ts_nocheck", "as_unknown_as",
               "non_null_assertion", "as_cast", "runtime_removed_lines", "runtime_added_lines"]


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
    runner = next((r for r in ("jest", "mocha", "jasmine") if r in dev), "other")
    dirs = source_dirs or [d for d in ("src", "lib") if (repo / d).is_dir()][:1] or ["."]
    return RepoSpec(image=f"node:{node}-bookworm", install=install, test="npm test", test_runner=runner,
                    source_dirs=dirs, test_paths=[])


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
        dependents=dependents, retry=retry, degraded=degraded)


def parse_worker_output(text: str, unit_files: list[str]):
    return prompts.parse_worker_output(text, unit_files)


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
