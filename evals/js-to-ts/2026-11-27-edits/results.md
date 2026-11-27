# Eval: js-to-ts (2026-11-27)

Systems: swarm  ·  repos: cheerio  ·  runs per cell: 1  ·  budget per run: $5.0

Values are mean ± sample standard deviation over runs. Every run is in `runs.csv`.

| system | repo | runs | success | tests equal | migrated | strict-clean files | strict errors | test violations | any /kLOC | ts-ignore etc /kLOC | as casts /kLOC | non-null /kLOC | runtime tokens changed | GT types equivalent | GT types identical | cost | $/kLOC | wall s | retries/unit | escalation rate | degraded files |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| swarm | cheerio | 1 | 0% | 100% | 92% | 92% | 0 | 0 | 1.95 | 0 | 22.7 | 1.95 | 0 | 14% | 9% | $1.16 | $0.75 | 1.25e+03 | 1.69 | 0.308 | 3 |

## Metric definitions

```
migrated_files      source files whose .js is gone and whose .ts exists
  strict_clean_files  migrated files with zero errors under strict (share is over ALL source files)
  tests_equal         passed >= baseline passed, failed <= baseline failed, no new failing names,
                      same exit outcome, no suite-load errors beyond baseline
  test_violations     protected test files modified, deleted, or renamed with runtime changes
  success             tests_equal AND no test_violations AND every source file migrated AND
                      zero strict errors in migrated files

Ground truth (gt_*), for repos with a human migration:
(see tools/node/gtcompare.mjs for the checker logic):
  - Both sides are reduced to declarations (.d.ts) emitted by TypeScript 6.0.3: the human migration at
    `ground_truth_sha` (built with a declaration-only tsconfig over its source dirs, tests excluded) and the
    system's final tree (the evaluator's own emit; files still in JS get JS-inferred declarations).
  - Modules are paired by path (with the manifest's ground_truth_path_map, e.g. cheerio lib/ -> src/).
  - For each symbol the human module exports (for `export =` modules: the whole export and each property),
    the system's same-named export is compared inside ONE TypeScript program:
        equivalent    mutually assignable and the system's printed type contains no `any` (lenient: a
                      non-generic signature can be equivalent to a generic one)
        identical     equivalent and both printed types are the same string (strict)
        any_weakened  mutually assignable only thanks to `any` on the system side
        wider / narrower / incomparable / missing
  - gt_equivalent_share / gt_identical_share = count / all human-exported symbols in paired modules
    (missing counts against).
Caveat: the human migrations sometimes refactored APIs or bumped dependencies in the same change; such
symbols show up as missing or incomparable. The share is a similarity measure, not a correctness score.
```
