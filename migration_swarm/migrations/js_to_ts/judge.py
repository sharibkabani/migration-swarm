"""judge (js-to-ts): deterministic accept/reject for one round of candidate units. No LLM.

Per round:
 1. write candidate .ts files (original .js removed) for every candidate unit;
 2. one tsc program pass (judge.mjs): errors attributed by file, escape counts, emitted JS;
 3. runtime drift: emitted JS of each candidate vs emitted JS of the original;
 4. tests on the shadow build (emitted JS at original paths) for units that passed 2-3;
    if the suite regresses, adaptive group testing (bisection) isolates the culprit units;
 5. protected (test) files must be byte-identical to the setup snapshot;
 6. rejected units are reverted to their original .js; accepted ones are committed.
"""

from __future__ import annotations

import collections
import difflib
import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from migration_swarm.core import integrity
from migration_swarm.core.context import RunContext
from migration_swarm.migrations.js_to_ts import setup_step as setup_mod
from migration_swarm.migrations.js_to_ts.prompts import ts_name
from migration_swarm.migrations.js_to_ts.testresults import TestResult

@dataclass
class Verdict:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    escapes: dict = field(default_factory=dict)
    escape_sites: list = field(default_factory=list)
    tests: str = ""

    def to_json(self) -> dict:
        return asdict(self)


def drift(original_tokens: str, candidate_tokens: str) -> tuple[int, int, list[str], list[str]]:
    """Token-level diff of canonical emitted JS (see judge.mjs): (removed, added, removed_examples, added_examples)."""
    a = [t for t in original_tokens.splitlines() if t]
    b = [t for t in candidate_tokens.splitlines() if t]
    removed = added = 0
    rex: list[str] = []
    aex: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        removed += i2 - i1
        added += j2 - j1
        ctx_before = " ".join(a[max(i1 - 4, 0):i1])
        if i2 > i1 and len(rex) < 6:
            rex.append(f"...{ctx_before} [{' '.join(a[i1:i2])[:200]}]")
        if j2 > j1 and len(aex) < 6:
            aex.append(f"...{ctx_before} [{' '.join(b[j1:j2])[:200]}]")
    return removed, added, rex, aex


def decide(*, unit_files: list[str], diagnostics: list[dict], escapes: dict[str, dict], drifts: dict[str, tuple],
           budgets: dict[str, float], lifted: set[str], sources: dict[str, str]) -> Verdict:
    """Pure accept/reject for one unit before tests. unit_files are .ts paths."""
    reasons: list[str] = []
    totals: collections.Counter = collections.Counter()
    sites: list = []
    for f in unit_files:
        errs = [d for d in diagnostics if d["file"] == f]
        lines = sources.get(f, "").splitlines()
        for d in errs[:25]:
            src = lines[d["line"] - 1].strip() if 0 < d["line"] <= len(lines) else ""
            reasons.append(f"{f}:{d['line']}:{d['col']} TS{d['code']}: {d['message']}" + (f"  | `{src[:160]}`" if src else ""))
        if len(errs) > 25:
            reasons.append(f"{f}: ... and {len(errs) - 25} more type errors")
        e = escapes.get(f, {})
        if e.get("missing"):
            reasons.append(f"{f}: not part of the TypeScript program")
            continue
        totals.update(e.get("counts", {}))
        sites += [{**s, "file": f} for s in e.get("sites", [])]
        if f in drifts:
            removed, added, rex, aex = drifts[f]
            totals["runtime_removed_tokens"] += removed
            totals["runtime_added_tokens"] += added
            if removed > budgets.get("runtime_removed_tokens", 0):
                reasons.append(f"{f}: runtime code changed ({removed} tokens of the original's compiled JS are missing or "
                               f"altered; only types may be added). Original tokens [in brackets] not in yours: " + " || ".join(rex)
                               + (" -- yours has instead: " + " || ".join(aex) if aex else ""))
            elif added > budgets.get("runtime_added_tokens", 0):
                reasons.append(f"{f}: {added} runtime tokens added that the original did not have: " + " || ".join(aex))
    for kind, n in sorted(totals.items()):
        if kind.startswith("runtime_") or kind in lifted:
            continue
        if n > budgets.get(kind, 0):
            where = ", ".join([f"{s['file']}:{s['line']}" for s in sites if s["kind"] == kind][:10])
            reasons.append(f"escape budget exceeded: {kind} = {n} (allowed {budgets.get(kind, 0)}) at {where}")
    return Verdict(accepted=not reasons, reasons=reasons, escapes=dict(totals), escape_sites=sites)


