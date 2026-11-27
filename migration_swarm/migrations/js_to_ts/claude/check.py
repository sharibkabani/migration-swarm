#!/usr/bin/env python3
"""Self-check for agents doing the js-to-ts benchmark (runs inside the agent container, stdlib only).

Runs exactly what the benchmark evaluator scores, on /work:
  1. strict tsc (TypeScript 6.0.3, the evaluator's own tsconfig) with per-file errors,
  2. escape counts from the AST (same judge script as the evaluator) vs the per-file budgets,
  3. runtime drift: compiled JS of each .ts vs the original's compiled JS, token by token,
  4. the unchanged test command against the emitted JS at the original paths.
Usage: python3 /opt/msw-run/check.py
"""

import difflib
import json
import os
import shutil
import subprocess
import sys

RUN = "/opt/msw-run"
OUT = "/tmp/msw-check"
cfg = json.load(open(f"{RUN}/check.json"))
NODE = "/opt/msw-check/node"


def ts_name(p):
    for a, b in ((".mjs", ".mts"), (".cjs", ".cts"), (".js", ".ts")):
        if p.endswith(a):
            return p[: -len(a)] + b
    return p


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    has_node_types = os.path.isdir("/work/node_modules/@types/node")
    tsconfig = dict(cfg["tsconfig"])
    tsconfig["compilerOptions"] = {**tsconfig["compilerOptions"], "types": ["node"] if has_node_types else [],
                                   "typeRoots": ["/work/node_modules/@types"]}
    tsconfig["include"] = [f"/work/{d}" for d in cfg["source_dirs"]]
    json.dump(tsconfig, open("/work/tsconfig.msw-check.json", "w"), indent=1)
    files = cfg["files"]
    migrated = [f for f in files if os.path.exists(f"/work/{ts_name(f)}") and not os.path.exists(f"/work/{f}")]
    left = [f for f in files if os.path.exists(f"/work/{f}")]
    req = {"project": "/work/tsconfig.msw-check.json", "outDir": f"{OUT}/emit", "buildInfo": f"{OUT}/tsbuildinfo",
           "emit": True, "escapeFiles": [ts_name(f) for f in migrated], "normalize": migrated,
           "normDir": f"{OUT}/norm", "result": f"{OUT}/result.json"}
    json.dump(req, open(f"{OUT}/req.json", "w"))
    r = subprocess.run([NODE, f"{RUN}/judge.mjs", f"{OUT}/req.json"], capture_output=True, text=True,
                       env={**os.environ, "NODE_PATH": "/opt/msw-check"})
    os.remove("/work/tsconfig.msw-check.json")
    if r.returncode != 0 or not os.path.exists(f"{OUT}/result.json"):
        print("judge failed:", r.stdout[-2000:], r.stderr[-2000:])
        sys.exit(2)
    res = json.load(open(f"{OUT}/result.json"))
    diags = [d for d in res["diagnostics"] if d["file"] is None or not d["file"].endswith((".js", ".mjs", ".cjs"))]
    problems = 0
    print(f"== files: {len(migrated)}/{len(files)} migrated; still JavaScript: {', '.join(left[:20]) or 'none'}")
    print(f"== strict tsc (TypeScript 6.0.3): {len(diags)} errors")
    for d in diags[:60]:
        print(f"  {d['file']}:{d['line']}:{d['col']} TS{d['code']}: {d['message'][:300]}")
    problems += len(diags) + len(left)
    budgets = cfg["escape_budgets"]
    print("== escapes over budget / runtime changes:")
    for f in migrated:
        t = ts_name(f)
        counts = res["escapes"].get(t, {}).get("counts", {})
        sites = res["escapes"].get(t, {}).get("sites", [])
        for k, n in counts.items():
            if n > budgets.get(k, 0):
                problems += 1
                lines = ",".join([str(s["line"]) for s in sites if s["kind"] == k][:12])
                print(f"  {t}: {k} = {n} (allowed {budgets.get(k, 0)}) at lines {lines}")
        o, c = f"{RUN}/orig_norm/{f}", f"{OUT}/norm/{f}"
        if os.path.exists(o) and os.path.exists(c):
            a = open(o).read().split("\n")
            b = open(c).read().split("\n")
            removed = added = 0
            ex = []
            for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
                if op != "equal":
                    removed += i2 - i1
                    added += j2 - j1
                    if len(ex) < 4:
                        ex.append(f"original [{' '.join(a[i1:i2])[:120]}] -> yours [{' '.join(b[j1:j2])[:120]}]")
            if removed > budgets.get("runtime_removed_tokens", 0) or added > budgets.get("runtime_added_tokens", 40):
                problems += 1
                print(f"  {t}: runtime code changed ({removed} original tokens removed/altered, {added} added): " + " | ".join(ex))
    # Tests on the emitted JS at the original paths, in a throwaway copy.
    shadow = f"{OUT}/shadow"
    subprocess.run(["cp", "-a", "/work", shadow], check=True)
    for f in migrated:
        src = f"{OUT}/emit/{f}"
        if os.path.exists(src):
            shutil.copyfile(src, f"{shadow}/{f}")
    for d in cfg["source_dirs"]:
        subprocess.run(f"find {shadow}/{d} \\( -name '*.ts' -o -name '*.mts' -o -name '*.cts' \\) ! -name '*.d.ts' "
                       f"! -path '*/node_modules/*' -delete", shell=True)
    t = subprocess.run(cfg["test"], shell=True, cwd=shadow, capture_output=True, text=True, timeout=3600)
    out = (t.stdout + t.stderr)
    print(f"== tests: `{cfg['test']}` exit {t.returncode} (original: exit 0 with {cfg['baseline_passed']} passing)")
    print("\n".join(out.strip().splitlines()[-25:]))
    if t.returncode != 0:
        problems += 1
    print(f"== {'OK: no problems found' if problems == 0 else f'{problems} problem(s) found'}")
    shutil.rmtree(shadow, ignore_errors=True)


if __name__ == "__main__":
    main()
