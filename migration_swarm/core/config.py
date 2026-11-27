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
    cfg = _merge(cfg, {k: v for k, v in overrides.items() if v is not None})
    provider = cfg.get("provider", "claude")
    if provider not in cfg["provider_models"]:
        raise ValueError(f"unknown provider {provider!r}; choose one of {', '.join(cfg['provider_models'])}")
    # explicit `models` entries (from a --config file or override) win over the provider's defaults
    cfg["models"] = {**cfg["provider_models"][provider], **(cfg.get("models") or {})}
    return cfg