def _original_source(ctx: RunContext, commit: str, js: str) -> Path:
    dest = ctx.run_dir / "orig_src" / js
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(ctx.git("show", f"{commit}:{js}") + "\n")
    return dest


def revert_unit(ctx: RunContext, commit: str, js_files: list[str]) -> None:
    for js in js_files:
        t = ctx.workdir / ts_name(js)
        if t.exists():
            t.unlink()
        ctx.git("checkout", commit, "--", js)


def write_unit(ctx: RunContext, files: dict[str, str]) -> None:
    for ts_path, content in files.items():
        js = re.sub(r"\.(c|m)?ts$", lambda m: f".{m.group(1) or ''}js", ts_path)
        p = ctx.workdir / js
        if p.exists():
            p.unlink()
        (ctx.workdir / ts_path).write_text(content)


def judge_round(ctx: RunContext, *, setup_info: dict, unit_files: dict[str, list[str]],
                candidates: dict[str, dict], accepted_units: list[str], layer: int) -> dict[str, Verdict]:
    """candidates: uid -> {"files": {ts_path: content}, "degraded": bool}. unit_files: uid -> original .js paths."""
    commit = setup_info["setup_commit"]
    cfg_budgets = ctx.cfg["escape_budgets"]
    lifted_degraded = set(ctx.cfg["degraded_lifts"])
    baseline = TestResult(**{k: v for k, v in setup_info["baseline"].items()})
    verdicts: dict[str, Verdict] = {}

    for uid in candidates:
        revert_unit(ctx, commit, unit_files[uid])
    for uid, cand in candidates.items():
        write_unit(ctx, cand["files"])

    ts_files = [ts_name(f) for uid in candidates for f in unit_files[uid]]
    js_files = [f for uid in candidates for f in unit_files[uid]]
    ctx.emit("judge_step", step="typecheck", detail=f"strict tsc + escape scan on {len(ts_files)} files")
    res = setup_mod.judge_request(ctx, setup_info["tsconfig"], "emit", emit=True, escape_files=ts_files,
                                  normalize=js_files, norm_dir="cand_norm")
    diags = res["diagnostics"]
    fatal = [d for d in diags if d["file"] is None]
    if fatal:
        raise RuntimeError("tsc configuration errors: " + "; ".join(d["message"] for d in fatal[:5]))
    cand_set = set(ts_files)
    collateral = [d for d in diags if d["file"] not in cand_set and d["file"].endswith((".ts", ".mts", ".cts"))]
    if collateral:
        ctx.log(f"judge: {len(collateral)} type errors outside this round's files, e.g. {collateral[0]}")

    for uid, cand in candidates.items():
        files_ts = [ts_name(f) for f in unit_files[uid]]
        drifts = {}
        for js in unit_files[uid]:
            o, c = ctx.run_dir / "orig_norm" / js, ctx.run_dir / "cand_norm" / js
            if o.exists() and c.exists():
                drifts[ts_name(js)] = drift(o.read_text(), c.read_text())
        verdicts[uid] = decide(unit_files=files_ts, diagnostics=diags, escapes=res["escapes"], drifts=drifts,
                               budgets=cfg_budgets, lifted=lifted_degraded if cand.get("degraded") else set(),
                               sources=cand["files"])

    # Statically rejected units go back to their original .js now, so the test tree is consistent.
    for uid, v in verdicts.items():
        if not v.accepted:
            revert_unit(ctx, commit, unit_files[uid])

    # Tests on the shadow build, for units that passed the static checks.
    testable = [uid for uid, v in verdicts.items() if v.accepted]
    emit_dir = ctx.run_dir / "emit"
    budget_runs = int(ctx.cfg["judge"]["bisect_max_test_runs"])
    runs = 0

    def run_with(included: list[str]) -> TestResult:
        nonlocal runs
        runs += 1
        files: dict[str, Path] = {}
        for uid in accepted_units:
            for js in unit_files[uid]:
                files[js] = emit_dir / js
        for uid in testable:
            for js in unit_files[uid]:
                files[js] = emit_dir / js if uid in included else _original_source(ctx, commit, js)
        ctx.emit("judge_step", step="tests", detail=f"test run {runs}: {len(included)} of {len(testable)} candidate units")
        r = setup_mod.run_tests(ctx, setup_mod.build_overlay(ctx, files))
        ctx.emit("judge_tests", units=len(included), ok=r.ok, passed=r.passed, failed=r.failed)
        return r

    if testable:
        good: list[str] = []
        bad: list[str] = []
        last: dict[str, TestResult] = {}

        def search(group: list[str]) -> None:
            if runs >= budget_runs:
                for uid in group:
                    last.setdefault(uid, TestResult(ok=False, tail="bisection budget exhausted"))
                bad.extend(group)
                return
            r = run_with(good + group)
            if r.no_worse_than(baseline):
                good.extend(group)
                return
            if len(group) == 1:
                last[group[0]] = r
                bad.extend(group)
                return
            mid = len(group) // 2
            search(group[:mid])
            search(group[mid:])

        search(testable)
        for uid in bad:
            r = last.get(uid)
            detail = (f"passed {r.passed} (baseline {baseline.passed}), failed {r.failed}; failing: "
                      + "; ".join(r.failing[:8])) if r and r.tail != "bisection budget exhausted" else "bisection budget exhausted"
            verdicts[uid].accepted = False
            verdicts[uid].reasons.append(f"tests regressed with this unit: {detail}")
            if r and not r.failing and r.tail:
                verdicts[uid].reasons.append("test output tail: " + r.tail[-1200:].replace("\n", " | "))
        for uid in good:
            verdicts[uid].tests = "ok"
        ctx.log(f"judge: layer {layer}: {len(good)} units passed tests, {len(bad)} failed, {runs} test runs")

    changed = integrity.verify(ctx.workdir, setup_info["protected"])
    if changed:
        for key in changed:
            if "#" not in key:
                ctx.git("checkout", commit, "--", key, check=False)
        for v in verdicts.values():
            v.accepted = False
            v.reasons.append(f"protected test files were modified: {changed[:5]}")

    for uid, v in verdicts.items():
        if not v.accepted and any((ctx.workdir / ts_name(f)).exists() for f in unit_files[uid]):
            revert_unit(ctx, commit, unit_files[uid])
    accepted_now = [uid for uid, v in verdicts.items() if v.accepted]
    if accepted_now:
        ctx.commit(f"migration-swarm: layer {layer}: accept {len(accepted_now)} units", "\n".join(sorted(accepted_now)),
                   paths=("-A", *ctx.repo.source_dirs))
    (ctx.run_dir / "judge").mkdir(exist_ok=True)
    (ctx.run_dir / "judge" / f"layer{layer}-{len(list((ctx.run_dir / 'judge').glob(f'layer{layer}-*')))}.json").write_text(
        json.dumps({uid: v.to_json() for uid, v in verdicts.items()}, indent=1))
    return verdicts


