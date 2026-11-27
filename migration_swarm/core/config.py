from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULT = Path(__file__).resolve().parents[1] / "config" / "default.yaml"


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: Path | None = None, **overrides: Any) -> dict:
    cfg = yaml.safe_load(DEFAULT.read_text())
    if path:
        cfg = _merge(cfg, yaml.safe_load(Path(path).read_text()) or {})
    return _merge(cfg, {k: v for k, v in overrides.items() if v is not None})
