"""Uniform evaluation of ANY system's js-to-ts output (swarm, ts-migrate, Claude Code baselines).

Every system is scored the same way, independent of the tsconfig or tooling it chose:
  - compile the final tree with OUR strict eval tsconfig (allowJs on, so leftover JS still builds);
  - per migrated file: strict error count, AST escape counts, runtime drift vs the original's
    compiled JS (canonical tokens);
  - run the repo's unchanged test command on the emitted JS at the original paths (shadow build);
  - protected test files must be unchanged. A test file renamed to .ts may differ from the original
    only by types (its transpiled tokens must equal the original's).

Definitions (also written into each eval report):
  migrated_files      source files whose .js is gone and whose .ts exists
  strict_clean_files  migrated files with zero errors under strict (share is over ALL source files)
  tests_equal         passed >= baseline passed, failed <= baseline failed, no new failing names,
                      same exit outcome, no suite-load errors beyond baseline
  test_violations     protected test files modified, deleted, or renamed with runtime changes
  success             tests_equal AND no test_violations AND every source file migrated AND
                      zero strict errors in migrated files
"""

from __future__ import annotations

import collections
import json
import shutil
import subprocess
from pathlib import Path

from migration_swarm.core import config as config_mod, integrity
from migration_swarm.core.budget import CostMeter
from migration_swarm.core.context import RepoSpec, RunContext
from migration_swarm.migrations.js_to_ts import analyzer, setup_step as setup_mod
from migration_swarm.migrations.js_to_ts.judge import drift
from migration_swarm.migrations.js_to_ts.prompts import ts_name
from migration_swarm.migrations.js_to_ts.testresults import TestResult

EVAL_TSCONFIG = "tsconfig.msw-eval.json"
DEFINITIONS = __doc__.split("Definitions (also written into each eval report):", 1)[1].strip()


def spec_from_entry(entry: dict) -> RepoSpec:
    return RepoSpec(entry["image"], entry["install"], entry["test"], entry["test_runner"], entry["source_dirs"],
                    entry.get("test_paths", []))


def _ctx(run_dir: Path, workdir: Path, spec: RepoSpec, source: str = "") -> RunContext:
    cfg = config_mod.load()
    return RunContext("eval", "js-to-ts", run_dir, workdir, source, spec, cfg, CostMeter(0, cfg["prices"]))


def fast_copy(src: Path, dest: Path) -> None:
    """Copy a tree; APFS copy-on-write clone when available (instant), plain copy otherwise."""
    if subprocess.run(["cp", "-cR", str(src), str(dest)], capture_output=True).returncode != 0:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, symlinks=True)


def clone_at(src_repo: Path, sha: str, dest: Path) -> None:
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(src_repo), str(dest)], check=True)
    subprocess.run(["git", "checkout", "-q", "--detach", sha], cwd=dest, check=True)
    for k, v in (("user.name", "msw-eval"), ("user.email", "msw-eval@localhost")):
        subprocess.run(["git", "config", k, v], cwd=dest, check=True)


def _write_eval_tsconfig(tree: Path, spec: RepoSpec, style: str, node_major: int) -> None:
    module, resolution = ("preserve", "bundler") if style == "esm-bundled" else ("nodenext", "nodenext")
    has_node_types = (tree / "node_modules" / "@types" / "node").exists()
    cfg = {"compilerOptions": {
        "target": setup_mod._target(node_major), "module": module, "moduleResolution": resolution,
        "strict": True, "allowJs": True, "checkJs": False, "esModuleInterop": True, "resolveJsonModule": True,
        "skipLibCheck": True, "useDefineForClassFields": True, "types": ["node"] if has_node_types else [],
        "outDir": "msw-eval-dist"}, "include": list(spec.source_dirs)}
    (tree / EVAL_TSCONFIG).write_text(json.dumps(cfg, indent=2) + "\n")


def _judge_version() -> str:
    """Prepared token streams depend on the judge script; a change invalidates cached preps."""
    import hashlib
    from migration_swarm.migrations.js_to_ts import tools
    return hashlib.sha256((tools.NODE_DIR / "judge.mjs").read_bytes() + tools.image_tag().encode()).hexdigest()[:16]


def prepare(entry: dict, clone_src: Path, prep_dir: Path) -> dict:
    """Pristine baseline for one repo: file list, baseline tests, original compiled tokens, protected hashes."""
    if (prep_dir / "prep.json").exists():
        cached = json.loads((prep_dir / "prep.json").read_text())
        if cached.get("judge_version") == _judge_version():
            return cached
    prep_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_from_entry(entry)
    tree = prep_dir / "tree"
    if not tree.exists():
        clone_at(clone_src, entry["pre_migration_sha"], tree)
    for stale in ("orig_emit", "orig_norm"):
        if (prep_dir / stale).exists():
            shutil.rmtree(prep_dir / stale)
    ctx = _ctx(prep_dir, tree, spec, str(clone_src))
    a = analyzer.analyze(ctx)                           # also installs dependencies
    protected = integrity.snapshot(tree, spec.test_paths, regex=analyzer.DEFAULT_TEST_RE)
    baseline = setup_mod.run_tests(ctx, overlay=None)
    if not baseline.ok:
        raise RuntimeError(f"{entry['name']}: baseline tests fail; cannot evaluate")
    node_major = setup_mod._node_major(ctx)
    style = setup_mod._module_style(ctx, a)
    _write_eval_tsconfig(tree, spec, style, node_major)
    protected_tests = [p for p in protected if "#" not in p and p.endswith((".js", ".mjs", ".cjs"))]
    setup_mod.judge_request(ctx, EVAL_TSCONFIG, "orig_emit", emit=True, normalize=a.files, norm_dir="orig_norm",
                            transpile_tokens=protected_tests)
    (tree / EVAL_TSCONFIG).unlink()
    from migration_swarm.migrations.js_to_ts import ground_truth
    gt = ground_truth.prepare(entry, prep_dir, clone_src, lambda run_dir, work: _ctx(run_dir, work, spec))
    prep = {"name": entry["name"], "ground_truth": gt, "files": a.files, "loc": a.loc, "total_loc": a.total_loc,
            "baseline": baseline.to_json(), "node_major": node_major, "module_style": style,
            "protected": protected, "prep_dir": str(prep_dir), "judge_version": _judge_version()}
    (prep_dir / "prep.json").write_text(json.dumps(prep, indent=1))
    return prep


