"""The pinned js-to-ts toolchain image (TypeScript compiler API + dependency-cruiser)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from migration_swarm.core import sandbox
from migration_swarm.core.sandbox import Mount

TOOLS_DIR = Path(__file__).parent / "tools"
NODE_DIR = TOOLS_DIR / "node"


def image_tag() -> str:
    h = hashlib.sha256()
    for name in ("Dockerfile", "package.json", "package-lock.json"):
        h.update((TOOLS_DIR / name).read_bytes())
    return f"msw-js-tools:{h.hexdigest()[:12]}"


def ensure_image() -> str:
    tag = image_tag()
    if not sandbox.image_exists(tag):
        subprocess.run(["docker", "build", "-q", "-t", tag, str(TOOLS_DIR)], check=True, capture_output=True)
    return tag


def run_tool(workdir: Path, command: str, *, mounts: list[Mount] | None = None, read_only: bool = True,
             timeout_s: int = 1800) -> sandbox.SandboxResult:
    """Run a command in the tools image. Our node scripts are at /opt/msw/scripts (read-only)."""
    return sandbox.run(
        workdir, ensure_image(), command,
        network=False, timeout_s=timeout_s, workdir_read_only=read_only,
        mounts=[Mount(NODE_DIR, "/opt/msw/scripts"), *(mounts or [])],
    )


def run_judge_script(workdir: Path, scratch: Path, request: dict, timeout_s: int = 1800) -> dict:
    """Run node/judge.mjs. `request` paths must be container paths (/work/..., /scratch/...)."""
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".json", dir=scratch, delete=False) as f:
        json.dump({**request, "result": "/scratch/_judge_result.json"}, f)
        req_name = Path(f.name).name
    res = run_tool(workdir, f"node /opt/msw/scripts/judge.mjs /scratch/{req_name}",
                   mounts=[Mount(scratch, "/scratch", read_only=False)], timeout_s=timeout_s)
    out = scratch / "_judge_result.json"
    if not res.ok or not out.exists():
        raise RuntimeError(f"judge.mjs failed (exit {res.exit_code}):\n{res.output[-4000:]}")
    data = json.loads(out.read_text())
    out.unlink()
    (scratch / req_name).unlink()
    return data
