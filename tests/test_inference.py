import json

import pytest

from migration_swarm.migrations.js_to_ts.tools import run_tool


@pytest.mark.docker
def test_inference_returns_only_improved_drafts_without_any(tmp_path):
    (tmp_path / "tsconfig.json").write_text(json.dumps({"compilerOptions": {"strict": True, "allowJs": True}}))
    files = ["use.cjs", "already.mjs"]
    (tmp_path / "files.json").write_text(json.dumps({"files": files}))
    source = "function plus(x, y) { return x * 2 + y.thing; }\n"
    (tmp_path / files[0]).write_text(source)
    (tmp_path / files[1]).write_text("export const n = 1;\n")
    result = run_tool(tmp_path, "node /opt/msw/scripts/infer.mjs tsconfig.json files.json")
    assert result.ok, result.output
    seeds = json.loads(result.output)
    assert list(seeds) == ["use.cjs"]
    seed = seeds["use.cjs"]
    assert seed["after"] < seed["before"]
    draft = seed["files"]["use.cts"]
    assert "x: number" in draft and "any" not in draft
    assert (tmp_path / files[0]).read_text() == source
    assert not (tmp_path / "use.cts").exists()
