"""The js-to-ts cheat detector (TS compiler API, runs in Docker) on one fixture per escape type."""

import json
from pathlib import Path

import pytest

from migration_swarm.migrations.js_to_ts import tools

FIXTURE = Path(__file__).parent / "fixtures" / "escapes"


@pytest.mark.docker
def test_each_escape_type_is_counted_exactly(tmp_path):
    expected = json.loads((FIXTURE / "expected.json").read_text())
    result = tools.run_judge_script(FIXTURE, tmp_path, {
        "project": "/work/tsconfig.json", "outDir": "/scratch/out", "buildInfo": "/scratch/tsbuildinfo",
        "emit": False, "escapeFiles": list(expected),
    })
    for file, want in expected.items():
        got = {k: v for k, v in result["escapes"][file]["counts"].items() if v}
        assert got == want, file
