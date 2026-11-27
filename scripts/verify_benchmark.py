"""Verify a benchmark manifest: every repo/SHA exists, and its tests pass at the pinned SHA in Docker.

Usage: python scripts/verify_benchmark.py benchmarks/js_to_ts/manifest.yaml [--only name,...] [--runs 2]

Per repo: clone -> fresh checkout of pre_migration_sha (and check ground_truth_sha exists)
-> install inside the sandbox WITH network -> run the test command N times WITHOUT network.
Raw logs go to .bench_cache/logs/ (gitignored); a summary is written next to the manifest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from migration_swarm.core import sandbox  # noqa: E402

CACHE = ROOT / ".bench_cache"


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def summarize(runner: str, out: str) -> dict:
    """Pull pass/fail counts out of the runner's console summary (best effort, for the record)."""
    s: dict = {}
    if runner == "jest":
        m = re.findall(r"^Tests:\s+(.*)$", out, re.M)
        if m:
            line = m[-1]
            for k in ("failed", "skipped", "todo", "passed", "total"):
                mm = re.search(rf"(\d+) {k}", line)
                if mm:
                    s[k] = int(mm.group(1))
    elif runner == "mocha":
        for k, pat in (("passed", r"(\d+) passing"), ("failed", r"(\d+) failing"), ("skipped", r"(\d+) pending")):
            m = re.findall(pat, out)
            if m:
                s[k] = int(m[-1])
    elif runner == "jasmine":
        m = re.findall(r"(\d+) specs?, (\d+) failures?(?:, (\d+) pending)?", out)
        if m:
            total, failed, pending = m[-1]
            s = {"total": int(total), "failed": int(failed), "skipped": int(pending or 0)}
            s["passed"] = s["total"] - s["failed"] - s["skipped"]
    return s


def verify(repo: dict, runs: int, logdir: Path) -> dict:
    name = repo["name"]
    rec: dict = {"name": name, "url": repo["url"], "pre_migration_sha": repo["pre_migration_sha"]}
    clone = CACHE / "clones" / name
    if not clone.exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "-q", "--no-checkout", repo["url"], str(clone))
    for key in ("pre_migration_sha", "ground_truth_sha"):
        sha = repo.get(key)
        if sha:
            rec[f"{key}_exists"] = git("cat-file", "-t", sha, cwd=clone) == "commit"
            rec[f"{key}_date"] = git("log", "-1", "--format=%cs", sha, cwd=clone)

    work = CACHE / "work" / name
    if work.exists():
        shutil.rmtree(work)
    git("clone", "-q", "--no-hardlinks", "--no-checkout", str(clone), str(work))
    git("checkout", "-q", "--detach", repo["pre_migration_sha"], cwd=work)

    inst = sandbox.run(work, repo["image"], repo["install"], network=True, timeout_s=2400)
    (logdir / f"{name}.install.log").write_text(inst.output)
    rec["install"] = {"ok": inst.ok, "exit_code": inst.exit_code, "seconds": round(inst.duration_s, 1)}
    rec["test_runs"] = []
    if inst.ok:
        for i in range(runs):
            t = sandbox.run(work, repo["image"], repo["test"], network=False, timeout_s=3600, ephemeral=True)
            (logdir / f"{name}.test{i + 1}.log").write_text(t.output)
            rec["test_runs"].append({
                "ok": t.ok, "exit_code": t.exit_code, "timed_out": t.timed_out,
                "seconds": round(t.duration_s, 1), "counts": summarize(repo["test_runner"], t.output),
            })
    runs_ok = bool(rec["test_runs"]) and all(r["ok"] for r in rec["test_runs"])
    counts = [json.dumps(r["counts"], sort_keys=True) for r in rec["test_runs"]]
    rec["stable"] = runs_ok and len(set(counts)) == 1
    rec["verdict"] = "PASS" if rec["stable"] else "FAIL"
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--only", default="")
    ap.add_argument("--runs", type=int, default=2)
    a = ap.parse_args()

    manifest_path = Path(a.manifest)
    manifest = yaml.safe_load(manifest_path.read_text())
    only = {x for x in a.only.split(",") if x}
    logdir = CACHE / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    sandbox.ensure_cache_volume()

    out_path = manifest_path.parent / "verification.json"
    results = json.loads(out_path.read_text())["repos"] if out_path.exists() else {}
    for repo in manifest["repos"]:
        if only and repo["name"] not in only:
            continue
        print(f"--> {repo['name']}", flush=True)
        rec = verify(repo, a.runs, logdir)
        rec["verified_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        results[repo["name"]] = rec
        runs = ", ".join(f"{r['exit_code']}/{r['seconds']}s {r['counts']}" for r in rec["test_runs"])
        print(f"    install ok={rec['install']['ok']} ({rec['install']['seconds']}s)  tests: {runs}  => {rec['verdict']}", flush=True)
        out_path.write_text(json.dumps({"manifest": str(manifest_path), "repos": results}, indent=2) + "\n")


if __name__ == "__main__":
    main()
