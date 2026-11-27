"""Prompts and response parsing for js-to-ts workers and the shared-types steward."""

from __future__ import annotations

import json
import re

MODULE_RULES = {
    "cjs": """This package is CommonJS and must stay CommonJS at runtime. Convert module syntax so the emitted JavaScript is identical:
- `const x = require('./x')` -> `import x = require('./x')` (also for npm packages and node builtins).
- `const { a, b } = require('./x')` -> `const { a, b } = require('./x') as typeof import('./x')` (identical emit; not counted as a cast).
- `module.exports = X` -> `export = X`. Never combine `export =` with other `export` statements.
- `exports.name = value` / `module.exports.name = value` (and no `module.exports =`) -> `export const name = value` or `export function name(...)`.
- Keep every require path exactly as written.
Example (JavaScript -> TypeScript, identical compiled output):
```js
const { domEach, isTag } = require('../utils');
const select = require('cheerio-select');
exports.closest = function (selector) { return domEach(this, isTag); };
```
```ts
const { domEach, isTag } = require('../utils') as typeof import('../utils');
import select = require('cheerio-select');
export const closest = function (this: Cheerio, selector?: string): Cheerio { return domEach(this, isTag); };
```""",
    "esm-node": """This package is native ESM ("type": "module"). Keep every import/export statement and specifier exactly as written, including the `.js` extension on relative imports (it resolves to the .ts file). Use `import type` for type-only imports.""",
    "esm-bundled": """This package uses ESM syntax compiled by babel. Keep every import/export statement and specifier exactly as written (extensionless relative imports are fine). Use `import type` for type-only imports.""",
}

WORKER_SYSTEM = """You convert JavaScript modules to strict TypeScript, one module (or one import cycle) at a time. A deterministic judge checks your output; follow every rule.

1. Runtime behaviour must not change. Only add types. Do not change logic, control flow, values, strings, error messages, export shapes or statement order, and do not delete code. Never rename an identifier, never introduce a new variable or helper, and never change how a value is obtained (keep each `require`, destructuring and property access as it is). The judge compiles your TypeScript and compares the emitted JavaScript token by token with the original; any removed or altered runtime token is rejected.
2. `tsc` with `strict: true` must report zero errors in your file. Forbidden: `any` (explicit, or implicit from a missing annotation), `@ts-ignore`, `@ts-expect-error`, `@ts-nocheck`, `as unknown as`. Use `as` casts and non-null `!` sparingly (a few per file at most). Prefer precise types, interfaces, generics, overloads, `unknown` with narrowing, and type predicates.
3. Module syntax:
{module_rules}
4. Only import type names that exist: for npm packages, use the exported names listed in the request (taken from the installed type declarations); if a name you want is not listed, derive the type instead (`ReturnType<typeof fn>`, `Parameters<...>`, indexed access) or declare a local interface. Types of other project modules are given as declarations. Reuse them via `import type`; do not redeclare them. The declarations of not-yet-migrated modules are inferred from JavaScript and may be loose; type your side precisely anyway.
5. If a type is shared by several modules and does not exist yet, declare it locally and also request it from the shared-types steward (it will be added to `{shared_path}` for later modules).
6. Keep comments and JSDoc. You may turn JSDoc types into TypeScript types.

Reply with exactly this format and nothing else:
```ts
// file: <path of the .ts file>
<complete file content>
```
(one such block per file when you are given several files), then optionally:
```json
{{"shared_type_requests": [{{"name": "Name", "definition": "export interface Name {{ ... }}", "reason": "why it is shared"}}]}}
```"""

DEGRADED_NOTE = """FINAL ATTEMPT (degraded mode): earlier attempts failed. You may now use explicit `any`, `as` casts and non-null assertions where a precise type is impractical. Still forbidden: `@ts-ignore`, `@ts-expect-error`, `@ts-nocheck`, and any runtime change."""

STEWARD_SYSTEM = """You maintain one shared TypeScript type file used across a codebase that is being migrated from JavaScript. Workers request shared types; you merge the requests into the file.

Rules:
- The file must contain only type-level declarations: `export interface`, `export type`, and `import type`. No runtime code: no values, functions, classes, enums or `const`.
- Deduplicate: if two requests describe the same concept, keep one well-named declaration. Do not rename or remove declarations already in the file (other modules import them); you may widen them compatibly.
- Types must compile under `strict: true` without `any`.

Reply with exactly one block:
```ts
// file: {path}
<complete file content>
```"""


def ts_name(js: str) -> str:
    return re.sub(r"\.(c|m)?js$", lambda m: f".{m.group(1) or ''}ts", js)


