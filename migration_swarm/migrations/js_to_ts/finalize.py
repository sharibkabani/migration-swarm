"""finalize (js-to-ts): allowJs off (if nothing was left as JS), full strict tsc, full tests, commit."""

from __future__ import annotations

import json
from pathlib import Path

from migration_swarm.core import integrity
from migration_swarm.core.context import RunContext
from migration_swarm.migrations.js_to_ts import setup as setup_mod
from migration_swarm.migrations.js_to_ts.analyzer import Analysis
from migration_swarm.migrations.js_to_ts.prompts import ts_name


def finalize(ctx: RunContext, setup_info: dict, analysis: Analysis) -> dict:
    remaining_js = [f for f in analysis.files if (ctx.workdir / f).exists()]
    migrated = [f for f in analysis.files if (ctx.workdir / ts_name(f)).exists()]
    tsconfig = setup_info["tsconfig"]
    cfg_path = ctx.workdir / tsconfig
    cfg = json.loads(cfg_path.read_text())
    cfg["compilerOptions"]["allowJs"] = bool(remaining_js)
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")

    res = setup_mod.judge_request(ctx, tsconfig, "final_emit", emit=True,
                                  escape_files=[ts_name(f) for f in migrated])
    errors = [d for d in res["diagnostics"]]
    overlay_files: dict[str, Path] = {f: ctx.run_dir / "final_emit" / f for f in migrated}
    tests = setup_mod.run_tests(ctx, setup_mod.build_overlay(ctx, overlay_files))
    baseline = setup_mod.testresults.TestResult(**setup_info["baseline"])
    protected_changed = integrity.verify(ctx.workdir, setup_info["protected"])

    pkg_path = ctx.workdir / "package.json"
    pkg = json.loads(pkg_path.read_text())
    scripts = pkg.setdefault("scripts", {})
    if "build:ts" not in scripts:
        scripts["build:ts"] = f"tsc -p {tsconfig}"
        pkg_path.write_text(json.dumps(pkg, indent=2) + "\n")

    ctx.commit("migration-swarm: finalize",
               f"allowJs={cfg['compilerOptions']['allowJs']}; {len(migrated)} files migrated; "
                  f"{len(remaining_js)} left as JS; tsc errors={len(errors)}; tests passed={tests.passed} (baseline {baseline.passed})")
    return {
        "allow_js": cfg["compilerOptions"]["allowJs"],
        "migrated_files": len(migrated),
        "remaining_js": remaining_js,
        "tsc_errors": len(errors),
        "tsc_error_samples": errors[:20],
        "tests": tests.to_json(),
        "tests_match_baseline": tests.no_worse_than(baseline),
        "protected_changed": protected_changed,
        "escapes_final": {f: v.get("counts", {}) for f, v in res["escapes"].items()},
        "branch": ctx.git("rev-parse", "--abbrev-ref", "HEAD"),
        "commit": ctx.git("rev-parse", "HEAD"),
    }
