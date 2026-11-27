"""Baseline systems for the js-to-ts eval. Each returns a SystemOutput whose final tree the
uniform evaluator (evaluate.py) scores exactly like the swarm's.

  ts-migrate        Airbnb's codemod, no LLM.
  claude-single     Claude Code headless (`claude -p`), one prompt, same $ budget.   (claude_baselines.py)
  claude-subagents  Claude Code told to split the work across parallel subagents.  (claude_baselines.py)
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from migration_swarm.core import sandbox
from migration_swarm.core.evals import SystemOutput
from migration_swarm.migrations.js_to_ts import analyzer, setup_step
from migration_swarm.migrations.js_to_ts import evaluate as ev
from migration_swarm.migrations.js_to_ts.prompts import ts_name

TSM_DIR = Path(__file__).parent / "tsmigrate"


def _tsm_image() -> str:
    h = hashlib.sha256()
    for n in ("Dockerfile", "package.json", "package-lock.json"):
        h.update((TSM_DIR / n).read_bytes())
    tag = f"msw-ts-migrate:{h.hexdigest()[:12]}"
    if not sandbox.image_exists(tag):
        subprocess.run(["docker", "build", "-q", "-t", tag, str(TSM_DIR)], check=True, capture_output=True)
    return tag


def fresh_tree(entry: dict, prep: dict, clone_src: Path, dest: Path) -> Path:
    """Pristine checkout at the pinned SHA with the prepared node_modules."""
    ev.clone_at(clone_src, entry["pre_migration_sha"], dest)
    ev.fast_copy(Path(prep["prep_dir"]) / "tree" / "node_modules", dest / "node_modules")
    return dest


def run_ts_migrate(entry: dict, prep: dict, clone_src: Path, run_dir: Path, cfg: dict) -> SystemOutput:
    tree = fresh_tree(entry, prep, clone_src, run_dir / "tree")
    start = time.monotonic()
    # Same @types the swarm's setup installs (a competent ts-migrate user installs them first; without
    # @types/node its tsserver annotates every `exports.x =` as an unknown name).
    ctx = ev._ctx(run_dir, tree, ev.spec_from_entry(entry))
    analysis = analyzer.load(Path(prep["prep_dir"]) / "analysis.json")
    installed = setup_step.install_types(ctx, analysis, prep["node_major"])
    # A strict tsconfig so ts-migrate's codemods annotate exactly what the evaluator checks.
    # ts-migrate runs TypeScript 4.9, which predates module "preserve".
    esm_bundled = prep["module_style"] == "esm-bundled"
    (tree / "tsconfig.json").write_text(json.dumps({"compilerOptions": {
        "target": "ES2020" if prep["node_major"] <= 14 else "ES2021" if prep["node_major"] <= 16 else "ES2022",
        "module": "esnext" if esm_bundled else "nodenext", "moduleResolution": "node" if esm_bundled else "nodenext",
        "strict": True, "allowJs": True, "checkJs": False, "esModuleInterop": True, "resolveJsonModule": True,
        "skipLibCheck": True, "noEmit": True, "types": ["node"]}, "include": entry["source_dirs"]}, indent=2) + "\n")
    # Rename exactly the analyzed source files (ts-migrate's `rename` takes one glob and would also rename
    # test fixtures living under the source dir; for non-JSX files it is a plain extension change).
    for f in prep["files"]:
        (tree / f).rename(tree / ts_name(f))
    # migrate (codemods), then reignore: ts-migrate's own pass that drops stale @ts-expect-error
    # directives and re-adds the needed ones; without it its output does not compile even under its own TS.
    res = sandbox.run(tree, _tsm_image(), "cd /opt/tsm && npx ts-migrate migrate /work && npx ts-migrate reignore /work",
                      network=False, timeout_s=7200)
    (run_dir / "ts-migrate.log").write_text(res.output)
    wall = time.monotonic() - start
    # For transparency: errors under ts-migrate's own TypeScript (4.9) and tsconfig. The benchmark itself
    # scores every system with the same TypeScript 6.0.3 strict config.
    own = sandbox.run(tree, _tsm_image(), "cd /opt/tsm && ./node_modules/.bin/tsc -p /work/tsconfig.json --noEmit | grep -c 'error TS'",
                      network=False, workdir_read_only=True, timeout_s=1800)
    own_errors = int(own.output.strip().splitlines()[-1]) if own.output.strip().splitlines()[-1:] and own.output.strip().splitlines()[-1].isdigit() else None
    return SystemOutput(final_tree=tree, wall_s=wall, cost_usd=0.0, tokens=None,
                        extra={"exit_code": res.exit_code, "types_installed": " ".join(installed),
                               "own_ts49_errors": own_errors}, error=None if res.ok else f"ts-migrate exit {res.exit_code}")
