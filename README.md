# migration-swarm

Migrate a codebase with a swarm of LLM agents checked by a deterministic judge. The first supported
migration is **JavaScript → strict TypeScript** (`js-to-ts`).

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the technical design.

## Goals

- **Verifiable, zero-trust migrations.** Your repository is cloned into an isolated workspace and
  never modified in place. Dependency installation and test suites run inside Docker containers with
  networking disabled during tests, and test files are hashed and kept read-only.
- **Strict correctness enforced by a deterministic judge.** Every migrated file must pass:
  - zero errors under TypeScript `strict`;
  - no `any`, `@ts-ignore`, `@ts-expect-error`, or `@ts-nocheck`, and tight per-file budgets for
    casts (`as`) and non-null assertions (`!`);
  - AST-normalized emitted JavaScript matching the original JS (catching agents that delete or alter
    runtime logic to silence type errors);
  - the repo's unchanged test suite passing against a **shadow build** (`tsc`-emitted JS at the
    original paths, with bisection to attribute test regressions to specific files).
- **Cost- and latency-efficient parallelism.** Use cheap worker models concurrently across
  independent files (scheduled as soon as their imports resolve, rather than waiting on full layer
  barriers), escalate only failing units to stronger models, coordinate cross-file types through a
  type-only steward, and enforce hard USD spend caps with checkpointed resume.
- **Reproducible, apples-to-apples benchmarking.** Evaluate the swarm alongside `ts-migrate` and
  single-agent/subagent baselines across real open-source repositories, comparing strict cleanliness,
  escape usage, runtime equivalence, cost, wall time, and declaration similarity against human
  ground-truth migrations.

## What's Been Done So Far

### Built & Working

- **Architecture & verified 7-repo benchmark (`benchmarks/js_to_ts/manifest.yaml`)** — Verified
  baseline test stability (3/3 identical runs) across `cheerio`, `yup`, `marked`, `hexo`, `knex`,
  `stylelint`, and `mathjs` (1,539 to 37,607 source LOC; CommonJS and ESM; Jest, Mocha, and
  Jasmine), with human ground-truth commits mapped for `cheerio`, `yup`, `marked`, and `hexo`.
- **Core swarm engine (`migration_swarm/core/`)** — LangGraph workflow with SQLite checkpointing
  (`analyze → plan → setup → migrate → finalize → report`), Tarjan SCC cycle grouping,
  dependency-driven async scheduling, per-unit write/judge/retry/escalation ladders, hard USD
  budget metering, and `status` / `watch` / `cancel` / `resume` support.
- **JS → TS migration pack (`migration_swarm/migrations/js_to_ts/`)** — `dependency-cruiser`
  analysis, deterministic baseline setup gate, pinned TypeScript 6.0.3 Docker tools image, shadow-build
  test runner with failure bisection, AST cheat detector and emitted-JS drift checker, shared-types
  steward, npm export inspection, filtered `infer-from-usage` draft seeding, and `SEARCH`/`REPLACE`
  edit-based retries.
- **Evaluation harness & baselines (`migration-swarm eval`)** — Uniform scoring across systems into
  `results.md` / `results.csv` / `runs.csv`, TypeScript `.d.ts` ground-truth comparison
  (`tools/node/gtcompare.mjs`), `ts-migrate` baseline, and containerized Claude Code baselines.
- **Interactive TUI, providers, & Claude Code integration** — Rich/questionary interactive wizard
  with natural-language request routing and pre-spend plan approval, `--backend plan` support for
  Claude Pro/Max plan logins (with rate-limit pause/resume), multi-provider wiring (Claude, Gemini,
  OpenAI — Gemini and OpenAI are wired but not yet benchmarked), `uv`/`pip` packaging, stdio MCP
  server (`migration-swarm mcp`), and Claude Code plugin/skill (`.claude-plugin/`).
- **Test coverage** — 37 non-Docker unit/integration tests covering planning, scheduling, edits,
  inference, judge/cheat detection, budget caps, resume, interactive flow, and the MCP server
  (plus 9 Docker-marked tests).

### Early Pilot Results (Cheerio & Yup)

Full multi-repo benchmark sweeps are still ahead; so far we have `ts-migrate` baselines on
`cheerio` and `yup` plus single-run development pilots on `cheerio` (1,539 LOC, 13 files, 693 tests):

