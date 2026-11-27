"""Accept/reject logic of the js-to-ts judge (pure functions, no Docker)."""

from migration_swarm.core import config
from migration_swarm.migrations.js_to_ts.judge import decide, drift

BUDGETS = config.load()["escape_budgets"]
CLEAN = {"counts": {"explicit_any": 0, "as_cast": 1}, "sites": [{"kind": "as_cast", "line": 3}]}


def _decide(**kw):
    base = dict(unit_files=["lib/a.ts"], diagnostics=[], escapes={"lib/a.ts": CLEAN}, drifts={},
                budgets=BUDGETS, lifted=set(), sources={"lib/a.ts": "let x = 1;\nlet y: number = 'a';\n"})
    base.update(kw)
    return decide(**base)


def test_clean_file_within_budget_is_accepted():
    v = _decide()
    assert v.accepted and v.reasons == [] and v.escapes["as_cast"] == 1


def test_type_error_rejects_with_file_line_and_source_text():
    v = _decide(diagnostics=[{"file": "lib/a.ts", "line": 2, "col": 5, "code": 2322, "message": "Type 'string' is not assignable to type 'number'."},
                             {"file": "lib/other.ts", "line": 1, "col": 1, "code": 2304, "message": "elsewhere"}])
    assert not v.accepted
    assert len(v.reasons) == 1
    assert v.reasons[0].startswith("lib/a.ts:2:5 TS2322") and "let y: number = 'a';" in v.reasons[0]


def test_escape_over_budget_rejects_and_names_the_sites():
    esc = {"counts": {"explicit_any": 2}, "sites": [{"kind": "explicit_any", "line": 4}, {"kind": "explicit_any", "line": 9}]}
    v = _decide(escapes={"lib/a.ts": esc})
    assert not v.accepted
    assert "explicit_any = 2 (allowed 0)" in v.reasons[0] and "lib/a.ts:4" in v.reasons[0] and "lib/a.ts:9" in v.reasons[0]


def test_degraded_mode_lifts_any_but_never_ts_ignore():
    esc = {"counts": {"explicit_any": 3, "ts_ignore": 1}, "sites": []}
    v = _decide(escapes={"lib/a.ts": esc}, lifted={"explicit_any", "as_cast"})
    assert not v.accepted
    assert len(v.reasons) == 1 and "ts_ignore" in v.reasons[0]
    assert v.escapes["explicit_any"] == 3  # still counted for the report


def test_runtime_drift_rejects_removed_code_but_tolerates_reordering_and_interop_lines():
    original = '"use strict";\nfunction f(n) {\n    return n + 1;\n}\nexports.f = f;\n'
    reordered = ('"use strict";\nObject.defineProperty(exports, "__esModule", { value: true });\n'
                 'exports.f = void 0;\nexports.f = f;\nfunction f(n) {\n    return n + 1;\n}\n')
    removed, added, _, _ = drift(original, reordered)
    assert (removed, added) == (0, 0)
    gutted = '"use strict";\nfunction f(n) {\n    return 0;\n}\nexports.f = f;\n'
    d = drift(original, gutted)
    assert d[0] == 1 and d[1] == 1
    v = _decide(drifts={"lib/a.ts": d})
    assert not v.accepted and "runtime code changed" in v.reasons[0] and "return n + 1;" in v.reasons[0]


def test_file_missing_from_program_is_rejected():
    v = _decide(escapes={"lib/a.ts": {"missing": True}})
    assert not v.accepted and "not part of the TypeScript program" in v.reasons[0]
