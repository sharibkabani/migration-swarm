"""Retries as SEARCH/REPLACE edits against the previous (rejected) attempt."""

from migration_swarm.migrations.js_to_ts.prompts import parse_worker_output

PREV = {"lib/a.ts": "export function f(x) {\n  return x.y;\n}\nexport const g = (z) => z;\n"}


def edit(path, search, replace):
    return f"<<<<<<< SEARCH {path}\n{search}=======\n{replace}>>>>>>> REPLACE\n"


def test_edits_apply_to_previous_attempt():
    text = (edit("lib/a.ts", "export function f(x) {\n", "export function f(x: { y: number }): number {\n")
            + "\n" + edit("lib/a.ts", "export const g = (z) => z;\n", "export const g = (z: string): string => z;\n"))
    files, reqs, problems = parse_worker_output(text, ["lib/a.js"], PREV)
    assert problems == [] and reqs == []
    assert files["lib/a.ts"] == ("export function f(x: { y: number }): number {\n  return x.y;\n}\n"
                                 "export const g = (z: string): string => z;\n")


def test_fenced_edits_and_json_requests():
    text = ("```ts\n" + edit("lib/a.ts", "  return x.y;\n", "  return x.y as number;\n") + "```\n"
            '```json\n{"shared_type_requests": [{"name": "Y", "definition": "export type Y = 1;"}]}\n```')
    files, reqs, problems = parse_worker_output(text, ["lib/a.js"], PREV)
    assert problems == [] and "x.y as number" in files["lib/a.ts"] and reqs[0]["name"] == "Y"


def test_edit_that_does_not_match_drops_the_file_with_a_clear_reason():
    text = edit("lib/a.ts", "return x.z;\n", "return 1;\n")
    files, _, problems = parse_worker_output(text, ["lib/a.js"], PREV)
    assert files == {}
    assert any("was not found" in p for p in problems) and any("no edits that applied" in p for p in problems)
    ambiguous = {"lib/a.ts": "a;\na;\n"}
    _, _, problems = parse_worker_output(edit("lib/a.ts", "a;\n", "b;\n"), ["lib/a.js"], ambiguous)
    assert any("matches 2 places" in p for p in problems)


def test_full_file_block_beats_edits_and_works_without_previous():
    text = "```ts\n// file: lib/a.ts\nexport const k = 1;\n```\n" + edit("lib/a.ts", "  return x.y;\n", "  return 2;\n")
    files, _, problems = parse_worker_output(text, ["lib/a.js"], PREV)
    assert files["lib/a.ts"] == "export const k = 1;\n" and problems == []
    files, _, problems = parse_worker_output("```ts\n// file: lib/a.ts\nexport const k = 1;\n```", ["lib/a.js"])
    assert files["lib/a.ts"] == "export const k = 1;\n" and problems == []


def test_retry_preserves_feedback_for_the_exact_draft_after_cutoffs():
    from migration_swarm.core.graph import retry_context

    history = [{"output_files": {"lib/a.ts": "older"}, "reasons": ["stale"]},
               {"output_files": PREV, "reasons": ["TS2322 at line 2"]},
               {"output_files": None, "reasons": ["cut off"]}]
    assert retry_context(history) == {"previous": PREV, "reasons": ["TS2322 at line 2", "cut off"]}
    seed = {"inferred": True, "previous": PREV, "reasons": ["TS7006"]}
    assert retry_context([], seed) == seed
    assert retry_context(history[-1:], seed)["reasons"] == ["TS7006", "cut off"]


def test_prompt_selects_one_output_format():
    from migration_swarm.migrations.js_to_ts.prompts import EDIT_FORMAT, FULL_FORMAT, worker_messages

    args = dict(style="cjs", shared_path="shared.ts", files={"lib/a.js": "f(1)"},
                deps_dts={}, shared_types=None, dependents=[], degraded=False)
    first, _ = worker_messages(**args, retry=None)
    retry, _ = worker_messages(**args, retry={"previous": PREV, "reasons": ["type error"]})
    assert FULL_FORMAT in first and EDIT_FORMAT not in first
    assert EDIT_FORMAT in retry and FULL_FORMAT not in retry


def test_parsed_edit_outcomes():
    for reply, expected in [
        (edit("lib/a.ts", "  return x.y;\n", "  return 2;\n"), "applied_edit"),
        (edit("lib/a.ts", "absent\n", "value\n"), "rejected_edit"),
        ("no blocks", "no_usable_output"),
        ("```ts\nexport const x = 1;\n```", "full_file"),
    ]:
        outcomes = {}
        parse_worker_output(reply, ["lib/a.js"], PREV, outcomes=outcomes)
        assert outcomes == {"lib/a.ts": expected}