def deps_declarations(ctx: RunContext, deps: list[str], limit_chars: int = 60000) -> dict[str, str]:
    """Current .d.ts of each dependency (from the latest emit; accurate for migrated deps)."""
    out: dict[str, str] = {}
    used = 0
    for js in deps:
        d = ctx.run_dir / "emit" / re.sub(r"\.(c|m)?js$", lambda m: f".d.{m.group(1) or ''}ts", js)
        if not d.exists():
            d = ctx.run_dir / "orig_emit" / re.sub(r"\.(c|m)?js$", lambda m: f".d.{m.group(1) or ''}ts", js)
        if not d.exists():
            continue
        text = d.read_text()
        if used + len(text) > limit_chars:
            text = text[: max(limit_chars - used, 0)] + "\n// ... (truncated)"
        out[js] = text
        used += len(text)
        if used >= limit_chars:
            break
    return out


def judge_steward(ctx: RunContext, setup_info: dict, content: str) -> list[str]:
    """Write the shared types file and check it. Returns rejection reasons (empty = accepted)."""
    path = setup_info["shared_types_path"]
    p = ctx.workdir / path
    previous = p.read_text() if p.exists() else None
    p.write_text(content)
    res = setup_mod.judge_request(ctx, setup_info["tsconfig"], "emit", emit=True, escape_files=[path],
                                  normalize=[re.sub(r"\.ts$", ".js", path)], norm_dir="cand_norm")
    reasons = [f"{d['file']}:{d['line']} TS{d['code']}: {d['message']}" for d in res["diagnostics"] if d["file"]][:20]
    counts = res["escapes"].get(path, {}).get("counts", {})
    for k in ("explicit_any", "ts_ignore", "ts_expect_error", "ts_nocheck", "as_unknown_as"):
        if counts.get(k):
            reasons.append(f"{k} is not allowed in the shared types file")
    norm = ctx.run_dir / "cand_norm" / re.sub(r"\.ts$", ".js", path)
    if norm.exists() and norm.read_text().strip():
        reasons.append("the shared types file must not contain runtime code (its emitted JS is not empty)")
    if reasons:
        if previous is None:
            p.unlink()
        else:
            p.write_text(previous)
    else:
        ctx.commit("migration-swarm: steward updates shared types", paths=(path,))
    return reasons


def clear_dir(p: Path) -> None:
    if p.exists():
        shutil.rmtree(p)
