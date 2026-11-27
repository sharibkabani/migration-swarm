"""Interactive mode pieces: request routing, provider profiles, repo detection, dashboard rendering."""

import json
import time

from rich.console import Console

from migration_swarm import tui
from migration_swarm.core import config, intent
from migration_swarm.migrations import js_to_ts


def test_route_parse_supported_and_unsupported():
    it = intent.parse('noise {"migration": "js-to-ts", "summary": "Convert lib", "source_dirs": ["lib"], '
                      '"budget_usd": 2, "unsupported_reason": null, "closest": null} trailing')
    assert (it.migration, it.source_dirs, it.budget_usd) == ("js-to-ts", ["lib"], 2)
    it = intent.parse('{"migration": "python-2-to-3", "summary": "x", "unsupported_reason": "not offered", "closest": "nope"}')
    assert it.migration is None and it.closest is None and it.unsupported_reason == "not offered"


def test_provider_profiles():
    assert config.load()["models"]["worker"].startswith("anthropic:")
    g = config.load(provider="gemini")
    assert g["models"]["worker"] == "google_genai:gemini-3.8-flash"
    o = config.load(provider="openai", models={"escalation": "openai:gpt-6.2-astra"})
    assert o["models"]["worker"] == "openai:gpt-6-luna" and o["models"]["escalation"] == "openai:gpt-6.2-astra"
    for cfg in (g, o):
        for m in cfg["models"].values():
            assert m.split(":", 1)[1] in cfg["prices"] or m.endswith("astra")


def test_detect_repo_spec(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "test").mkdir()
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}, "jest": {"x": 1}}))
    spec = js_to_ts.detect_repo_spec(tmp_path)
    assert spec.test_runner == "node-test" and spec.source_dirs == ["lib"]
    assert "test/**" in spec.test_paths and "package.json#jest" in spec.test_paths


def test_dashboard_renders_a_run():
    class Ctx:
        migration = "js-to-ts"

        class meter:
            @staticmethod
            def snapshot():
                return {"spent_usd": 0.004, "budget_usd": 5.0, "tokens": {"input": 1000}}

    d = tui.Dashboard(Ctx, "claude", "plan")
    t = time.time()
    for ev in [
        {"kind": "layer", "index": 0, "count": 2, "units": 1},
        {"kind": "agent_start", "agent": "lib/a.js", "role": "worker", "unit": "lib/a.js", "files": ["lib/a.js"],
         "model": "anthropic:claude-haiku-5-5", "attempt": 0, "degraded": False},
        {"kind": "agent_progress", "agent": "lib/a.js", "chars": 900, "tail": "export const a = 1;"},
        {"kind": "agent_done", "agent": "lib/a.js", "unit": "lib/a.js", "cost_usd": 0.001},
        {"kind": "judge_start", "layer": 0, "units": ["lib/a.js"]},
        {"kind": "verdict", "unit": "lib/a.js", "files": ["lib/a.js"], "status": "accepted", "attempt": 1, "reasons": []},
        {"kind": "plan_wait", "until": t + 3600, "reason": "five_hour"},
    ]:
        d.on_event({"t": t, **ev})
    c = Console(width=100, record=True)
    c.print(d)
    text = c.export_text()
    assert "lib/a.js" in text and "accepted" in text and "usage limit" in text and "$0.004" in text