| System / Pilot | Repo | Migrated | Strict Errors | Tests | Explicit `any` /kLOC | GT Equiv | Cost | Wall Time |
|---|---|---:|---:|---|---:|---:|---:|---:|
| `ts-migrate` (3 runs) | `cheerio` | 13/13 | 40.3 ± 3.5 | 693/693 | 183 | 0% | $0.00 | 5.8s |
| `ts-migrate` (3 runs) | `yup` | 26/26 | 9.0 | 572 passed (2 skipped) | 160 | 1% | $0.00 | 8.5s |
| Swarm pilot (`2026-11-27`, full-file retries) | `cheerio` | 13/13 (4 degraded) | 0 | 693/693 | 150 | 7% | $1.53 | 20m 18s |
| Swarm pilot (`2026-11-27-edits`, edit retries) | `cheerio` | 12/13 (3 degraded) | 0 | 693/693 | 1.95 | 14% | $1.16 | 20m 53s |
| Codex single-agent interactive pilot (`2026-11-27-codex`) | `cheerio` | 13/13 (1 over cast/! cap) | 0 | 693/693 | 0 | 34% | Unmetered | 5m 20s |

*Note: Those swarm pilots predate the latest filtered `infer-from-usage` draft seeding and retry-diagnostic fixes (`4962320`).*

### What's Next

- Run the full multi-repo benchmark (including `marked`, `hexo`, `knex`, `stylelint`, `mathjs`,
  containerized Claude Code baselines, and the new inference-seeded pipeline).
- Improve first-pass type accuracy and ground-truth equivalence on fluent/dynamic APIs while
  keeping degraded fallbacks low.
- Validate Gemini and OpenAI providers on real benchmark runs, and add additional migration packs
  beyond `js-to-ts`.

## Install

You need Python ≥ 3.12 and **Docker**. Your repo's install and test commands run inside
containers, never on your machine.

```bash
uv tool install git+https://github.com/sharibkabani/migration-swarm
# or
pip install git+https://github.com/sharibkabani/migration-swarm
```

## Use

```bash
cd your-js-repo
migration-swarm   # or `msw`
```

The interactive mode walks you through four steps:

1. **Provider.** Pick Claude, Gemini or OpenAI. With Claude you can use your Claude plan login
   (Pro/Max) instead of an API key; usage then counts against your plan's limits and isn't billed.
2. **Request.** Describe the migration in your own words, e.g. "convert lib/ to strict TypeScript".
   It is matched to a supported migration, or you get told it isn't supported.
3. **Plan.** Review the plan before anything is spent: files and import-graph layers, the model
   for each role, the budget and a cost estimate, and how the result is judged. Approve it, change
   the budget, or cancel.
4. **Run.** Watch the agents live. Each file gets a worker; failed files are retried with the
   judge's exact errors, then escalated to a stronger model.

The result is a branch in a local clone, plus a report. Your repo is never modified, and nothing
is pushed.

### From Claude Code (plugin: MCP server + skill)

```
/plugin marketplace add sharibkabani/migration-swarm
/plugin install migration-swarm@migration-swarm-tools
```

Then ask Claude something like "migrate this repo to TypeScript with migration-swarm". The skill has
Claude follow a fixed sequence:

1. Plan the run in the background. Nothing is spent at this step.
2. Show you the plan and wait for your approval.
3. Start the run and report progress as it goes.
4. Summarize the report at the end.

MCP tools: `list_migrations`, `start_migration`, `get_status`, `approve_plan`, `get_report`,
`cancel` and `list_runs`. The plugin needs [`uv`](https://docs.astral.sh/uv/). On first start it
installs its own environment into the plugin's data directory, which takes a few seconds. If that
first start is slow on your network, launch Claude with `MCP_TIMEOUT=120000`.

The server can also run without the plugin: `migration-swarm mcp` (stdio).

### Non-interactive

```bash
migration-swarm run ./repo -m js-to-ts --provider gemini --budget 3
migration-swarm run ./repo -m js-to-ts --backend plan        # Claude plan login
migration-swarm watch <run-id>
migration-swarm status <run-id>
migration-swarm resume <run-id>
migration-swarm report <run-id>
migration-swarm eval -m js-to-ts --manifest benchmarks/js_to_ts/manifest.yaml
```

Keys are read from the environment, `./.env` or `~/.migration-swarm/.env`:

| Provider | Key |
|---|---|
| Claude | `ANTHROPIC_API_KEY` |
| Gemini | `GOOGLE_API_KEY` or `GEMINI_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |

Runs are stored in `~/.migration-swarm/runs`.
