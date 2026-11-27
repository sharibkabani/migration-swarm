"""Runtime-drift check: canonical token streams of compiled JS (judge.mjs) + token diff (judge.py)."""

from pathlib import Path

import pytest

from migration_swarm.migrations.js_to_ts import tools
from migration_swarm.migrations.js_to_ts.judge import drift

FIXTURE = Path(__file__).parent / "fixtures" / "drift"
CASES = ["options.js", "parser.js", "fns.js", "jsdoc.js", "parens.js", "gutted.js", "precedence.js"]


def _tokens(tmp_path, side):
    tools.run_judge_script(FIXTURE, tmp_path, {
        "project": "/work/tsconfig.json", "outDir": f"/work/{side}", "buildInfo": f"/scratch/{side}.tsbuildinfo",
        "emit": False, "normalize": CASES, "normDir": f"/scratch/{side}_norm"})
    return {c: (tmp_path / f"{side}_norm" / c).read_text() for c in CASES}


@pytest.mark.docker
def test_equivalent_commonjs_export_forms_have_no_drift_but_gutting_is_caught(tmp_path):
    orig, cand = _tokens(tmp_path, "orig"), _tokens(tmp_path, "cand")
    for case in ("options.js", "parser.js", "fns.js", "jsdoc.js", "parens.js"):
        removed, added, rex, aex = drift(orig[case], cand[case])
        assert (removed, added) == (0, 0), (case, rex, aex)
    removed, added, rex, _ = drift(orig["gutted.js"], cand["gutted.js"])
    assert removed >= 3 and "a + 1" in rex[0]
    assert drift(orig["precedence.js"], cand["precedence.js"])[0] > 0   # dropping needed parens is a change


def test_drift_counts_tokens():
    assert drift("a\n=\n1\n", "a\n=\n1\n")[:2] == (0, 0)
    removed, added, rex, aex = drift("return\na\n+\n1\n", "return\n0\n")
    assert (removed, added) == (3, 1) and "[a + 1]" in rex[0] and "[0]" in aex[0]
