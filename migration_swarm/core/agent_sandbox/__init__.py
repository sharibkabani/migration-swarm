"""Run a coding agent (Claude Code) on untrusted repo code, isolated.

  - The agent container is attached ONLY to an internal Docker network (no route out).
  - A tinyproxy container bridges that network to the internet and allows CONNECT to an allowlist
    (api.anthropic.com, registry.npmjs.org) on port 443 only. Everything else is refused.
  - All capabilities dropped, no-new-privileges, non-root, memory/pid limits, only the run's fresh
    clone mounted read-write, no host env or credentials except the baseline API key passed in.
  - The credential passed in (a separate, spend-limited API key, or a Claude plan OAuth token from
    `claude setup-token`) is readable by untrusted test code in the container. Use one you can revoke.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from migration_swarm.core import sandbox
from migration_swarm.core.sandbox import Mount

HERE = Path(__file__).parent
NETWORK = "msw-agent-net"
PROXY = "msw-egress"
CLAUDE_CODE_VERSION = "2.1.332"


def _hash(*paths: Path, extra: str = "") -> str:
    h = hashlib.sha256(extra.encode())
    for p in paths:
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def ensure_egress() -> str:
    d = HERE / "egress"
    tag = f"msw-egress:{_hash(d / 'Dockerfile', d / 'tinyproxy.conf', d / 'filter')}"
    if not sandbox.image_exists(tag):
        _docker("build", "-q", "-t", tag, str(d))
    if _docker("network", "inspect", NETWORK, check=False).returncode != 0:
        _docker("network", "create", "--internal", NETWORK)
    running = _docker("ps", "-q", "--filter", f"name=^{PROXY}$").stdout.strip()
    if running:
        image = _docker("inspect", "-f", "{{.Config.Image}}", PROXY).stdout.strip()
        if image == tag:
            return f"http://{PROXY}:8888"
    _docker("rm", "-f", PROXY, check=False)
    _docker("run", "-d", "--name", PROXY, "--network", NETWORK, "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--memory", "256m", "--restart", "unless-stopped", tag)
    _docker("network", "connect", "bridge", PROXY)
    return f"http://{PROXY}:8888"


def agent_image(base: str) -> str:
    tag = f"msw-agent:{_hash(HERE / 'Dockerfile.agent', extra=base + CLAUDE_CODE_VERSION)}"
    if not sandbox.image_exists(tag):
        _docker("build", "-q", "-t", tag, "-f", str(HERE / "Dockerfile.agent"), "--build-arg", f"BASE={base}",
                "--build-arg", f"CLAUDE_CODE_VERSION={CLAUDE_CODE_VERSION}", str(HERE))
    return tag


def run_agent(workdir: Path, base_image: str, command: str, *, auth_env: dict[str, str], timeout_s: int,
              mounts: list[Mount] | None = None, env: dict[str, str] | None = None) -> sandbox.SandboxResult:
    proxy = ensure_egress()
    proxy_env = {
        "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy,
        "npm_config_https_proxy": proxy, "npm_config_proxy": proxy, "NO_PROXY": "localhost,127.0.0.1",
        **auth_env, "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1", **(env or {}),
    }
    return sandbox.run(workdir, agent_image(base_image), command, network=NETWORK, timeout_s=timeout_s,
                       env=proxy_env, mounts=mounts, memory="8g", cpus="4")
