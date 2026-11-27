"""The plan a user approves: written to runs/<id>/plan_summary.json by the plan node, shown by the
interactive mode, returned by the MCP server."""

from __future__ import annotations

import json

from migration_swarm.core.budget import Usage, price_usage


def estimate_cost(cfg: dict, files: list[str], workdir) -> tuple[float, float]:
    """Rough cost range (USD, API prices) from file sizes and the escalation ladder; capped at the budget."""
    chars = sum((workdir / f).stat().st_size for f in files if (workdir / f).exists())
    tok = chars / 3.5
    worker, esc = cfg["models"]["worker"], cfg["models"]["escalation"]
    n = len(files)

    def cost(model, files_tok, calls):
        u = Usage(input_tokens=int(files_tok * 2.2 + calls * 3500), output_tokens=int(files_tok * 1.3 + calls * 2500))
        return price_usage(cfg["prices"], model, u)

    low = cost(worker, tok * 1.2, n * 1.2) + cost(esc, tok * 0.15, n * 0.15)
    high = cost(worker, tok * 2.5, n * 2.5) + cost(esc, tok * 0.8, n * 0.8)
    return round(low, 4), round(min(high, cfg["budget_usd"]), 4)


def build(ctx, analysis: dict, layers: list[list[str]], unit_files: dict[str, list[str]]) -> dict:
    cfg = ctx.cfg
    low, high = estimate_cost(cfg, analysis["files"], ctx.workdir)
    return {
        "run_id": ctx.run_id,
        "migration": ctx.migration,
        "source_repo": ctx.source_repo,
        "source_dirs": ctx.repo.source_dirs,
        "files": len(analysis["files"]),
        "loc": analysis["total_loc"],
        "import_cycles": len(analysis["cycles"]),
        "layers": [{"index": k + 1, "units": len(layer), "files": [f for uid in layer for f in unit_files[uid]]}
                   for k, layer in enumerate(layers)],
        "provider": cfg.get("provider", "claude"),
        "llm_backend": cfg.get("llm_backend", "api"),
        "models": cfg["models"],
        "retries_before_escalation": cfg["retries_before_escalation"],
        "concurrency": cfg["concurrency"],
        "budget_usd": cfg["budget_usd"],
        "estimate_usd": [low, high],
        "billing": ("API-equivalent; not billed, uses the Claude plan's limits" if cfg.get("llm_backend") == "plan"
                    else "billed to the provider API key"),
        "judge": {"test_command": ctx.repo.test, "protected_tests": ctx.repo.test_paths,
                  "escape_budgets": cfg["escape_budgets"]},
        "output": {"branch": f"migration-swarm/{ctx.run_id}", "workdir": str(ctx.workdir)},
    }


def write(ctx, summary: dict) -> None:
    (ctx.run_dir / "plan_summary.json").write_text(json.dumps(summary, indent=1))


def load(run_dir) -> dict | None:
    p = run_dir / "plan_summary.json"
    return json.loads(p.read_text()) if p.exists() else None
