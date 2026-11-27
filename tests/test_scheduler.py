"""Dependency-driven scheduling: a unit starts when its own imports are done, not when a whole layer is."""

import asyncio
import json
import time

import pytest

from migration_swarm import migrations
from migration_swarm.core import graph
from tests.test_e2e import FakeChat
from tests.test_resume import _setup_run


@pytest.mark.docker
def test_unit_starts_when_its_imports_are_done_not_its_layer(tmp_path, monkeypatch):
    ctx = _setup_run(tmp_path, monkeypatch, "sched")
    real = FakeChat._generate

    def slow_cycle(self, messages, stop=None, run_manager=None, **kw):
        if "// file: lib/a.js\n" in messages[-1].content:  # the a<->b cycle is slow; format.js doesn't import it
            time.sleep(8)
        return real(self, messages, stop, run_manager, **kw)

    monkeypatch.setattr(FakeChat, "_generate", slow_cycle)
    asyncio.run(graph.execute(ctx, migrations.load("js-to-ts")))
    ev = [json.loads(l) for l in (ctx.run_dir / "events.jsonl").read_text().splitlines()]
    t = lambda kind, unit: min(e["t"] for e in ev if e["kind"] == kind and e.get("unit") == unit)
    # format.js (imports constants only) starts before the slow cycle unit lib/a.js is resolved ...
    assert t("agent_start", "lib/format.js") < t("verdict", "lib/a.js")
    # ... and index.js (imports a and format) waits for both
    assert t("agent_start", "lib/index.js") > t("verdict", "lib/a.js")
    report = json.loads((ctx.run_dir / "report.json").read_text())
    assert report["complete"] and report["result"]["files_by_status"] == {"accepted": 5}
