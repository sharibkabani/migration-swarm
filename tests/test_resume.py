"""Checkpoint resume: after a crash mid-run, and after a clean cancel."""

import asyncio
import json
import shutil
import subprocess

import pytest

from migration_swarm import migrations
from migration_swarm.core import config, graph, runs
from migration_swarm.core.context import RepoSpec
from migration_swarm.core.llm import LLM
from tests.test_e2e import FIXTURE, FakeChat


def _setup_run(tmp_path, monkeypatch, run_id):
    repo = tmp_path / "tinyrepo"
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns("expected"))
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    cfg = config.load(budget_usd=1.0, concurrency=4)
    cfg["js_to_ts"] = {"install_types": False}
    spec = RepoSpec("node:22-bookworm", "mkdir -p node_modules", "node --test", "node-test", ["lib"], ["test"])
    ctx = runs.create(str(repo), "js-to-ts", spec, cfg, run_id=run_id)
    FakeChat.calls = []
    ctx.llm = LLM(cfg, ctx.meter, ctx.run_id, ctx.migration, factory=lambda *a: FakeChat())
    return ctx


def _calls_for(js):
    return sum(1 for c in FakeChat.calls if f"// file: {js}\n" in c)


@pytest.mark.docker
def test_resume_after_crash_does_not_redo_finished_work(tmp_path, monkeypatch):
    ctx = _setup_run(tmp_path, monkeypatch, "crash")
    pack = migrations.load("js-to-ts")
    real_judge = pack.judge
    state = {"crashed": False}

    def crashing_judge(ctx_, setup_info, unit_files, candidates, accepted, layer):
        if layer == 1 and not state["crashed"]:
            state["crashed"] = True
            raise RuntimeError("simulated crash")
        return real_judge(ctx_, setup_info, unit_files, candidates, accepted, layer)

    monkeypatch.setattr(pack, "judge", crashing_judge)
    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(graph.execute(ctx, pack))
    calls_before = len(FakeChat.calls)

    ctx2 = runs.load("crash")
    ctx2.llm = LLM(ctx2.cfg, ctx2.meter, ctx2.run_id, ctx2.migration, factory=lambda *a: FakeChat())
    asyncio.run(graph.execute(ctx2, pack, resume=True))
    report = json.loads((ctx2.run_dir / "report.json").read_text())
    assert report["complete"] and report["result"]["files_by_status"] == {"accepted": 5}
    assert _calls_for("lib/constants.js") == 1 and _calls_for("lib/a.js") == 1   # layer 0 not redone
    assert calls_before >= 4  # layer-1 worker output survived the crash in the checkpoint
    assert report["cost"]["calls"] == len(FakeChat.calls)                      # cost carried across resume


@pytest.mark.docker
def test_resume_after_cancel_continues_to_completion(tmp_path, monkeypatch):
    ctx = _setup_run(tmp_path, monkeypatch, "cancel")
    pack = migrations.load("js-to-ts")
    original = FakeChat._answer

    def answer_then_cancel(self, prompt):
        if "// file: lib/format.js\n" in prompt:
            (ctx.run_dir / "CANCEL").write_text("x")
        return original(self, prompt)

    monkeypatch.setattr(FakeChat, "_answer", answer_then_cancel)
    asyncio.run(graph.execute(ctx, pack))
    partial = json.loads((ctx.run_dir / "report.json").read_text())
    assert partial["stop_reason"] == "cancelled" and not partial["complete"]
    assert partial["result"]["files_by_status"].get("not_reached", 0) >= 1

    monkeypatch.setattr(FakeChat, "_answer", original)
    (ctx.run_dir / "CANCEL").unlink()
    ctx2 = runs.load("cancel")
    ctx2.llm = LLM(ctx2.cfg, ctx2.meter, ctx2.run_id, ctx2.migration, factory=lambda *a: FakeChat())
    asyncio.run(graph.execute(ctx2, pack, resume=True))
    report = json.loads((ctx2.run_dir / "report.json").read_text())
    assert report["complete"] and report["result"]["files_by_status"] == {"accepted": 5}
    assert report["final"]["tsc_errors"] == 0 and report["final"]["tests_match_baseline"]


@pytest.mark.docker
def test_budget_stop_is_clean_and_resumable_with_more_budget(tmp_path, monkeypatch):
    ctx = _setup_run(tmp_path, monkeypatch, "budget")
    ctx.cfg["budget_usd"] = ctx.meter.budget_usd = 0.035  # room for ~2 worst-case Haiku calls, not the whole run
    pack = migrations.load("js-to-ts")
    asyncio.run(graph.execute(ctx, pack))
    partial = json.loads((ctx.run_dir / "report.json").read_text())
    assert partial["stop_reason"] == "budget" and not partial["complete"]
    assert partial["cost"]["spent_usd"] <= 0.035
    assert partial["final"]["tests_match_baseline"]          # partial work is still sound
    assert (ctx.run_dir / "report.md").exists()

    ctx2 = runs.load("budget")
    ctx2.cfg["budget_usd"] = ctx2.meter.budget_usd = 1.0
    ctx2.llm = LLM(ctx2.cfg, ctx2.meter, ctx2.run_id, ctx2.migration, factory=lambda *a: FakeChat())
    asyncio.run(graph.execute(ctx2, pack, resume=True))
    report = json.loads((ctx2.run_dir / "report.json").read_text())
    assert report["complete"] and report["result"]["files_by_status"] == {"accepted": 5}
