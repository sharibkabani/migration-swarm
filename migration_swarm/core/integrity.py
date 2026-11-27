"""Read-only enforcement: hash protected files (tests, test config) and detect any change.

Patterns are supplied by the pack: shell globs over tracked files, a regex, or `file.json#key`
to protect one top-level key of a JSON file (e.g. `package.json#jest`).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import subprocess
from pathlib import Path


def _tracked(workdir: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=workdir, check=True, capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if l]


def _matches(path: str, glob: str) -> bool:
    g = glob.rstrip("/")
    return fnmatch.fnmatch(path, g) or path.startswith(g + "/") or fnmatch.fnmatch(path, g + "/*")


def snapshot(workdir: Path, globs: list[str], regex: str | None = None) -> dict[str, str]:
    files = _tracked(workdir)
    rx = re.compile(regex) if regex else None
    snap: dict[str, str] = {}
    for g in globs:
        if "#" in g:
            fname, key = g.split("#", 1)
            p = workdir / fname
            if p.exists():
                val = json.loads(p.read_text()).get(key)
                snap[g] = hashlib.sha256(json.dumps(val, sort_keys=True).encode()).hexdigest()
    for f in files:
        if any(_matches(f, g) for g in globs if "#" not in g) or (rx and rx.search(f)):
            snap[f] = hashlib.sha256((workdir / f).read_bytes()).hexdigest()
    return snap


def verify(workdir: Path, snap: dict[str, str]) -> list[str]:
    """Return protected paths that were modified or deleted since the snapshot."""
    bad = []
    for key, digest in snap.items():
        if "#" in key:
            fname, k = key.split("#", 1)
            p = workdir / fname
            cur = (hashlib.sha256(json.dumps(json.loads(p.read_text()).get(k), sort_keys=True).encode()).hexdigest()
                   if p.exists() else None)
        else:
            p = workdir / key
            cur = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
        if cur != digest:
            bad.append(key)
    return bad
