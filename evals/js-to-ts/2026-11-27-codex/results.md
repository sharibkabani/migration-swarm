# Cheerio single-agent Codex pilot

One interactive Codex session migrated the pinned Cheerio checkout. The agent used the pre-migration source, its shipped declarations, ad hoc editing scripts, compiler diagnostics and evaluator feedback. The session had prior context about the swarm's Cheerio results. The target branch stayed local. Target compilation and tests ran in Docker. Cost and token usage were unmetered.

| Metric | Codex single | Earlier swarm | Edit-retry swarm |
|---|---:|---:|---:|
| Migration wall time | 5m 20s | 20m 18s | 20m 53s |
| Files migrated | 13/13 | 13/13 | 12/13 |
| Strict errors | 0 | 0 | 0 |
| Tests passed | 693 | 693 | 693 |
| Explicit any | 0 | 231 | 3 |
| Equivalent ground-truth exports | 31/91 | 6/91 | 13/91 |
| API cost | Unmetered | $1.53 | $1.16 |

**Evaluator success:** yes. No protected test changes or normalized emitted-JS token differences. **Per-file escape budgets:** exceeded in one file. Traversing used 6 casts and 8 non-null assertions against limits of 5 each. Totals were 34 casts and 21 non-null assertions. Six of 91 reference exports had identical types, 28 comparisons were weakened by inferred any, and 12 exports were missing from the declaration comparison.

The Codex timer ran from the first recorded setup timestamp through the local candidate commit, including intermediate scoring and edits. Final scoring finished at 5m 48s from start. The engine's swarm timer excludes independent final scoring. Each column represents one run. The historical swarms predate filtered inference and the retry-diagnostics fix.
