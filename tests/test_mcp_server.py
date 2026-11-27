"""MCP tools without an LLM: catalog, input validation, status of runs in various states."""

import json

import pytest

from migration_swarm import mcp_server
from migration_swarm.core import runs


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "LOGS", tmp_path / ".mcp-logs")
    return tmp_path


def test_list_migrations():
    assert [m["name"] for m in mcp_server.list_migrations()] == ["js-to-ts"]


def test_start_migration_validates(tmp_path, runs_dir):
    assert "not a git repository" in mcp_server.start_migration(str(tmp_path))["error"]
    (tmp_path / ".git").mkdir()
    assert "unknown migration" in mcp_server.start_migration(str(tmp_path), migration="cobol-to-go")["error"]
    assert "unknown provider" in mcp_server.start_migration(str(tmp_path), provider="llama")["error"]


def test_status_of_awaiting_run_includes_plan_and_cancel_declines(runs_dir):
    d = runs_dir / "r1"
    d.mkdir()
    (d / "status.json").write_text(json.dumps({"phase": "awaiting_approval", "cost": {"spent_usd": 0, "budget_usd": 5}}))
    (d / "plan_summary.json").write_text(json.dumps({"files": 3, "layers": []}))
    (d / "events.jsonl").write_text(json.dumps({"t": 1, "kind": "verdict", "status": "rejected", "files": ["a.js"],
                                                "reasons": ["TS2345: nope"]}) + "\n")
    st = mcp_server.get_status("r1")
    assert st["phase"] == "awaiting_approval" and st["plan"]["files"] == 3 and "warning" not in st
    assert st["recent"] == ["rejected: a.js (TS2345: nope)"]
    assert "no report yet" in mcp_server.get_report("r1")["error"]
    with pytest.raises(ValueError):
        mcp_server.get_status("../etc")


def test_dead_unfinished_run_warns(runs_dir):
    d = runs_dir / "r2"
    d.mkdir()
    (d / "status.json").write_text(json.dumps({"phase": "migrate"}))
    st = mcp_server.get_status("r2")
    assert st["process_running"] is False and "warning" in st and "resume r2" in st["hint"]
