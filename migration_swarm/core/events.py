"""Run events: what the agents are doing, for live views and `events.jsonl`.

Emitters call ctx.emit(kind, **data). Listeners (e.g. the interactive dashboard) get every event in
the emitting thread, so they must be quick and thread-safe. Kinds used by the core:

  phase        {phase, detail}                                analyze / plan / setup / migrate / finalize / done
  layer        {index, count, units}
  agent_start  {agent, role, unit, files, model, attempt, degraded}
  agent_progress {agent, chars, tail}                          streaming output so far
  agent_done   {agent, unit, cost_usd, tokens, stop_reason, error}
  judge_start  {layer, units}
  judge_tests  {units, ok}                                    one (bisection) test run
  verdict      {unit, files, status, attempt, reasons}        accepted / degraded / rejected / failed
  steward      {requests, rejected}
  plan_wait    {until, reason}                                paused on a plan usage limit
  log          {msg}
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

Listener = Callable[[dict], None]
EPHEMERAL = {"agent_progress"}  # high-frequency; delivered to listeners, not written to events.jsonl


class EventBus:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.listeners: list[Listener] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Listener) -> None:
        self.listeners.append(fn)

    def emit(self, kind: str, **data) -> None:
        ev = {"t": round(time.time(), 3), "kind": kind, **data}
        with self._lock:
            if self.path is not None and kind not in EPHEMERAL:
                with self.path.open("a") as f:
                    f.write(json.dumps(ev, default=str) + "\n")
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:  # a broken view must never break a run
                pass
