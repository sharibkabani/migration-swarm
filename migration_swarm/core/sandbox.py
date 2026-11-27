"""Run untrusted commands (installs, builds, tests) inside a locked-down Docker container.

Only the given workdir is mounted. No home directory, no host env vars, no credentials.
Network is off unless the caller asks for it (package installs need the registry).
"""

from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

CACHE_VOLUME = "msw-pkg-cache"  # shared npm/yarn download cache; never mounted on the host
CONTAINER_UID = "1000:1000"  # the `node` user in official node images


@dataclass
class SandboxResult:
    exit_code: int
    output: str
    duration_s: float
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def ensure_cache_volume() -> None:
    subprocess.run(["docker", "volume", "create", CACHE_VOLUME], check=True, capture_output=True)
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{CACHE_VOLUME}:/cache", "alpine:3.20",
         "sh", "-c", f"mkdir -p /cache/npm /cache/yarn && chown -R {CONTAINER_UID} /cache"],
        check=True, capture_output=True,
    )


def run(
    workdir: Path,
    image: str,
    command: str,
    *,
    network: bool = False,
    timeout_s: int = 1800,
    env: dict[str, str] | None = None,
    memory: str = "6g",
    cpus: str = "4",
    ephemeral: bool = False,
) -> SandboxResult:
    """ephemeral=True mounts the workdir read-only and runs on a throwaway in-container copy.

    Use it for test runs: nothing the suite writes reaches the workdir, and the container's own
    filesystem avoids timing races seen on Docker Desktop bind mounts (e.g. hexo's un-awaited unlink).
    """
    name = f"msw-{uuid.uuid4().hex[:12]}"
    if ephemeral:
        command = f"cp -a /work /tmp/w && cd /tmp/w && {command}"
    args = [
        "docker", "run", "--rm", "--init", "--name", name,
        "--network", "bridge" if network else "none",
        "--user", CONTAINER_UID,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", memory, "--cpus", cpus, "--pids-limit", "4096",
        "-v", f"{workdir.resolve()}:/work{':ro' if ephemeral else ''}",
        "-v", f"{CACHE_VOLUME}:/cache",
        "-w", "/work",
        "-e", "HOME=/tmp/home",
        "-e", "CI=true",
        "-e", "npm_config_cache=/cache/npm",
        "-e", "YARN_CACHE_FOLDER=/cache/yarn",
        "-e", "npm_config_update_notifier=false",
        "-e", "npm_config_fund=false",
        "-e", "npm_config_audit=false",
    ]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    args += [image, "bash", "-lc", command]

    start = time.monotonic()
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s, errors="replace")
        return SandboxResult(proc.returncode, proc.stdout + proc.stderr, time.monotonic() - start, False)
    except subprocess.TimeoutExpired as e:
        subprocess.run(["docker", "kill", name], capture_output=True)
        out = (e.stdout or b"").decode(errors="replace") + (e.stderr or b"").decode(errors="replace")
        return SandboxResult(124, out, time.monotonic() - start, True)
