import shutil
import subprocess

import pytest


def _docker_ok() -> bool:
    return shutil.which("docker") is not None and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def pytest_collection_modifyitems(config, items):
    if _docker_ok():
        return
    skip = pytest.mark.skip(reason="docker not available")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)
