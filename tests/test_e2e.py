"""End-to-end run on a 5-file JS repo (with an import cycle) with the LLM mocked.

The fake model answers each file with a hand-written TypeScript version, except that the first
attempt for lib/format.js uses `any`. The judge must reject it, the retry must be accepted, and the
run must finish with strict tsc clean, allowJs off, tests matching the baseline, and a report.
"""

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from migration_swarm import migrations
from migration_swarm.core import config, graph, runs
from migration_swarm.core.context import RepoSpec
from migration_swarm.core.llm import LLM

FIXTURE = Path(__file__).parent / "fixtures" / "tinyrepo"


class FakeChat(BaseChatModel):
    calls: list = []

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _answer(self, prompt: str) -> str:
        targets = re.findall(r"^// file: (lib/[\w.]+\.js)$", prompt, re.M)
        blocks = []
        for js in targets:
            ts = js[:-3] + ".ts"
            body = (FIXTURE / "expected" / ts).read_text()
            if js == "lib/format.js" and "REJECTED" not in prompt:
                body = body.replace("items: readonly string[]", "items: any")
            blocks.append(f"```ts\n// file: {ts}\n{body}```")
        if "lib/constants.js" in targets:
            blocks.append('```json\n{"shared_type_requests": [{"name": "Separator", "definition": "export type Separator = string;", "reason": "test"}]}\n```')
        if not targets and "shared TypeScript type file" in prompt:  # steward
            path = re.search(r"// file: (\S+)", prompt).group(1)
            blocks.append(f"```ts\n// file: {path}\nexport type Separator = string;\n```")
        return "\n".join(blocks)

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        system = messages[0].content[0]["text"] if isinstance(messages[0].content, list) else messages[0].content
        prompt = system + "\n" + messages[-1].content
        FakeChat.calls.append(prompt)
        msg = AIMessage(content=self._answer(prompt), response_metadata={"stop_reason": "end_turn"},
                        usage_metadata={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200,
                                        "input_token_details": {"cache_read": 0, "cache_creation": 0}})
        return ChatResult(generations=[ChatGeneration(message=msg)])


@pytest.mark.docker
def test_end_to_end_with_mocked_llm(tmp_path, monkeypatch):
    repo = tmp_path / "tinyrepo"
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns("expected"))
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")

    cfg = config.load(budget_usd=1.0, concurrency=4)
    cfg["js_to_ts"] = {"install_types": False}  # no network in this test
    spec = RepoSpec(image="node:22-bookworm", install="mkdir -p node_modules", test="node --test",
                    test_runner="node-test", source_dirs=["lib"], test_paths=["test"])
    ctx = runs.create(str(repo), "js-to-ts", spec, cfg, run_id="e2e")
    FakeChat.calls = []
    ctx.llm = LLM(cfg, ctx.meter, ctx.run_id, ctx.migration, factory=lambda *a: FakeChat())
    pack = migrations.load("js-to-ts")

    asyncio.run(graph.execute(ctx, pack))

    report = json.loads((ctx.run_dir / "report.json").read_text())
    by_path = {f["path"]: f for f in report["files"]}
    assert report["complete"], report["stop_reason"]
    assert report["result"]["files_by_status"] == {"accepted": 5}
    assert by_path["lib/format.js"]["attempts"] == 2          # rejected for `any`, then accepted
    assert by_path["lib/a.js"]["cyclic_unit"] and by_path["lib/a.js"]["unit"] == by_path["lib/b.js"]["unit"]
    assert all(by_path[f]["attempts"] == 1 for f in ("lib/constants.js", "lib/a.js", "lib/b.js", "lib/index.js"))
    assert report["result"]["escapes"]["explicit_any"] == 0
    fin = report["final"]
    assert fin["tsc_errors"] == 0 and fin["allow_js"] is False and fin["remaining_js"] == []
    assert fin["tests_match_baseline"] and fin["tests"]["passed"] == 3
    assert fin["protected_changed"] == []
    # 5 units' first attempts... (4 units: cycle is one) + 1 retry + 1 steward call, all metered
    assert report["cost"]["calls"] == len(FakeChat.calls) == 6
    assert report["cost"]["spent_usd"] > 0
    # the retry prompt carried the exact rejection reason
    retry = [c for c in FakeChat.calls if "REJECTED" in c][0]
    assert "explicit_any = 1 (allowed 0)" in retry
    # the work is on a branch in the run's clone; the source repo is untouched
    work = ctx.workdir
    assert (work / "lib" / "format.ts").exists() and not (work / "lib" / "format.js").exists()
    assert (work / "lib" / "__msw_shared_types__.ts").read_text().strip() == "export type Separator = string;"
    assert (repo / "lib" / "format.js").exists() and not (repo / "lib" / "format.ts").exists()
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=work, capture_output=True, text=True).stdout.strip()
    assert branch == "migration-swarm/e2e"
    assert (ctx.run_dir / "report.md").exists()
