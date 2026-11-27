# Phase 1 results (2026-11-27)

First real runs of the engine (Haiku 5.5 workers, Sonnet 5.5 escalation and steward) on
cheerio @ `8b0792d` (1,539 LOC, 13 files, CommonJS, jest, 693 tests). These are development runs,
not benchmark numbers. Phase 2's harness produces those.

## Run 4 (first clean run, default config, $5 budget)

| | |
|---|---|
| Outcome | complete |
| Strict `tsc` on the result | 0 errors (allowJs still on: 1 file left as JS) |
| Tests on migrated code | 693 passed / 0 failed, same as baseline |
| Protected test files changed | none |
| Files | 8 accepted, 4 degraded, 1 failed (left as JS) |
| Escapes in accepted files | explicit any 0, ts-ignore 0, as casts 18, non-null 1, runtime changes 0 |
| Escapes in degraded files | explicit any 118 (90 in `api/manipulation`), as casts 10 |
| Cost | $1.62 (42 calls). Haiku: 29 calls, $0.21. Sonnet: 13 calls, $1.41 |
| Wall clock | 26 min (5 layers; Sonnet calls on the 350-LOC API files dominate) |
| Retries per unit / escalation rate | 2.15 / 0.54 |

Per file: `options`, `forms`, `utils`, `css`, `parsers/htmlparser2` were accepted on the first or
second Haiku attempt. `parse`, `load`, `parsers/parse5` needed 3-4 attempts. `api/attributes`,
`api/manipulation`, `cheerio` and `static` ended degraded. `api/traversing` failed all 5 attempts
(the last one on `Cheerio` vs `Node[]` type errors) and stays JS.

## Engine bugs found by real runs (all fixed in `0b3a043`)

1. Line-based drift check rejected equivalent CommonJS export forms. Replaced with canonical AST
   token streams, export plumbing folded, JSDoc skipped.
2. Statically rejected files were reverted only after the test run, so the suite crashed on a
   missing module and other units' verdicts were poisoned.
3. Diagnostics from our extra `.d.ts` emit (TS9005 on JS files) were counted as errors.
4. Flat 32k `max_tokens` made worst-case reservations so large that a $1 budget stopped at $0.03.
5. `@types/<pkg>@latest` can be a stub for an older major. `@types` is now pinned to the installed
   major.
6. Workers guessed npm type names (`ChildNode` is not exported by domhandler 4.x). Workers now get
   each imported package's version and real exported names, read via the TS compiler.

After fixes 1-6, layer 0 went from 2/5 to 4/5 units accepted on the first attempt.

## What this says for Phase 2

- Sonnet escalations are ~85-87% of cost. First-pass Haiku quality is the main cost lever.
- Strictly typing the core of a fluent-API library like cheerio is hard. Expect degraded files on
  such repos, and report them as such, not as successes.
- Wall clock is dominated by long LLM calls on large files, not by tsc or tests.
