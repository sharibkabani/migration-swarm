---
name: migration-swarm
description: Migrate a codebase with migration-swarm (currently JavaScript -> strict TypeScript) - plan it, get the user's approval, run it in the background, report the result. Use when the user asks to convert/migrate a JS repo or directory to TypeScript, add types to a JS codebase, or check on a migration-swarm run.
---

# Migrating a codebase with migration-swarm

migration-swarm runs parallel LLM agents, one per file. Each file starts as soon as the files it imports
are done. A
deterministic judge accepts or rejects every file:

- strict `tsc` with zero errors;
- no `any`, `@ts-ignore` or `@ts-nocheck`;
- compiled output identical to the original JavaScript;
- the repo's unchanged tests passing, in Docker.

The user's repo is never modified. The result is a branch in a local clone, plus a report.

Use the `migration-swarm` MCP tools. Do not migrate the files yourself when the user asked for
migration-swarm.

## Workflow

1. **Check the request fits.** Call `list_migrations`. If the request isn't one of them, say so and
   stop. The repo must be a git repo (the last commit is what gets migrated: tell the user if they
   have uncommitted changes) with a `package.json` and a test suite. Docker must be running.
2. **Pick settings with the user** if they haven't said:
   - `provider`: claude (default), gemini or openai.
   - For Claude, `auth: "auto"` uses their Claude plan login if they're signed in. That isn't
     billed but counts against their plan limits. `"api"` uses `ANTHROPIC_API_KEY`.
   - `budget_usd`: a hard stop, default 5.
   - `source_dirs`, if they named directories.
3. **Plan.** Call `start_migration(repo_path=…, review_plan=true)`. Then poll `get_status` about
   every 30 seconds until `phase` is `awaiting_approval`. The first run builds Docker images and
   installs dependencies, which can take several minutes.
4. **Show the plan and ask.** From `get_status(...).plan`, show:
   - file count and lines of code, and the import-depth groups with their files (abbreviate long lists);
   - the model for each role;
   - the budget and the `estimate_usd` range, plus `billing`;
   - the test command;
   - the output branch and its location.

   Ask the user to approve. Never approve on your own.
5. **Run.** On approval, call `approve_plan(run_id)`. If they decline, call `cancel(run_id)`.
6. **Watch.** Poll `get_status` every minute or two, and relay meaningful changes briefly: files
   done, accepted/rejected files with the reason, spend. Runs take minutes to hours. If the user
   wants to do something else, tell them the `run_id` and that they can ask for status later.
   - A `plan_wait` event means the Claude plan's usage limit was hit; the run resumes on its own
     after the reset.
   - A `warning` means the run process died. Show `log_tail` and the resume hint.
7. **Report.** Call `get_report(run_id)` and summarize:
   - files accepted, degraded (explicit `any` was allowed) and failed (left as JavaScript);
   - strict errors, and tests compared with before;
   - cost (API-equivalent if it ran on a plan);
   - where the branch is, and the `bring_into_repo` command.

   Don't overstate the result: degraded and failed files are real limitations.

To stop a run, call `cancel(run_id)`. It writes a partial report, and
`migration-swarm resume <run_id>` continues it later.
