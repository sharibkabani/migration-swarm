"""Ground-truth comparison: exported-symbol types of a system's output vs the maintainers' own migration.

Precise definition (see tools/node/gtcompare.mjs for the checker logic):
  - Both sides are reduced to declarations (.d.ts) emitted by TypeScript 6.0.3: the human migration at
    `ground_truth_sha` (built with a declaration-only tsconfig over its source dirs, tests excluded) and the
    system's final tree (the evaluator's own emit; files still in JS get JS-inferred declarations).
  - Modules are paired by path (with the manifest's ground_truth_path_map, e.g. cheerio lib/ -> src/).
  - For each symbol the human module exports (for `export =` modules: the whole export and each property),
    the system's same-named export is compared inside ONE TypeScript program:
        equivalent    mutually assignable and the system's printed type contains no `any` (lenient: a
                      non-generic signature can be equivalent to a generic one)
        identical     equivalent and both printed types are the same string (strict)
        any_weakened  mutually assignable only thanks to `any` on the system side
        wider / narrower / incomparable / missing
  - gt_equivalent_share / gt_identical_share = count / all human-exported symbols in paired modules
    (missing counts against).
Caveat: the human migrations sometimes refactored APIs or bumped dependencies in the same change; such
symbols show up as missing or incomparable. The share is a similarity measure, not a correctness score.
"""

from __future__ import annotations

import collections
import json
import shutil
from pathlib import Path

from migration_swarm.core import sandbox
from migration_swarm.core.sandbox import Mount
from migration_swarm.migrations.js_to_ts import tools

DEFINITION = __doc__.split("Precise definition", 1)[1].strip()


def _install_cmd(tree: Path, date: str | None) -> str:
    if (tree / "yarn.lock").exists():
        return "yarn install --frozen-lockfile --ignore-scripts --ignore-engines"
    if (tree / "package-lock.json").exists():
        return "npm ci --ignore-scripts"
    return f"npm install --ignore-scripts{' --before=' + date if date else ''}"


def _map(path: str, path_map: dict) -> str:
    for a, b in path_map.items():
        if path.startswith(a):
            return b + path[len(a):]
    return path


def _dts(js: str) -> str:
    for a, b in ((".mjs", ".d.mts"), (".cjs", ".d.cts"), (".js", ".d.ts")):
        if js.endswith(a):
            return js[: -len(a)] + b
    return js


def prepare(entry: dict, prep_dir: Path, clone_src: Path, ctx_factory) -> dict | None:
    sha = entry.get("ground_truth_sha")
    if not sha:
        return None
    from migration_swarm.migrations.js_to_ts import evaluate as ev, setup_step
    gt_tree = prep_dir / "gt_tree"
    if not gt_tree.exists():
        ev.clone_at(clone_src, sha, gt_tree)
    date = __import__("subprocess").run(["git", "log", "-1", "--format=%cs", sha], cwd=gt_tree, capture_output=True,
                                        text=True).stdout.strip()
    if not (gt_tree / "node_modules").exists():
        r = sandbox.run(gt_tree, entry["image"], _install_cmd(gt_tree, date), network=True, timeout_s=2400)
        if not r.ok:
            return {"error": f"ground-truth install failed: {r.output[-800:]}"}
    path_map = entry.get("ground_truth_path_map") or {}
    gt_dirs = [_map(d.rstrip("/") + "/", path_map).rstrip("/") for d in entry["source_dirs"]]
    pkg = json.loads((gt_tree / "package.json").read_text())
    module = ("nodenext", "nodenext") if pkg.get("type") == "module" else ("preserve", "bundler")
    (gt_tree / "tsconfig.msw-gt.json").write_text(json.dumps({"compilerOptions": {
        "target": "ES2022", "module": module[0], "moduleResolution": module[1], "strict": True, "allowJs": True,
        "checkJs": False, "skipLibCheck": True, "esModuleInterop": True, "resolveJsonModule": True},
        "include": gt_dirs, "exclude": ["**/*.spec.*", "**/*.test.*", "**/__tests__/**", "**/__fixtures__/**", "**/test/**"]}))
    ctx = ctx_factory(prep_dir, gt_tree)
    out = prep_dir / "gt_emit"
    if out.exists():
        shutil.rmtree(out)
    setup_step.judge_request(ctx, "tsconfig.msw-gt.json", "gt_emit", emit=True)
    dts_root = gt_tree / ".msw-gt-dts"
    if dts_root.exists():
        shutil.rmtree(dts_root)
    shutil.copytree(out, dts_root, ignore=lambda d, names: [n for n in names if (Path(d) / n).is_file() and ".d." not in n])
    return {"sha": sha, "path_map": path_map, "gt_dirs": gt_dirs,
            "gt_modules": sorted(str(p.relative_to(dts_root)) for p in dts_root.rglob("*.d.*ts"))}


def compare(prep: dict, eval_dir: Path, tree: Path) -> dict:
    gt = prep.get("ground_truth")
    if not gt or gt.get("error"):
        return {"gt_available": False, **({"gt_error": gt["error"]} if gt and gt.get("error") else {})}
    gt_tree = Path(prep["prep_dir"]) / "gt_tree"
    ours_root = tree / ".msw-gt-ours"
    if ours_root.exists():
        shutil.rmtree(ours_root)
    shutil.copytree(eval_dir / "emit", ours_root,
                    ignore=lambda d, names: [n for n in names if (Path(d) / n).is_file() and ".d." not in n])
    pairs = []
    for f in prep["files"]:
        g = _dts(_map(f, gt["path_map"]))
        if (gt_tree / ".msw-gt-dts" / g).exists() and (ours_root / _dts(f)).exists():
            pairs.append({"module": f, "ours": f"/work/.msw-gt-ours/{_dts(f)}", "gt": f"/gt/.msw-gt-dts/{g}"})
    scratch = eval_dir / "gt"
    scratch.mkdir(exist_ok=True)
    (scratch / "req.json").write_text(json.dumps({"pairs": pairs, "result": "/scratch/result.json"}))
    r = tools.run_tool(tree, "node /opt/msw/scripts/gtcompare.mjs /scratch/req.json",
                       mounts=[Mount(gt_tree, "/gt"), Mount(scratch, "/scratch", read_only=False)], timeout_s=1800)
    if not r.ok or not (scratch / "result.json").exists():
        return {"gt_available": True, "gt_error": r.output[-800:]}
    results = json.loads((scratch / "result.json").read_text())["results"]
    (scratch / "symbols.json").write_text(json.dumps(results, indent=1))
    c = collections.Counter(x["verdict"] for x in results)
    total = len(results)
    return {"gt_available": True, "gt_modules_paired": len(pairs), "gt_symbols": total,
            **{f"gt_{k}": c.get(k, 0) for k in ("equivalent", "any_weakened", "wider", "narrower", "incomparable", "missing")},
            "gt_identical": sum(1 for x in results if x.get("identical")),
            "gt_equivalent_share": round(c.get("equivalent", 0) / total, 4) if total else None,
            "gt_identical_share": round(sum(1 for x in results if x.get("identical")) / total, 4) if total else None}
