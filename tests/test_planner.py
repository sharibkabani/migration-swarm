from migration_swarm.core.planner import plan_layers, strongly_connected_components


def test_cycle_becomes_one_unit_and_layers_are_leaves_first():
    # utils <- a <-> b (cycle) <- app ;  constants <- utils ; c is independent
    files = ["app.js", "a.js", "b.js", "utils.js", "constants.js", "c.js"]
    edges = {
        "app.js": {"a.js", "c.js"},
        "a.js": {"b.js", "utils.js"},
        "b.js": {"a.js"},
        "utils.js": {"constants.js", "node:fs"},  # unknown targets are ignored
    }
    layers = plan_layers(files, edges)
    as_files = [[u.files for u in layer] for layer in layers]
    assert as_files == [
        [("c.js",), ("constants.js",)],
        [("utils.js",)],
        [("a.js", "b.js")],
        [("app.js",)],
    ]
    assert layers[2][0].cyclic and not layers[0][0].cyclic


def test_every_unit_depends_only_on_earlier_layers():
    files = [f"f{i}.js" for i in range(30)]
    edges = {f"f{i}.js": {f"f{j}.js" for j in range(i) if (i * j) % 7 == 1} for i in range(30)}
    edges["f3.js"].add("f20.js")
    edges["f20.js"].add("f3.js")  # introduce a cycle
    layers = plan_layers(files, edges)
    layer_of = {f: k for k, layer in enumerate(layers) for u in layer for f in u.files}
    assert sorted(layer_of) == sorted(files)
    for k, layer in enumerate(layers):
        for u in layer:
            for f in u.files:
                for d in edges.get(f, ()):
                    if d not in u.files:
                        assert layer_of[d] < k


def test_scc_self_loop_and_singletons():
    comps = strongly_connected_components(["x", "y"], {"x": {"x"}, "y": set()})
    assert sorted(comps) == [["x"], ["y"]]