def worker_messages(*, style: str, shared_path: str, files: dict[str, str], deps_dts: dict[str, str],
                    shared_types: str | None, dependents: list[str], retry: dict | None, degraded: bool,
                    npm: dict[str, dict] | None = None) -> tuple[str, str]:
    system = WORKER_SYSTEM.format(module_rules=MODULE_RULES[style], shared_path=shared_path)
    parts = []
    targets = ", ".join(f"`{f}` -> `{ts_name(f)}`" for f in files)
    parts.append(f"Convert {targets}." + (" These files import each other (an import cycle); convert them together." if len(files) > 1 else ""))
    for path, src in files.items():
        parts.append(f"Original JavaScript:\n```js\n// file: {path}\n{src}\n```")
    if deps_dts:
        parts.append("Declarations of the project modules it imports:")
        for path, dts in deps_dts.items():
            parts.append(f"```ts\n// declarations of: {path}\n{dts}\n```")
    if npm:
        lines = []
        for pkg, e in sorted(npm.items()):
            if e.get("unresolved"):
                lines.append(f"- {pkg}@{e.get('version')}: no type declarations (type what you use locally)")
            else:
                lines.append(f"- {pkg}@{e.get('version')}: types: {', '.join(e['types'][:120]) or '(none)'}; "
                             f"values (classes here are also types): {', '.join(e['values'][:120]) or '(none)'}")
        parts.append("npm packages this code imports (installed version, exported names from their type declarations):\n" + "\n".join(lines))
    if shared_types:
        parts.append(f"Shared types file `{shared_path}` (import with `import type`):\n```ts\n{shared_types}\n```")
    if dependents:
        parts.append("Modules that import this one (keep the exported API compatible): " + ", ".join(dependents[:30]))
    if retry:
        parts.append("Your previous attempt was REJECTED by the judge. Fix every problem below and return the complete files again.")
        parts.append("Rejection reasons:\n" + "\n".join(f"- {r}" for r in retry["reasons"]))
        if retry.get("previous"):
            prev = "\n".join(f"```ts\n// file: {p}\n{c}\n```" for p, c in retry["previous"].items())
            parts.append("Your previous attempt:\n" + prev)
    if degraded:
        parts.append(DEGRADED_NOTE)
    return system, "\n\n".join(parts)


def steward_messages(*, path: str, current: str | None, requests: list[dict], retry_reasons: list[str] | None) -> tuple[str, str]:
    parts = [f"Current content of `{path}`:\n```ts\n{current or '// (empty)'}\n```",
             "Requests from workers:\n```json\n" + json.dumps(requests, indent=1) + "\n```"]
    if retry_reasons:
        parts.append("Your previous version was rejected:\n" + "\n".join(f"- {r}" for r in retry_reasons))
    return STEWARD_SYSTEM.format(path=path), "\n\n".join(parts)


BLOCK = re.compile(r"```(ts|typescript|json)[ \t]*\n(.*?)\n```", re.S)
HEADER = re.compile(r"^\s*//\s*file:\s*(\S+)\s*\n")


def parse_worker_output(text: str, expected: list[str]) -> tuple[dict[str, str], list[dict], list[str]]:
    """Returns ({ts_path: content}, shared_type_requests, problems)."""
    files: dict[str, str] = {}
    requests: list[dict] = []
    problems: list[str] = []
    expected_ts = [ts_name(f) for f in expected]
    unnamed: list[str] = []
    for lang, body in BLOCK.findall(text):
        if lang == "json":
            try:
                requests.extend(json.loads(body).get("shared_type_requests", []))
            except (ValueError, AttributeError):
                problems.append("the json block was not valid JSON with a shared_type_requests list")
            continue
        m = HEADER.match(body)
        if m:
            path = m.group(1)
            files[path] = body[m.end():]
        else:
            unnamed.append(body)
    if unnamed and len(expected_ts) == 1 and expected_ts[0] not in files:
        files[expected_ts[0]] = unnamed[0]
    for p in list(files):
        if p not in expected_ts:
            problems.append(f"unexpected file `{p}` in reply (you may only write {', '.join(expected_ts)})")
            del files[p]
    for p in expected_ts:
        if p not in files:
            problems.append(f"no ```ts block for `{p}` in the reply")
    return {p: (c if c.endswith("\n") else c + "\n") for p, c in files.items()}, requests, problems


def parse_steward_output(text: str, path: str) -> str | None:
    for lang, body in BLOCK.findall(text):
        if lang != "json":
            m = HEADER.match(body)
            return body[m.end():] if m else body
    return None
