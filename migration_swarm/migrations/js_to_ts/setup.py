"""setup (js-to-ts): deps, baseline tests, @types, strict tsconfig, and the shadow-build gate.

The gate emits the *untouched* JS through tsc with the chosen tsconfig, puts the emitted JS at the
original paths, and runs the repo's unchanged test command. It must match the baseline before any
LLM call is made, which proves the build/test pipeline preserves behaviour on this repo.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from pathlib import Path

from migration_swarm.core import integrity, sandbox
from migration_swarm.core.context import RunContext
from migration_swarm.migrations.js_to_ts import testresults, tools
from migration_swarm.migrations.js_to_ts.analyzer import DEFAULT_TEST_RE, Analysis, ensure_installed

TS_VERSION = "6.0.3"
TSCONFIG_DEFAULT = "tsconfig.json"
TSCONFIG_FALLBACK = "tsconfig.migration.json"
SHARED_TYPES_NAME = "__msw_shared_types__.ts"


class SetupFailed(Exception):
    pass


def ts_path(js: str) -> str:
    return re.sub(r"\.(c|m)?js$", lambda m: f".{m.group(1) or ''}ts", js)


def shared_types_path(ctx: RunContext) -> str:
    return f"{ctx.repo.source_dirs[0].rstrip('/')}/{SHARED_TYPES_NAME}"


def run_tests(ctx: RunContext, overlay: Path | None) -> testresults.TestResult:
    # Inside the throwaway copy, drop .ts sources so runners only ever see the JS at original paths.
    dirs = " ".join(shlex.quote(d) for d in ctx.repo.source_dirs)
    strip = (f"find {dirs} \\( -name '*.ts' -o -name '*.mts' -o -name '*.cts' \\) ! -name '*.d.ts' "
             f"! -path '*/node_modules/*' -delete 2>/dev/null; ")
    res = sandbox.run(ctx.workdir, ctx.repo.image, strip + ctx.repo.test, network=False, ephemeral=True,
                      overlay=overlay, timeout_s=ctx.cfg["judge"]["test_timeout_s"])
    return testresults.parse(ctx.repo.test_runner, res.output, res.ok, res.duration_s)


def _node_major(ctx: RunContext) -> int:
    r = sandbox.run(ctx.workdir, ctx.repo.image, "node -p process.versions.node", workdir_read_only=True)
    return int(r.output.strip().splitlines()[-1].split(".")[0])


def _target(node_major: int) -> str:
    return "ES2020" if node_major <= 14 else "ES2021" if node_major <= 16 else "ES2022"


def _module_style(ctx: RunContext, analysis: Analysis) -> str:
    pkg = json.loads((ctx.workdir / "package.json").read_text())
    if pkg.get("type") == "module":
        return "esm-node"
    if any(v == "es6" for v in analysis.module_system.values()):
        return "esm-bundled"     # ESM syntax compiled by babel/bundler (no "type": "module")
    return "cjs"


def _has_bundled_types(workdir: Path, pkg: str) -> bool:
    d = workdir / "node_modules" / pkg
    pj = d / "package.json"
    if not pj.exists():
        return False
    j = json.loads(pj.read_text())
    if j.get("types") or j.get("typings") or (d / "index.d.ts").exists():
        return True
    exports = json.dumps(j.get("exports", {}))
    return '"types"' in exports


def _types_name(pkg: str) -> str:
    return "@types/" + (pkg[1:].replace("/", "__") if pkg.startswith("@") else pkg)


def _install_dev(ctx: RunContext, specs: list[str]) -> None:
    if not specs:
        return
    q = " ".join(shlex.quote(s) for s in specs)
    if (ctx.workdir / "yarn.lock").exists():
        cmd = f"yarn add -D --ignore-scripts --ignore-engines {q}"
    else:
        cmd = f"npm install -D --ignore-scripts --no-audit --no-fund {q}"
    r = sandbox.run(ctx.workdir, ctx.repo.image, cmd, network=True, timeout_s=1800)
    if not r.ok:
        raise SetupFailed(f"installing {specs} failed:\n{r.output[-3000:]}")


def _installed_major(workdir: Path, pkg: str) -> str | None:
    pj = workdir / "node_modules" / pkg / "package.json"
    if not pj.exists():
        return None
    v = json.loads(pj.read_text()).get("version", "")
    return v.split(".")[0] if v[:1].isdigit() else None


def install_types(ctx: RunContext, analysis: Analysis, node_major: int) -> list[str]:
    """@types for npm deps without bundled types, pinned to the installed package's major version when
    such a @types release exists (e.g. parse5 v6 needs @types/parse5@6; @types/parse5@latest is a stub)."""
    wanted = [p for p in analysis.externals if not _has_bundled_types(ctx.workdir, p) and not p.startswith("@types/")]
    found: list[str] = []
    if wanted:
        lines = []
        for p in wanted:
            t, major = _types_name(p), _installed_major(ctx.workdir, p)
            pinned = f"{t}@{major}" if major else t
            lines.append(f"echo {shlex.quote(pinned)}=$(npm view {shlex.quote(pinned)} version 2>/dev/null | tail -1)")
            if major:
                lines.append(f"echo {shlex.quote(t)}=$(npm view {shlex.quote(t)} version 2>/dev/null)")
        r = sandbox.run(ctx.workdir, ctx.repo.image, "; ".join(lines), network=True, workdir_read_only=True, timeout_s=600)
        avail = {}
        for line in r.output.splitlines():
            if line.startswith("@types/") and "=" in line:
                name, ver = line.split("=", 1)
                avail[name] = bool(ver.strip())
        for p in wanted:
            t, major = _types_name(p), _installed_major(ctx.workdir, p)
            if major and avail.get(f"{t}@{major}"):
                found.append(f"{t}@{major}")
            elif avail.get(t):
                found.append(t)
    specs = [f"@types/node@{node_major}", f"typescript@{TS_VERSION}", *found]
    if not ctx.cfg.get("js_to_ts", {}).get("install_types", True):
        ctx.log(f"setup: install_types disabled; not installing {specs}")
        return []
    _install_dev(ctx, specs)
    return specs


def write_tsconfig(ctx: RunContext, style: str, node_major: int, allow_js: bool = True, node_types: bool = True) -> str:
    name = TSCONFIG_DEFAULT if not (ctx.workdir / TSCONFIG_DEFAULT).exists() or _is_ours(ctx) else TSCONFIG_FALLBACK
    module, resolution = ("preserve", "bundler") if style == "esm-bundled" else ("nodenext", "nodenext")
    cfg = {
        "compilerOptions": {
            "target": _target(node_major),
            "module": module,
            "moduleResolution": resolution,
            "strict": True,
            "allowJs": allow_js,
            "checkJs": False,
            "esModuleInterop": True,
            "resolveJsonModule": True,
            "skipLibCheck": True,
            "useDefineForClassFields": True,
            "forceConsistentCasingInFileNames": True,
            "types": ["node"] if node_types else [],
            "outDir": "dist",
        },
        "include": list(ctx.repo.source_dirs),
        "//": "written by migration-swarm",
    }
    (ctx.workdir / name).write_text(json.dumps(cfg, indent=2) + "\n")
    return name


def _is_ours(ctx: RunContext) -> bool:
    p = ctx.workdir / TSCONFIG_DEFAULT
    return p.exists() and "written by migration-swarm" in p.read_text()


def judge_request(ctx: RunContext, tsconfig: str, out: str, *, emit: bool, escape_files=(), normalize=(),
                  norm_dir: str = "", list_exports=()) -> dict:
    """Paths: tsconfig relative to the workdir; out/norm_dir relative to the run dir (mounted at /scratch).

    Diagnostics on files that are still JavaScript are dropped (checkJs is off; they are not ours to fix).
    """
    res = tools.run_judge_script(ctx.workdir, ctx.run_dir, {
        "project": f"/work/{tsconfig}", "outDir": f"/scratch/{out}", "buildInfo": f"/scratch/{out}.tsbuildinfo",
        "emit": emit, "escapeFiles": list(escape_files), "normalize": list(normalize),
        "normDir": f"/scratch/{norm_dir}" if norm_dir else "", "listExports": list(list_exports),
    })
    res["diagnostics"] = [d for d in res["diagnostics"]
                          if d["file"] is None or not d["file"].endswith((".js", ".mjs", ".cjs", ".jsx"))]
    return res


def build_overlay(ctx: RunContext, files: dict[str, Path]) -> Path:
    """files: original js path -> host file whose content should appear at that path."""
    overlay = ctx.run_dir / "overlay"
    if overlay.exists():
        shutil.rmtree(overlay)
    for js, src in files.items():
        dest = overlay / js
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    overlay.mkdir(exist_ok=True)
    return overlay


def setup(ctx: RunContext, analysis: Analysis) -> dict:
    ensure_installed(ctx)
    ctx.log("setup: baseline test run on the untouched repo")
    baseline = run_tests(ctx, overlay=None)
    ctx.log(f"setup: baseline ok={baseline.ok} passed={baseline.passed} failed={baseline.failed} skipped={baseline.skipped}")
    if not baseline.ok or baseline.passed == 0:
        raise SetupFailed("the test suite does not pass on the untouched repo, so behaviour cannot be judged: "
                          + baseline.tail[-1500:])

    node_major = _node_major(ctx)
    style = _module_style(ctx, analysis)
    ctx.log(f"setup: node {node_major}, module style {style}; installing @types")
    installed = install_types(ctx, analysis, node_major)
    tsconfig = write_tsconfig(ctx, style, node_major, node_types=any(s.startswith("@types/node") for s in installed))

    ctx.log("setup: gate - emit untouched JS through tsc and rerun tests on the emitted JS")
    res = judge_request(ctx, tsconfig, "orig_emit", emit=True, normalize=analysis.files, norm_dir="orig_norm",
                        list_exports=analysis.externals)
    npm_exports = {p: {**e, "version": json.loads((ctx.workdir / "node_modules" / p / "package.json").read_text()).get("version")
                       if (ctx.workdir / "node_modules" / p / "package.json").exists() else None}
                   for p, e in res.get("packageExports", {}).items()}
    config_errors = [d for d in res["diagnostics"] if d["file"] is None or d["file"].endswith(tsconfig)]
    if config_errors:
        raise SetupFailed("tsconfig errors: " + "; ".join(f"TS{d['code']}: {d['message']}" for d in config_errors[:10]))
    missing = [f for f in analysis.files if not (ctx.run_dir / "orig_emit" / f).exists()]
    if missing:
        raise SetupFailed(f"tsc did not emit {len(missing)} files, e.g. {missing[:5]}")
    gate = run_tests(ctx, build_overlay(ctx, {f: ctx.run_dir / "orig_emit" / f for f in analysis.files}))
    ctx.log(f"setup: gate ok={gate.ok} passed={gate.passed} failed={gate.failed}")
    if not gate.no_worse_than(baseline):
        raise SetupFailed(f"tests on tsc-emitted original JS differ from baseline: gate={gate.to_json()} baseline={baseline.to_json()}")

    protected = integrity.snapshot(ctx.workdir, ctx.repo.test_paths, regex=DEFAULT_TEST_RE)
    ctx.commit("migration-swarm: setup (tsconfig, @types)")
    setup_commit = ctx.git("rev-parse", "HEAD")
    info = {
        "baseline": baseline.to_json(), "gate": gate.to_json(), "node_major": node_major, "module_style": style,
        "tsconfig": tsconfig, "installed": installed, "protected": protected, "setup_commit": setup_commit,
        "shared_types_path": shared_types_path(ctx), "npm_exports": npm_exports,
    }
    (ctx.run_dir / "setup.json").write_text(json.dumps(info, indent=1))
    return info
