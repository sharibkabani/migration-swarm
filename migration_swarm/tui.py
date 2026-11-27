"""Interactive mode: `migration-swarm` with no arguments.

  1. pick a provider (Claude / Gemini / OpenAI) and how to authenticate
  2. point at a repo and describe the migration in your own words
  3. review the plan (what will change, models, budget, how it is judged) and approve it
  4. watch the agents work live; get a summary, a report and a branch at the end
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import questionary
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from migration_swarm import migrations
from migration_swarm.core import config as config_mod, graph, intent as intent_mod, plansummary, runs
from migration_swarm.core.budget import CostMeter
from migration_swarm.core.llm import LLM
from migration_swarm.core.providers import PROVIDERS

console = Console(highlight=False)
ACCENT = "#d97757"
DIM = "grey50"
OK, WARN, BAD = "green3", "gold3", "red3"
QSTYLE = questionary.Style([
    ("qmark", f"fg:{ACCENT} bold"), ("question", "bold"), ("answer", f"fg:{ACCENT}"),
    ("pointer", f"fg:{ACCENT} bold"), ("highlighted", f"fg:{ACCENT}"), ("selected", f"fg:{ACCENT}"),
    ("instruction", "fg:#808080"), ("text", ""),
])


class Abort(Exception):
    pass


def _ask(q):
    ans = q.ask()
    if ans is None:  # Ctrl-C
        raise Abort()
    return ans


async def _ask_async(q):
    ans = await q.ask_async()
    if ans is None:
        raise Abort()
    return ans


def money(v: float) -> str:
    return f"${v:.2f}" if v >= 1 or v == 0 else f"${v:.3f}"


def short_model(model: str) -> str:
    return model.split(":", 1)[-1].replace("claude-", "").replace("-5-5", " 5.5")


# ---------------------------------------------------------------------------------------------
# setup questions


def banner() -> None:
    console.print()
    console.print(Text.assemble(("  migration-swarm", f"bold {ACCENT}"),
                                ("  agents migrate your code; a deterministic judge checks every file", DIM)))
    console.print()


def check_docker() -> bool:
    if not shutil.which("docker"):
        console.print(f"[{BAD}]Docker is required[/] (installs and tests run in containers). Install Docker Desktop and retry.")
        return False
    r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True)
    if r.returncode != 0:
        console.print(f"[{BAD}]Docker isn't running.[/] Start Docker Desktop and retry.")
        return False
    return True


def claude_plan_status() -> dict | None:
    try:
        from claude_agent_sdk import _bundled  # noqa: F401
        cli = Path(__import__("claude_agent_sdk").__file__).parent / "_bundled" / "claude"
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        r = subprocess.run([str(cli), "auth", "status", "--json"], capture_output=True, text=True, timeout=20, env=env)
        d = json.loads(r.stdout)
        return d if d.get("loggedIn") else None
    except Exception:
        return None


def save_key(var: str, value: str) -> Path:
    path = runs.HOME_DIR / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [ln for ln in (path.read_text().splitlines() if path.exists() else []) if not ln.startswith(f"{var}=")]
    path.write_text("\n".join([*lines, f"{var}={value}"]) + "\n")
    path.chmod(0o600)
    return path


def choose_provider() -> tuple[str, str]:
    """-> (provider, llm_backend)"""
    choices = [questionary.Choice(title=[("", f"{p.label:<20}"), ("class:instruction", p.note)], value=name)
               for name, p in PROVIDERS.items()]
    provider = _ask(questionary.select("Which model provider?", choices=choices, style=QSTYLE, qmark="›"))
    p = PROVIDERS[provider]
    if provider == "claude":
        status = claude_plan_status()
        opts = []
        if status:
            plan = (status.get("subscriptionType") or "claude").title()
            opts.append(questionary.Choice(f"Claude {plan} plan  (signed in; uses your plan's limits, no API bill)", "plan"))
        opts.append(questionary.Choice("API key" + ("  (found ANTHROPIC_API_KEY)" if p.key() else ""), "api"))
        if not status:
            opts.append(questionary.Choice("Claude plan (not signed in: run `claude auth login` first)", "plan-missing",
                                           disabled="sign in first"))
        backend = _ask(questionary.select("How should it authenticate?", choices=opts, style=QSTYLE, qmark="›"))
        if backend == "plan":
            return provider, "plan"
    if not p.key():
        console.print(f"  [{DIM}]No {p.env_keys[0]} found. Get one at {p.key_url}[/]")
        key = _ask(questionary.password(f"{p.env_keys[0]}:", style=QSTYLE, qmark="›"))
        if not key.strip():
            raise Abort()
        os.environ[p.env_keys[0]] = key.strip()
        if _ask(questionary.confirm(f"Save it to {runs.HOME_DIR / '.env'} (readable only by you)?", default=True,
                                    style=QSTYLE, qmark="›")):
            console.print(f"  [{DIM}]saved to {save_key(p.env_keys[0], key.strip())}[/]")
    return provider, "api"


def choose_repo() -> Path:
    while True:
        raw = _ask(questionary.path("Repository to migrate:", default=str(Path.cwd()), only_directories=True,
                                    style=QSTYLE, qmark="›"))
        repo = Path(raw).expanduser().resolve()
        if not (repo / ".git").exists():
            console.print(f"  [{WARN}]{repo} isn't a git repository.[/] migration-swarm works on a clone of your "
                          "last commit, so commit your code first.")
            continue
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout.strip()
        if dirty:
            console.print(f"  [{WARN}]Uncommitted changes won't be included[/] [{DIM}](the run clones your last commit).[/]")
        return repo


# ---------------------------------------------------------------------------------------------
# plan screen


def plan_view(it: intent_mod.Intent, state: dict, cfg: dict, ctx, repo: Path) -> Group:
    a = state["analysis"]
    layers = state["layers"]
    unit_files = state["unit_files"]
    head = Text.assemble(("Plan", f"bold {ACCENT}"), ("  ·  ", DIM), (it.summary or ctx.migration, "bold"))
    facts = Table.grid(padding=(0, 2))
    facts.add_column(style=DIM, justify="right")
    facts.add_column()
    facts.add_row("repo", f"{repo}")
    facts.add_row("migration", f"{ctx.migration}  [{DIM}]{migrations.load(ctx.migration).DESCRIPTION}[/]")
    facts.add_row("scope", f"{len(a['files'])} files · {a['total_loc']:,} lines in {', '.join(ctx.repo.source_dirs)}"
                  + (f" · {len(a['cycles'])} import cycle{'s' if len(a['cycles']) != 1 else ''} (each handled as one unit)"
                     if a["cycles"] else ""))
    facts.add_row("order", f"{len(layers)} layers through the import graph, leaves first; up to {cfg['concurrency']} agents at once")

    lt = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style=DIM)
    lt.add_column("layer", justify="right", style=DIM)
    lt.add_column("units", justify="right")
    lt.add_column("files")
    for k, layer in enumerate(layers):
        files = [f for uid in layer for f in unit_files[uid]]
        shown = ", ".join(files[:4]) + (f" [{DIM}]+{len(files) - 4} more[/]" if len(files) > 4 else "")
        lt.add_row(str(k + 1), str(len(layer)), shown)
        if k == 7 and len(layers) > 9:
            lt.add_row("…", "", f"[{DIM}]{len(layers) - 8} more layers[/]")
            break

    m = cfg["models"]
    n = cfg["retries_before_escalation"]
    agents = Table.grid(padding=(0, 2))
    agents.add_column(style=DIM, justify="right")
    agents.add_column()
    agents.add_row("workers", f"{short_model(m['worker'])}: {n + 1} tries per file with the judge's exact errors")
    agents.add_row("escalation", f"{short_model(m['escalation'])}: 1 strict try, then 1 'degraded' try (explicit any allowed)")
    agents.add_row("steward", f"{short_model(m['steward'])}: owns the shared types file other agents request types in")
    low, high = plansummary.estimate_cost(cfg, a["files"], ctx.workdir)
    billing = ("API-equivalent; not billed, uses your Claude plan's limits" if cfg.get("llm_backend") == "plan"
               else "billed to your API key")
    agents.add_row("budget", f"hard stop at ${cfg['budget_usd']:.2f}  [{DIM}]estimate ${low:.2f}–${high:.2f} ({billing})[/]")

    judge = Table.grid(padding=(0, 2))
    judge.add_column(style=DIM, justify="right")
    judge.add_column()
    judge.add_row("types", "strict TypeScript, zero errors; no any / @ts-ignore / @ts-nocheck; few casts")
    judge.add_row("behaviour", "compiled output must match the original JavaScript token for token")
    judge.add_row("tests", f"`{ctx.repo.test}` must still pass, in Docker, with test files untouched (hashed)")
    judge.add_row("output", f"branch migration-swarm/{ctx.run_id} in {ctx.workdir}  [{DIM}]your repo is not modified[/]")

    return Group(Rule(style=DIM), Padding(head, (1, 0, 1, 1)), Padding(facts, (0, 0, 1, 1)),
                 Padding(lt, (0, 0, 1, 2)),
                 Padding(Text("agents", style=f"bold {ACCENT}"), (0, 0, 0, 1)), Padding(agents, (0, 0, 1, 1)),
                 Padding(Text("judge", style=f"bold {ACCENT}"), (0, 0, 0, 1)), Padding(judge, (0, 0, 1, 1)),
                 Rule(style=DIM))


# ---------------------------------------------------------------------------------------------
# live dashboard


ICON = {"accepted": ("✓", OK), "degraded": ("◐", WARN), "rejected": ("↻", WARN), "failed": ("✗", BAD)}
PHASE_LABEL = {"analyze": "Analyzing", "plan": "Planning", "setup": "Setting up", "migrate": "Migrating",
               "finalize": "Finalizing", "done": "Done", "setup_failed": "Setup failed", "declined": "Cancelled"}


class Dashboard:
    def __init__(self, ctx, provider: str, backend: str):
        self.ctx, self.provider, self.backend = ctx, provider, backend
        self.phase, self.detail = "analyze", "starting"
        self.layer = (0, 0)
        self.units: dict[str, str] = {}
        self.agents: dict[str, dict] = {}
        self.judge: str | None = None
        self.activity: collections.deque = collections.deque(maxlen=9)
        self.started = time.time()
        self.last_log = ""
        self.plan_wait: float | None = None

    # events arrive from worker threads; dict/deque updates are atomic enough for a view
    def on_event(self, ev: dict) -> None:
        k = ev["kind"]
        if k == "phase":
            self.phase, self.detail = ev["phase"], ev.get("detail") or ""
            if ev["phase"] in ("finalize", "done"):
                self.judge = None
        elif k == "layer":
            self.layer = (ev["index"] + 1, ev["count"])
            self.phase = "migrate"
            self.agents = {a: v for a, v in self.agents.items() if v["state"] not in ("done", "error")}
        elif k == "agent_start":
            self.agents[ev["agent"]] = {"role": ev["role"], "files": ev["files"], "unit": ev["unit"], "model": ev["model"],
                                        "attempt": ev["attempt"], "degraded": ev["degraded"], "state": "thinking",
                                        "chars": 0, "tail": "", "t0": time.time(), "cost": None}
            self.units.setdefault(ev["unit"], "pending")
        elif k == "agent_progress" and ev["agent"] in self.agents:
            a = self.agents[ev["agent"]]
            a.update(state="writing", chars=ev["chars"], tail=ev["tail"])
        elif k == "agent_done" and ev["agent"] in self.agents:
            a = self.agents[ev["agent"]]
            a.update(state="error" if ev.get("error") else "done", cost=ev.get("cost_usd"), error=ev.get("error"),
                     t1=time.time())
        elif k == "judge_start":
            self.judge = f"checking {len(ev['units'])} files from layer {ev['layer'] + 1}"
            for a in self.agents.values():
                if a["state"] == "done":
                    a["state"] = "judging"
        elif k == "judge_step":
            self.judge = ev["detail"]
        elif k == "judge_tests":
            self.judge = f"tests {'passed' if ev['ok'] else 'failed'} ({ev['passed']} passed, {ev['failed']} failed)"
        elif k == "verdict":
            self.units[ev["unit"]] = ev["status"] if ev["status"] != "rejected" else "pending"
            icon, color = ICON[ev["status"]]
            name = self._name(ev["files"])
            why = ""
            if ev["status"] in ("rejected", "failed") and ev.get("reasons"):
                why = " · " + str(ev["reasons"][0]).replace("\n", " ")[:90]
            verb = {"accepted": "accepted", "degraded": "accepted (degraded)", "rejected": "rejected, retrying",
                    "failed": "failed, left as JavaScript"}[ev["status"]]
            self.activity.append((time.time(), Text.assemble((f"{icon} ", color), (name, "bold"), (f"  {verb}", color),
                                                              (why, DIM))))
            a = self.agents.get(ev["unit"])
            if a:
                a["state"] = ev["status"]
        elif k == "steward":
            msg = (f"merged {ev['requests']} shared type requests" if not ev["rejected"]
                   else f"rejected shared types: {ev['rejected'][0]}")
            self.activity.append((time.time(), Text.assemble(("◆ ", ACCENT), ("steward", "bold"), (f"  {msg}", DIM))))
        elif k == "plan_wait":
            self.plan_wait = ev["until"]
        elif k == "log":
            self.last_log = ev["msg"]

    @staticmethod
    def _name(files: list[str]) -> str:
        if not files:
            return "shared types"
        return files[0] + (f" +{len(files) - 1}" if len(files) > 1 else "")

    def _header(self) -> Table:
        snap = self.ctx.meter.snapshot()
        el = int(time.time() - self.started)
        total = len(self.units) or 1
        counts = collections.Counter(self.units.values())
        done = counts["accepted"] + counts["degraded"] + counts["failed"]
        g = Table.grid(padding=(0, 2), expand=True)
        g.add_column(style=DIM, width=7)
        g.add_column(ratio=1)
        g.add_column(justify="right", no_wrap=True)
        phase = PHASE_LABEL.get(self.phase, self.phase)
        layer = f"  ·  layer {self.layer[0]}/{self.layer[1]}" if self.layer[1] and self.phase == "migrate" else ""
        g.add_row("", Text.assemble((phase, f"bold {ACCENT}"), (layer, ""), (f"  {self.detail}" if self.phase != "migrate" else "", DIM)),
                  Text(f"{el // 60:02d}:{el % 60:02d}", style=DIM))
        g.add_row("files", ProgressBar(total=total, completed=done, width=None, complete_style=OK, finished_style=OK),
                  Text.assemble((f"{done}/{len(self.units)}", "bold"), "  ",
                                (f"✓ {counts['accepted']}", OK), "  ", (f"◐ {counts['degraded']}", WARN), "  ",
                                (f"✗ {counts['failed']}", BAD)))
        spent, budget = snap["spent_usd"], snap["budget_usd"]
        tokens = sum(v for v in snap["tokens"].values() if isinstance(v, (int, float)))
        g.add_row("spend", ProgressBar(total=budget, completed=min(spent, budget), width=None,
                                       complete_style=ACCENT if spent < budget * 0.8 else WARN),
                  Text.assemble((money(spent), "bold"), (f" / ${budget:.2f}", DIM), (f"  ·  {tokens / 1000:,.0f}k tok", DIM),
                                (f"  ·  {self.provider}{' plan' if self.backend == 'plan' else ''}", DIM)))
        return g

    def _agents(self) -> Table:
        t = Table(box=None, show_header=False, pad_edge=False, expand=True, padding=(0, 1))
        t.add_column(width=2)
        t.add_column(no_wrap=True, max_width=36, overflow="ellipsis")
        t.add_column(no_wrap=True, style=DIM, width=24, overflow="ellipsis")
        t.add_column(no_wrap=True, width=12)
        t.add_column(ratio=1, no_wrap=True, overflow="ellipsis", style=DIM)
        rows = sorted(self.agents.items(), key=lambda kv: kv[1]["t0"])[-12:]
        for _, a in rows:
            st = a["state"]
            if st == "thinking":
                icon, status, detail = Spinner("dots", style=ACCENT), Text("thinking", style=ACCENT), ""
            elif st == "writing":
                icon, status = Spinner("dots", style=ACCENT), Text(f"{a['chars'] / 1000:.1f}k chars", style=ACCENT)
                detail = a["tail"]
            elif st in ("done", "judging"):
                icon, status, detail = Spinner("dots2", style=DIM), Text("judging", style=DIM), "waiting for the judge"
            elif st == "error":
                icon, status, detail = Text("✗", style=BAD), Text("error", style=BAD), a.get("error") or ""
            else:
                ic, color = ICON.get(st, ("·", DIM))
                icon, status = Text(ic, style=color), Text(st, style=color)
                detail = money(a["cost"]) if a.get("cost") else ""
            role = a["role"] + (f" #{a['attempt'] + 1}" if a["attempt"] else "")
            if a["degraded"]:
                role += " (degraded)"
            t.add_row(icon, Text(self._name(a["files"]) if a["files"] else a["unit"], style="bold"),
                      f"{role} · {short_model(a['model'])}", status, detail)
        return t

    def __rich__(self):
        parts = [Padding(self._header(), (1, 1, 1, 1))]
        parts.append(Padding(Text("agents", style=f"bold {ACCENT}"), (0, 1)))
        if self.agents:
            parts.append(Padding(self._agents(), (0, 1, 1, 3)))
        else:
            parts.append(Padding(Spinner("dots", style=ACCENT, text=Text(f" {self.last_log or 'starting'}"[:110], style=DIM)),
                                 (0, 1, 1, 3)))
        if self.judge:
            parts.append(Padding(Text.assemble(("judge  ", f"bold {ACCENT}"), (self.judge, DIM)), (0, 1, 1, 1)))
        if self.plan_wait and self.plan_wait > time.time():
            left = int(self.plan_wait - time.time())
            parts.append(Padding(Text(f"paused on your Claude plan's usage limit; resuming in {left // 3600}h "
                                      f"{left % 3600 // 60:02d}m", style=WARN), (0, 1, 1, 1)))
        if self.activity:
            act = Table.grid(padding=(0, 2), expand=True)
            act.add_column(style=DIM, no_wrap=True, min_width=8)
            act.add_column(no_wrap=True, overflow="ellipsis", ratio=1)
            for ts, line in self.activity:
                act.add_row(time.strftime("%H:%M:%S", time.localtime(ts)), line)
            parts.append(Padding(Text("activity", style=f"bold {ACCENT}"), (0, 1)))
            parts.append(Padding(act, (0, 1, 1, 3)))
        return Panel(Group(*parts), box=box.ROUNDED, border_style=DIM,
                     title=Text.assemble((" migration-swarm ", f"bold {ACCENT}"), (f"{self.ctx.migration} ", DIM)),
                     title_align="left", subtitle=Text(" ctrl-c to stop · resume later ", style=DIM), subtitle_align="right")


# ---------------------------------------------------------------------------------------------
# summary


def summary(ctx) -> None:
    p = ctx.run_dir / "report.json"
    if not p.exists():
        console.print(f"[{WARN}]No report was written.[/] Log: {ctx.run_dir / 'run.log'}")
        return
    r = json.loads(p.read_text())
    res, fin, cost = r["result"], r.get("final") or {}, r["cost"]
    by = res["files_by_status"]
    ok = r["complete"]
    title = Text("Migration complete" if ok else f"Stopped: {r['stop_reason']}", style=f"bold {OK if ok else WARN}")
    g = Table.grid(padding=(0, 2))
    g.add_column(style=DIM, justify="right")
    g.add_column()
    g.add_row("files", Text.assemble((f"✓ {by.get('accepted', 0)} typed", OK), "   ", (f"◐ {by.get('degraded', 0)} degraded", WARN),
                                     "   ", (f"✗ {by.get('failed', 0)} left as JS", BAD),
                                     *([("   ", ""), (f"{by.get('not_reached', 0) + by.get('stopped_mid_retry', 0)} not reached", DIM)]
                                       if by.get("not_reached") or by.get("stopped_mid_retry") else [])))
    if fin.get("tsc_errors") is not None:
        g.add_row("type-check", f"{fin['tsc_errors']} strict errors")
    if fin.get("tests"):
        t = fin["tests"]
        g.add_row("tests", f"{t.get('passed', '?')} passed, {t.get('failed', '?')} failed"
                  + (f"  [{DIM}](same as before)[/]" if fin.get("tests_match_baseline") else f"  [{WARN}](worse than before)[/]"))
    plan = r.get("llm_backend") == "plan"
    g.add_row("cost", money(cost["spent_usd"]) + (f"  [{DIM}]API-equivalent, nothing billed[/]" if plan else "")
              + f"  [{DIM}]· {cost['calls']} model calls · {r['wall_clock_s']}s[/]")
    if r.get("branch"):
        g.add_row("branch", f"{r['branch']}  [{DIM}]in {r['workdir']}[/]")
    g.add_row("report", str(ctx.run_dir / "report.md"))
    console.print(Panel(Group(Padding(title, (0, 0, 1, 0)), g), box=box.ROUNDED, border_style=DIM, padding=(1, 2)))
    if r.get("branch"):
        console.print(f"  [{DIM}]Bring the branch into your repo:[/]")
        console.print(f"    git fetch {r['workdir']} {r['branch']}:{r['branch']}", soft_wrap=True)
    if not ok:
        console.print(f"  [{DIM}]Continue later:[/]  migration-swarm resume {ctx.run_id}")
    console.print()


# ---------------------------------------------------------------------------------------------
# flow


async def _run(ctx, pack, it, cfg, repo, provider, backend) -> None:
    dash = Dashboard(ctx, provider, backend)
    ctx.bus().subscribe(dash.on_event)
    prep = {"live": None}

    def prep_view():
        return Padding(Group(Spinner("dots", text=Text.assemble(
            (PHASE_LABEL.get(dash.phase, dash.phase), f"bold {ACCENT}"), ("  " + (dash.last_log or dash.detail)[:110], DIM)),
            style=ACCENT)), (0, 0, 0, 2))

    class _PrepView:
        def __rich__(self):
            return prep_view()

    async def approve(state: dict) -> bool:
        prep["live"].stop()
        while True:
            console.print(plan_view(it, state, cfg, ctx, repo))
            choice = await _ask_async(questionary.select(
                "Start the migration?", style=QSTYLE, qmark="›",
                choices=[questionary.Choice("Start", "go"), questionary.Choice("Change budget", "budget"),
                         questionary.Choice("Cancel", "no")]))
            if choice == "budget":
                v = await _ask_async(questionary.text("Budget in USD:", default=f"{cfg['budget_usd']:.2f}", style=QSTYLE,
                                                      qmark="›", validate=lambda s: s.replace(".", "", 1).isdigit()))
                cfg["budget_usd"] = ctx.cfg["budget_usd"] = ctx.meter.budget_usd = float(v)
                (ctx.run_dir / "config.json").write_text(json.dumps(ctx.cfg, indent=1))
                continue
            if choice != "go":
                return False
            console.print()
            dash.started = time.time()
            dash.units = {uid: "pending" for uid in state["unit_files"]}
            dash.phase, dash.detail = "setup", "tsconfig, types, baseline tests"
            prep["live"] = Live(dash, console=console, refresh_per_second=8, transient=False)
            prep["live"].start()
            return True

    prep["live"] = Live(_PrepView(), console=console, refresh_per_second=10, transient=True)
    prep["live"].start()
    try:
        await graph.execute(ctx, pack, approve=approve)
    finally:
        if prep["live"] is not None:
            prep["live"].stop()


def main() -> int:
    banner()
    try:
        if not check_docker():
            return 1
        provider, backend = choose_provider()
        repo = choose_repo()
        cfg = config_mod.load(provider=provider, llm_backend=backend)
        router = LLM(cfg, CostMeter(budget_usd=1.0, prices=cfg["prices"]), "intent", "-")
        while True:
            request = _ask(questionary.text("What migration do you want?", style=QSTYLE, qmark="›",
                                            instruction="  e.g. convert lib/ to strict TypeScript"))
            with console.status(Text("understanding the request", style=DIM), spinner="dots", spinner_style=ACCENT):
                it = asyncio.run(intent_mod.route(router, request, repo))
            if it.migration:
                console.print(Text.assemble(("  → ", ACCENT), (it.migration, "bold"), (f"  {it.summary}", DIM)))
                break
            console.print(Text.assemble(("  Not supported yet: ", WARN), (it.unsupported_reason or "", "")))
            console.print(Text("  Supported: " + "; ".join(f"{n}: {migrations.load(n).DESCRIPTION}"
                                                           for n in migrations.MIGRATIONS), style=DIM))
        pack = migrations.load(it.migration)
        try:
            spec = pack.detect_repo_spec(repo, it.source_dirs)
        except FileNotFoundError as e:
            console.print(f"[{BAD}]This repo doesn't fit {it.migration}:[/] {e}")
            return 1
        if it.budget_usd:
            cfg["budget_usd"] = float(it.budget_usd)
        ctx = runs.create(str(repo), pack.NAME, spec, cfg)
        ctx.quiet = True
        ctx.llm = LLM(cfg, ctx.meter, ctx.run_id, ctx.migration, log=ctx.log, emit=ctx.emit)
        console.print(Text(f"  run {ctx.run_id}  ·  installing and analyzing in Docker (the first run builds images)", style=DIM))
        try:
            asyncio.run(_run(ctx, pack, it, cfg, repo, provider, backend))
        except KeyboardInterrupt:
            console.print(f"\n[{WARN}]Stopped.[/] Continue with:  migration-swarm resume {ctx.run_id}")
            return 130
        status = json.loads((ctx.run_dir / "status.json").read_text()).get("phase") if (ctx.run_dir / "status.json").exists() else None
        if status == "declined":
            console.print(Text("  Cancelled. Nothing was changed or spent.", style=DIM))
            return 0
        console.print()
        summary(ctx)
        return 0
    except Abort:
        console.print(Text("  bye", style=DIM))
        return 130
