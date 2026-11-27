---
name: parallel-ts-migration
description: Migrate many JavaScript files to strict TypeScript quickly by splitting the work across parallel subagents (one owner per file, git worktrees), layer by layer through the import graph, verifying each layer with the repo's checker.
---

# Parallel JS -> strict TS migration

You coordinate; `ts-file-migrator` subagents do the per-file conversion in parallel.

## 1. Plan (you, once)

1. Read `/opt/msw-run/files.txt` (the files to migrate) and the rules in your task.
2. Build the import graph of those files: for each file, find its local `require(...)` / `import ... from` targets.
3. Group files into layers, leaves first: layer 0 = files that import no other listed file; layer k = files whose
   imports are all in earlier layers. Files that import each other (a cycle) form one group owned by one subagent.
4. Note the modules imported by many files (utils, constants, shared option objects): their types matter most.
   Migrate them carefully (yourself, or one dedicated subagent) in their layer.

## 2. Execute, one layer at a time

For each layer:
1. Split the layer into groups of 1-4 files (cycle groups stay together). Each file has exactly one owner.
2. Launch one `ts-file-migrator` subagent per group, all at once (parallel), each in its own git worktree
   (worktree isolation on the Agent tool) if available; otherwise in /work directly (file ownership already
   prevents conflicts). Give each subagent: its file paths, the task rules, and the paths of the already-migrated
   .ts files it imports (it should read their types).
   In a worktree, `ln -s /work/node_modules node_modules` first so tsc and imports resolve.
3. When the layer's subagents finish: bring their files into /work (merge their branches; files are disjoint so
   there are no conflicts), then run `python3 /opt/msw-run/check.py`.
4. For every problem it reports in this layer's files (type errors, escape budgets, runtime changes, failing
   tests), re-dispatch that file's owner with the exact messages, or fix it yourself if it is small. Do not move on
   while this layer has problems you can fix.

## 3. Finish

Run `python3 /opt/msw-run/check.py` on the whole repo, fix what remains, and stop when it reports no problems or you
cannot make further progress. Never edit test files to make checks pass.
