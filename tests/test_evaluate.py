"""The uniform evaluator must reward a clean migration and catch the cheats."""

import shutil
import subprocess

import pytest

from migration_swarm.migrations.js_to_ts import evaluate as ev
from tests.test_e2e import FIXTURE

ENTRY = {"name": "tinyrepo", "image": "node:22-bookworm", "install": "mkdir -p node_modules",
         "test": "node --test", "test_runner": "node-test", "source_dirs": ["lib"], "test_paths": ["test"],
         "pre_migration_sha": "HEAD"}


def _git_repo(tmp_path):
    repo = tmp_path / "src"
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns("expected"))
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)
    return repo


def _final_tree(tmp_path, name, repo, edits):
    tree = tmp_path / name
    shutil.copytree(repo, tree)
    for js in ("constants", "format", "a", "b", "index"):
        (tree / "lib" / f"{js}.js").unlink()
        (tree / "lib" / f"{js}.ts").write_text((FIXTURE / "expected" / "lib" / f"{js}.ts").read_text())
    for rel, transform in edits.items():
        p = tree / rel
        p.write_text(transform(p.read_text()))
    return tree


@pytest.mark.docker
def test_evaluator_rewards_clean_migration_and_catches_cheats(tmp_path):
    repo = _git_repo(tmp_path)
    prep = ev.prepare(ENTRY, repo, tmp_path / "prep")
    assert prep["baseline"]["passed"] == 3

    clean = ev.evaluate(ENTRY, prep, _final_tree(tmp_path, "clean", repo, {}), tmp_path / "e-clean")
    assert clean["success"] and clean["migrated_files"] == 5 and clean["strict_errors"] == 0
    assert clean["tests_equal"] and clean["escapes"].get("explicit_any", 0) == 0
    assert clean["escapes"]["runtime_removed_tokens"] == 0

    cheat = ev.evaluate(ENTRY, prep, _final_tree(tmp_path, "cheat", repo, {
        "lib/format.ts": lambda s: s.replace("items: readonly string[]", "items: any")
                                    .replace("return items.slice(0, constants.MAX).join(s);", "return items.join(s);"),
        "test/index.test.js": lambda s: s.replace("'a, b, c'", "'a, b, c, d'"),
    }), tmp_path / "e-cheat")
    assert not cheat["success"]
    assert cheat["escapes"]["explicit_any"] == 1
    assert cheat["escapes"]["runtime_removed_tokens"] > 0                 # gutted slice(0, MAX)
    assert cheat["test_violations"] == ["test/index.test.js: modified"]

    renamed = _final_tree(tmp_path, "renamed", repo, {})
    t = renamed / "test" / "index.test.js"
    (renamed / "test" / "index.test.ts").write_text(t.read_text().replace("() => {", "(): void => {"))
    t.unlink()
    r = ev.evaluate(ENTRY, prep, renamed, tmp_path / "e-renamed")
    assert r["test_violations"] == []          # renamed test differs only by types: allowed