def evaluate(entry: dict, prep: dict, final_tree: Path, eval_dir: Path) -> dict:
    """Score a system's final tree. Works on a copy; the system's output is not modified."""
    spec = spec_from_entry(entry)
    prep_dir = Path(prep["prep_dir"])
    if eval_dir.exists():
        shutil.rmtree(eval_dir)
    eval_dir.mkdir(parents=True)
    tree = eval_dir / "tree"
    fast_copy(final_tree, tree)
    if not (tree / "node_modules").exists():
        fast_copy(prep_dir / "tree" / "node_modules", tree / "node_modules")
    ctx = _ctx(eval_dir, tree, spec)
    _write_eval_tsconfig(tree, spec, prep["module_style"], prep["node_major"])

    files = prep["files"]
    migrated = [f for f in files if (tree / ts_name(f)).exists() and not (tree / f).exists()]
    both = [f for f in files if (tree / ts_name(f)).exists() and (tree / f).exists()]
    protected_tests = [p for p in prep["protected"] if "#" not in p and p.endswith((".js", ".mjs", ".cjs"))]
    renamed_tests = [p for p in protected_tests if not (tree / p).exists() and (tree / ts_name(p)).exists()]
    res = setup_mod.judge_request(ctx, EVAL_TSCONFIG, "emit", emit=True, escape_files=[ts_name(f) for f in migrated],
                                  normalize=migrated, norm_dir="norm",
                                  transpile_tokens=[ts_name(p) for p in renamed_tests])
    errors_by_file = collections.Counter(d["file"] for d in res["diagnostics"] if d["file"])
    config_errors = [d for d in res["diagnostics"] if d["file"] is None]

    escapes: collections.Counter = collections.Counter()
    per_file = {}
    for f in migrated:
        e = res["escapes"].get(ts_name(f), {}).get("counts", {})
        o, c = prep_dir / "orig_norm" / f, eval_dir / "norm" / f
        removed, added = (drift(o.read_text(), c.read_text())[:2] if o.exists() and c.exists() else (0, 0))
        e = {**e, "runtime_removed_tokens": removed, "runtime_added_tokens": added}
        escapes.update(e)
        per_file[f] = {"strict_errors": errors_by_file.get(ts_name(f), 0), "escapes": e}
    strict_errors = sum(v["strict_errors"] for v in per_file.values())
    clean = [f for f, v in per_file.items() if v["strict_errors"] == 0]

    # Tests on the shadow build: emitted JS for every migrated file, at the original path.
    overlay = {f: eval_dir / "emit" / f for f in migrated if (eval_dir / "emit" / f).exists()}
    tests = setup_mod.run_tests(ctx, setup_mod.build_overlay(ctx, overlay))
    baseline = TestResult(**prep["baseline"])

    # Protected tests: unchanged, or renamed to .ts with identical runtime tokens.
    violations = []
    for key in integrity.verify(tree, prep["protected"]):
        if key in renamed_tests:
            o = prep_dir / "orig_norm" / (key + ".tok")
            c = eval_dir / "norm" / (ts_name(key) + ".tok")
            if o.exists() and c.exists() and drift(o.read_text(), c.read_text())[:2] == (0, 0):
                continue
            violations.append(f"{key}: renamed to .ts with runtime changes")
        elif not (tree / key.split("#")[0]).exists():
            violations.append(f"{key}: deleted")
        else:
            violations.append(f"{key}: modified")

    kloc = max(prep["total_loc"], 1) / 1000
    m = {
        "source_files": len(files),
        "migrated_files": len(migrated),
        "migrated_share": round(len(migrated) / max(len(files), 1), 4),
        "ts_and_js_both_present": len(both),
        "strict_errors": strict_errors + len(config_errors),
        "strict_clean_files": len(clean),
        "strict_clean_share": round(len(clean) / max(len(files), 1), 4),
        "tests_baseline_passed": baseline.passed,
        "tests_passed": tests.passed,
        "tests_failed": tests.failed,
        "tests_equal": tests.no_worse_than(baseline),
        "test_violations": violations,
        "escapes": dict(escapes),
        "escapes_per_kloc": {k: round(v / kloc, 3) for k, v in escapes.items()},
        "per_file": per_file,
        "config_errors": [d["message"] for d in config_errors][:5],
        "tests_tail": "" if tests.no_worse_than(baseline) else tests.tail[-1500:],
    }
    from migration_swarm.migrations.js_to_ts import ground_truth
    m.update(ground_truth.compare(prep, eval_dir, tree))
    m["success"] = bool(m["tests_equal"] and not violations and len(migrated) == len(files) and m["strict_errors"] == 0)
    (eval_dir / "metrics.json").write_text(json.dumps(m, indent=1))
    return m
