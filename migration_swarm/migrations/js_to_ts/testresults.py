"""Parse JS test-runner console output into counts and failing test names (best effort)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class TestResult:
    ok: bool
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    failing: list[str] = field(default_factory=list)
    suite_errors: int = 0         # suites that failed to load (jest)
    seconds: float = 0.0
    tail: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    def no_worse_than(self, base: "TestResult") -> bool:
        """Equal-or-better than the baseline: same outcome, no lost passes, no new failures."""
        if base.ok and not self.ok:
            return False
        if self.passed < base.passed or self.failed > base.failed or self.suite_errors > base.suite_errors:
            return False
        return set(self.failing) <= set(base.failing)


def parse(runner: str, output: str, exit_ok: bool, seconds: float = 0.0) -> TestResult:
    out = ANSI.sub("", output)
    r = TestResult(ok=exit_ok, seconds=round(seconds, 1), tail=out[-3000:])
    if runner == "jest":
        m = re.findall(r"^Tests:\s+(.*)$", out, re.M)
        if m:
            for k in ("failed", "skipped", "passed"):
                mm = re.search(rf"(\d+) {k}", m[-1])
                if mm:
                    setattr(r, k, int(mm.group(1)))
        names = re.findall(r"^\s+● (.+?)\s*$", out, re.M)
        r.suite_errors = sum(1 for n in names if n.startswith("Test suite failed to run"))
        r.failing = sorted({n for n in names if not n.startswith("Test suite failed to run")})
    elif runner == "mocha":
        for k, pat in (("passed", r"(\d+) passing"), ("failed", r"(\d+) failing"), ("skipped", r"(\d+) pending")):
            m = re.findall(pat, out)
            if m:
                setattr(r, k, int(m[-1]))
        block = out.split(f"{r.failed} failing", 1)[1] if r.failed else ""
        r.failing = sorted(set(re.findall(r"^\s+\d+\) (.+?):?\s*$", block, re.M)))
    elif runner == "jasmine":
        m = re.findall(r"(\d+) specs?, (\d+) failures?(?:, (\d+) pending)?", out)
        if m:
            total, failed, pending = (int(x or 0) for x in m[-1])
            r.failed, r.skipped, r.passed = failed, pending, total - failed - pending
        block = out.split("Failures:", 1)[1] if "Failures:" in out else ""
        r.failing = sorted(set(re.findall(r"^\d+\) (.+?)\s*$", block, re.M)))
    elif runner == "node-test":
        for k, key in (("passed", "pass"), ("failed", "fail"), ("skipped", "skipped")):
            m = re.findall(rf"^[#ℹ] {key} (\d+)$", out, re.M)
            if m:
                setattr(r, k, int(m[-1]))
        r.failing = sorted(set(re.findall(r"^\s*not ok \d+ - (.+)$", out, re.M)))
    else:
        r.passed = 1 if exit_ok else 0
        r.failed = 0 if exit_ok else 1
    return r
