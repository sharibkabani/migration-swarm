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
