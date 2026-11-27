"""Available migrations. One entry per pack; no registry machinery until a second pack exists."""

import importlib

MIGRATIONS = {
    "js-to-ts": "migration_swarm.migrations.js_to_ts",
}


def load(name: str):
    if name not in MIGRATIONS:
        raise KeyError(f"unknown migration {name!r}; available: {', '.join(MIGRATIONS)}")
    return importlib.import_module(MIGRATIONS[name])
