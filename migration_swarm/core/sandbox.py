"""Run untrusted commands (installs, builds, tests, analyzers) inside a locked-down Docker container.

Only the given workdir (plus explicit extra mounts) is mounted. No home directory, no host env
vars, no credentials. Network is off unless the caller asks for it (package installs need it).
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


@dataclass(frozen=True)
class Mount:
    host: Path
    container: str
    read_only: bool = True

    def arg(self) -> str:
        return f"{Path(self.host).resolve()}:{self.container}{':ro' if self.read_only else ''}"


def ensure_cache_volume() -> None:
    subprocess.run(["docker", "volume", "create", CACHE_VOLUME], check=True, capture_output=True)
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{CACHE_VOLUME}:/cache", "alpine:3.20",
         "sh", "-c", f"mkdir -p /cache/npm /cache/yarn && chown -R {CONTAINER_UID} /cache"],
        check=True, capture_output=True,
    )


def image_exists(tag: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0


def run(
    workdir: Path,
    image: str,
    command: str,
    *,
    network: bool | str = False,
    timeout_s: int = 1800,
    env: dict[str, str] | None = None,
    memory: str = "6g",
    cpus: str = "4",
    ephemeral: bool = False,
    overlay: Path | None = None,
    workdir_read_only: bool = False,
    mounts: list[Mount] | None = None,
) -> SandboxResult:
    """Run `command` with the workdir at /work.

    ephemeral=True mounts the workdir read-only and runs on a throwaway in-container copy at /tmp/w.
    Use it for test runs: nothing the suite writes reaches the workdir, and the container's own
    filesystem avoids timing races seen on Docker Desktop bind mounts (e.g. hexo's un-awaited unlink).
    overlay (ephemeral only): a host dir whose files are copied over the throwaway copy first.
    """
    name = f"msw-{uuid.uuid4().hex[:12]}"
    all_mounts = [Mount(workdir, "/work", read_only=ephemeral or workdir_read_only), *(mounts or [])]
    if ephemeral:
        prep = "cp -a /work /tmp/w"
        if overlay is not None:
            all_mounts.append(Mount(overlay, "/overlay", read_only=True))
            prep += " && cp -a /overlay/. /tmp/w/"
        command = f"{prep} && cd /tmp/w && {command}"
    args = [
        "docker", "run", "--rm", "--init", "--name", name,
        "--network", network if isinstance(network, str) else ("bridge" if network else "none"),
        "--user", CONTAINER_UID,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", memory, "--cpus", cpus, "--pids-limit", "4096",
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
    for m in all_mounts:
        args += ["-v", m.arg()]
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
