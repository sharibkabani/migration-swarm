# migration-swarm

Migrate a codebase with a swarm of LLM agents. A deterministic judge checks every file the agents
write. The first supported migration is **JavaScript → strict TypeScript**.

The full write-up (architecture diagram, benchmark results, limitations) is still in progress.
See `docs/ARCHITECTURE.md` for the design.

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
migration-swarm
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

What the judge requires of every file:

- zero errors under `strict`;
- no `any`, `@ts-ignore` or `@ts-nocheck`, and only a few casts;
- compiled output that matches the original JavaScript token for token;
- your unchanged test suite still passing. Test files are hashed and read-only.

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
migration-swarm status <run-id>
migration-swarm resume <run-id>
migration-swarm report <run-id>
```

Keys are read from the environment, `./.env` or `~/.migration-swarm/.env`:

| Provider | Key |
|---|---|
| Claude | `ANTHROPIC_API_KEY` |
| Gemini | `GOOGLE_API_KEY` or `GEMINI_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |

Runs are stored in `~/.migration-swarm/runs`.
