"""Per-run context handed to pack functions. Plain data plus a few helpers; no TS assumptions."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from migration_swarm.core.budget import CostMeter


@dataclass
class RepoSpec:
    image: str                       # Docker image the repo's own install/test commands run in
    install: str
    test: str
    test_runner: str                 # jest | mocha | jasmine | ... (pack interprets)
    source_dirs: list[str]
    test_paths: list[str] = field(default_factory=list)


@dataclass
class RunContext:
    run_id: str
    migration: str
    run_dir: Path
    workdir: Path                    # local clone the run edits (never the user's repo)
    source_repo: str
    repo: RepoSpec
    cfg: dict
    meter: CostMeter
    llm: object = None               # core.llm.LLM (injected; mocked in tests)

    @property
    def scratch(self) -> Path:
        p = self.run_dir / "scratch"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def log(self, msg: str) -> None:
        line = f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with (self.run_dir / "run.log").open("a") as f:
            f.write(line + "\n")

    def git(self, *args: str, check: bool = True) -> str:
        return subprocess.run(["git", *args], cwd=self.workdir, check=check, capture_output=True, text=True).stdout.strip()

    def save_meta(self) -> None:
        meta = {"run_id": self.run_id, "migration": self.migration, "workdir": str(self.workdir),
                "source_repo": self.source_repo, "repo": asdict(self.repo)}
        (self.run_dir / "run.json").write_text(json.dumps(meta, indent=2))
