"""Turn a free-text migration request into one of the supported migrations (or a clear "not supported").

The LLM only routes and extracts options; everything after that is the pack's deterministic pipeline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from migration_swarm import migrations

SYSTEM = """You route a user's request to one of the migrations a code-migration tool supports. You never invent
capabilities: if the request is not one of the supported migrations (or clearly a subset of one), say so.

Reply with ONLY one JSON object, no prose:
{
  "migration": "<supported migration name, or null>",
  "summary": "<one sentence restating what will be done, in the user's terms>",
  "source_dirs": ["<repo-relative dirs to migrate, only if the user named them or the repo listing makes it obvious>"] or null,
  "budget_usd": <number if the user named a budget, else null>,
  "unsupported_reason": "<if migration is null: why, in one sentence>" or null,
  "closest": "<closest supported migration name if migration is null and one is related, else null>"
}"""


@dataclass
class Intent:
    migration: str | None
    summary: str
    source_dirs: list[str] | None = None
    budget_usd: float | None = None
    unsupported_reason: str | None = None
    closest: str | None = None
    raw: dict = field(default_factory=dict)


def catalog() -> str:
    lines = []
    for name in migrations.MIGRATIONS:
        pack = migrations.load(name)
        lines.append(f"- {name}: {pack.DESCRIPTION}" + (f"\n  {pack.INTENT_HINT}" if getattr(pack, "INTENT_HINT", None) else ""))
    return "\n".join(lines)


def repo_listing(repo: Path, limit: int = 60) -> str:
    entries = []
    for p in sorted(repo.iterdir()):
        if p.name.startswith(".") or p.name in ("node_modules", "dist", "build"):
            continue
        entries.append(p.name + ("/" if p.is_dir() else ""))
    out = "Top level: " + ", ".join(entries[:limit])
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            d = json.loads(pkg.read_text())
            out += f"\npackage.json: type={d.get('type', 'commonjs')}, scripts={list((d.get('scripts') or {}))[:12]}"
        except ValueError:
            pass
    return out


def parse(text: str) -> Intent:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"router reply had no JSON: {text[:200]}")
    d = json.loads(m.group(0))
    name = d.get("migration")
    if name not in migrations.MIGRATIONS:
        name = None
    return Intent(migration=name, summary=d.get("summary") or "", source_dirs=d.get("source_dirs") or None,
                  budget_usd=d.get("budget_usd"), unsupported_reason=d.get("unsupported_reason"),
                  closest=d.get("closest") if d.get("closest") in migrations.MIGRATIONS else None, raw=d)


async def route(llm, request: str, repo: Path) -> Intent:
    user = f"Supported migrations:\n{catalog()}\n\nRepository:\n{repo_listing(repo)}\n\nUser request:\n{request.strip()}"
    r = await llm.complete("planner", SYSTEM, user, key="__intent__", expected_output_chars=600)
    return parse(r.text)
