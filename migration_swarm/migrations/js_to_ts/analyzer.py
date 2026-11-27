"""analyze (js-to-ts): import graph via dependency-cruiser, cycles, LOC, shared modules, externals."""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path

from migration_swarm.core import sandbox
from migration_swarm.core.context import RunContext
from migration_swarm.core.planner import strongly_connected_components
from migration_swarm.migrations.js_to_ts import tools

JS_EXT = (".js", ".mjs", ".cjs")
DEFAULT_TEST_RE = r"(^|/)(__tests__|__fixtures__|__mocks__|testUtils|test|tests)/|\.(test|spec)\.[cm]?js$"
SHARED_NAME_RE = re.compile(r"(^|/)(utils?|helpers?|constants?|types|models?|common|shared|errors?)(/|\.|$)", re.I)


@dataclass
class Analysis:
    files: list[str]
    edges: dict[str, list[str]]
    loc: dict[str, int]
    module_system: dict[str, str]            # file -> cjs | es6
    externals: list[str]                     # npm package names imported by source files
    uses_node_builtins: bool
    cycles: list[list[str]]
    shared: list[str]
    fan_in: dict[str, int] = field(default_factory=dict)
    file_externals: dict[str, list[str]] = field(default_factory=dict)

    @property
    def total_loc(self) -> int:
        return sum(self.loc.values())

    def to_json(self) -> dict:
        return asdict(self)


def is_test_path(path: str, test_globs: list[str]) -> bool:
    if re.search(DEFAULT_TEST_RE, path):
        return True
    for g in test_globs:
        g = g.split("#", 1)[0]
        if fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g.rstrip("/") + "/*") or path.startswith(g.rstrip("/") + "/"):
            return True
    return False


def count_loc(text: str) -> int:
    n, in_block = 0, False
    for line in text.splitlines():
        s = line.strip()
        if in_block:
            if "*/" in s:
                in_block = False
                s = s.split("*/", 1)[1].strip()
            else:
                continue
        if not s or s.startswith("//"):
            continue
        if s.startswith("/*"):
            if "*/" not in s:
                in_block = True
            continue
        n += 1
    return n


def package_name(spec: str) -> str:
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


def ensure_installed(ctx: RunContext) -> None:
    """Dependencies must be installed for the import graph to resolve npm packages."""
    if (ctx.workdir / "node_modules").exists():
        return
    ctx.log(f"installing dependencies ({ctx.repo.install})")
    r = sandbox.run(ctx.workdir, ctx.repo.image, ctx.repo.install, network=True, timeout_s=2400)
    if not r.ok:
        raise RuntimeError(f"install failed:\n{r.output[-3000:]}")


def analyze(ctx: RunContext) -> Analysis:
    ensure_installed(ctx)
    dirs = " ".join(shlex.quote(d) for d in ctx.repo.source_dirs)
    cmd = (f"/opt/msw/node_modules/.bin/depcruise --no-config --output-type json --do-not-follow '^node_modules' "
           f"--exclude {shlex.quote(DEFAULT_TEST_RE)} {dirs}")
    res = tools.run_tool(ctx.workdir, cmd, timeout_s=1800)
    if not res.ok:
        raise RuntimeError(f"dependency-cruiser failed:\n{res.output[-3000:]}")
    data = json.loads(res.output[res.output.index("{"):])
    (ctx.run_dir / "depcruise.json").write_text(json.dumps(data))

    def in_scope(p: str) -> bool:
        return (p.endswith(JS_EXT) and any(p == d or p.startswith(d.rstrip("/") + "/") for d in ctx.repo.source_dirs)
                and not is_test_path(p, ctx.repo.test_paths))

    files = sorted(m["source"] for m in data["modules"] if in_scope(m["source"]))
    fileset = set(files)
    edges: dict[str, list[str]] = {}
    module_system: dict[str, str] = {}
    externals: set[str] = set()
    file_externals: dict[str, list[str]] = {}
    builtins = False
    for m in data["modules"]:
        src = m["source"]
        if src not in fileset:
            continue
        deps = set()
        ext = set()
        systems = set()
        for d in m.get("dependencies", []):
            systems.add(d.get("moduleSystem", ""))
            if d.get("coreModule"):
                builtins = True
            elif d.get("resolved") in fileset:
                deps.add(d["resolved"])
            elif any(t.startswith("npm") for t in d.get("dependencyTypes", [])):
                externals.add(package_name(d["module"]))
                ext.add(package_name(d["module"]))
        edges[src] = sorted(deps)
        file_externals[src] = sorted(ext)
        module_system[src] = "es6" if "es6" in systems else "cjs" if "cjs" in systems else "unknown"

    loc = {f: count_loc((ctx.workdir / f).read_text(errors="replace")) for f in files}
    fan_in = {f: 0 for f in files}
    for f, ds in edges.items():
        for d in ds:
            fan_in[d] += 1
    cycles = [c for c in strongly_connected_components(files, {k: set(v) for k, v in edges.items()}) if len(c) > 1]
    shared = sorted(f for f in files if fan_in[f] >= 3 or SHARED_NAME_RE.search(f))
    a = Analysis(files, edges, loc, module_system, sorted(externals), builtins, cycles, shared, fan_in, file_externals)
    (ctx.run_dir / "analysis.json").write_text(json.dumps(a.to_json(), indent=1))
    return a


def load(path: Path) -> Analysis:
    return Analysis(**json.loads(path.read_text()))
