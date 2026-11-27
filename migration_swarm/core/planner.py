"""Turn a file dependency graph into layers of work units, leaves first.

A unit is one file, or one strongly connected component (an import cycle) that a single worker
owns. A unit is in layer k when every unit it depends on is in a layer < k.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Unit:
    id: str
    files: tuple[str, ...]

    @property
    def cyclic(self) -> bool:
        return len(self.files) > 1


def strongly_connected_components(nodes: list[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's algorithm, iterative, deterministic (nodes and neighbours visited in sorted order)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(edges.get(w, ())))))
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[v])
                if low[v] == index[v]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    out.append(sorted(comp))
    return out


def plan_layers(files: list[str], edges: dict[str, set[str]]) -> list[list[Unit]]:
    """edges[a] = local files that a imports. Edges to unknown files are ignored."""
    known = set(files)
    edges = {f: {d for d in edges.get(f, ()) if d in known and d != f} for f in files}
    comps = strongly_connected_components(files, edges)
    unit_of: dict[str, Unit] = {}
    for comp in comps:
        u = Unit(id=comp[0], files=tuple(comp))
        for f in comp:
            unit_of[f] = u
    deps = {u: {unit_of[d] for f in u.files for d in edges[f]} - {u} for u in set(unit_of.values())}

    layer_of: dict[Unit, int] = {}

    def depth(u: Unit) -> int:
        # The condensation is a DAG, so this terminates; memoized, and Tarjan emits SCCs in
        # reverse topological order, so dependencies are usually resolved first.
        if u not in layer_of:
            layer_of[u] = 0 if not deps[u] else 1 + max(depth(d) for d in deps[u])
        return layer_of[u]

    for comp in comps:  # dependencies-first order keeps recursion shallow
        depth(unit_of[comp[0]])
    n = max(layer_of.values(), default=-1) + 1
    layers: list[list[Unit]] = [[] for _ in range(n)]
    for u, k in layer_of.items():
        layers[k].append(u)
    return [sorted(layer, key=lambda u: u.id) for layer in layers]
