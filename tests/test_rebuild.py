"""End-to-end tests for POST /plan/rebuild."""

import collections
import random

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _plan(**payload):
    response = client.post("/plan/rebuild", json=payload)
    return response.status_code, response.json()


def _edge(source, source_version, target, target_version):
    return {
        "source": source,
        "source_version": source_version,
        "target": target,
        "target_version": target_version,
    }


def _names(path):
    return [node["name"] for node in path]


CHAIN_RESOLVED = {"app": "1.0", "lib": "1.0", "util": "1.0"}
CHAIN_GRAPH = [
    _edge("<root>", "", "app", "1.0"),
    _edge("app", "1.0", "lib", "1.0"),
    _edge("lib", "1.0", "util", "1.0"),
]

DIAMOND_RESOLVED = {"app": "1.0", "left": "1.0", "right": "1.0", "shared": "1.5"}
DIAMOND_GRAPH = [
    _edge("<root>", "", "app", "1.0"),
    _edge("app", "1.0", "left", "1.0"),
    _edge("app", "1.0", "right", "1.0"),
    _edge("left", "1.0", "shared", "1.5"),
    _edge("right", "1.0", "shared", "1.5"),
]


def test_chain_plan_orders_dependencies_before_dependents():
    status, body = _plan(
        resolved=CHAIN_RESOLVED,
        dependency_graph=CHAIN_GRAPH,
        changed=["util"],
    )
    assert status == 200
    assert body["status"] == "ok"
    assert body["summary"] == {
        "changed": 1,
        "missing": 0,
        "affected": 3,
        "groups": 3,
        "stages": 3,
    }
    assert body["missing"] == []
    assert body["stages"] == [
        [{"packages": ["util"], "cyclic": False}],
        [{"packages": ["lib"], "cyclic": False}],
        [{"packages": ["app"], "cyclic": False}],
    ]
    packages = {p["name"]: p for p in body["packages"]}
    assert packages["util"]["changed"] is True
    assert packages["util"]["distance"] == 0
    assert packages["util"]["stage"] == 0
    assert _names(packages["util"]["paths"][0]) == ["util"]
    assert packages["lib"]["distance"] == 1
    assert packages["lib"]["stage"] == 1
    assert _names(packages["lib"]["paths"][0]) == ["util", "lib"]
    assert packages["app"]["distance"] == 2
    assert packages["app"]["stage"] == 2
    assert _names(packages["app"]["paths"][0]) == ["util", "lib", "app"]
    # Paths carry the resolved versions.
    assert packages["app"]["paths"][0][-1] == {"name": "app", "version": "1.0"}


def test_diamond_reports_all_shortest_reverse_paths_and_parallel_stage():
    status, body = _plan(
        resolved=DIAMOND_RESOLVED,
        dependency_graph=DIAMOND_GRAPH,
        changed=["shared"],
    )
    assert status == 200
    assert body["summary"]["affected"] == 4
    # left and right are independent of each other -> same stage.
    assert body["stages"] == [
        [{"packages": ["shared"], "cyclic": False}],
        [
            {"packages": ["left"], "cyclic": False},
            {"packages": ["right"], "cyclic": False},
        ],
        [{"packages": ["app"], "cyclic": False}],
    ]
    packages = {p["name"]: p for p in body["packages"]}
    paths = [_names(p) for p in packages["app"]["paths"]]
    assert paths == [
        ["shared", "left", "app"],
        ["shared", "right", "app"],
    ]
    assert packages["app"]["distance"] == 2


def test_cycle_is_compressed_into_one_same_batch_group():
    status, body = _plan(
        resolved={"a": "1.0", "b": "1.0", "c": "1.0"},
        dependency_graph=[
            _edge("<root>", "", "c", "1.0"),
            _edge("c", "1.0", "a", "1.0"),
            _edge("a", "1.0", "b", "1.0"),
            _edge("b", "1.0", "a", "1.0"),  # a <-> b cycle
        ],
        changed=["b"],
    )
    assert status == 200
    assert body["summary"]["groups"] == 2
    assert body["stages"] == [
        [{"packages": ["a", "b"], "cyclic": True}],
        [{"packages": ["c"], "cyclic": False}],
    ]
    packages = {p["name"]: p for p in body["packages"]}
    assert packages["a"]["group"] == ["a", "b"]
    assert packages["b"]["group"] == ["a", "b"]
    assert packages["a"]["stage"] == 0
    assert packages["c"]["stage"] == 1
    assert _names(packages["a"]["paths"][0]) == ["b", "a"]
    assert _names(packages["c"]["paths"][0]) == ["b", "a", "c"]


def test_three_package_cycle_is_a_single_group_and_stage():
    status, body = _plan(
        resolved={"a": "1.0", "b": "1.0", "c": "1.0"},
        dependency_graph=[
            _edge("<root>", "", "a", "1.0"),
            _edge("a", "1.0", "b", "1.0"),
            _edge("b", "1.0", "c", "1.0"),
            _edge("c", "1.0", "a", "1.0"),
        ],
        changed=["a"],
    )
    assert status == 200
    assert body["summary"] == {
        "changed": 1,
        "missing": 0,
        "affected": 3,
        "groups": 1,
        "stages": 1,
    }
    assert body["stages"] == [[{"packages": ["a", "b", "c"], "cyclic": True}]]
    packages = {p["name"]: p for p in body["packages"]}
    assert _names(packages["b"]["paths"][0]) == ["a", "c", "b"]
    assert _names(packages["c"]["paths"][0]) == ["a", "c"]


def test_self_loop_marks_singleton_group_cyclic():
    status, body = _plan(
        resolved={"a": "1.0"},
        dependency_graph=[
            _edge("<root>", "", "a", "1.0"),
            _edge("a", "1.0", "a", "1.0"),
        ],
        changed=["a"],
    )
    assert status == 200
    assert body["stages"] == [[{"packages": ["a"], "cyclic": True}]]


def test_missing_changed_entries_are_reported_and_skipped():
    status, body = _plan(
        resolved=CHAIN_RESOLVED,
        dependency_graph=CHAIN_GRAPH,
        changed=["ghost", "util"],
    )
    assert status == 200
    assert body["summary"]["changed"] == 2
    assert body["summary"]["missing"] == 1
    assert body["missing"] == ["ghost"]
    # The missing entry contributes nothing to the plan.
    assert {p["name"] for p in body["packages"]} == {"app", "lib", "util"}


def test_unaffected_packages_are_excluded():
    status, body = _plan(
        resolved={"app": "1.0", "lib": "1.0", "other": "1.0"},
        dependency_graph=[
            _edge("<root>", "", "app", "1.0"),
            _edge("<root>", "", "other", "1.0"),
            _edge("app", "1.0", "lib", "1.0"),
        ],
        changed=["lib"],
    )
    assert status == 200
    assert {p["name"] for p in body["packages"]} == {"app", "lib"}


def test_changed_package_that_is_itself_a_dependency_of_a_changed_package():
    # Both a and c changed; b sits between them, so its nearest changed
    # package is c (distance 1), not a.
    status, body = _plan(
        resolved={"a": "1.0", "b": "1.0", "c": "1.0"},
        dependency_graph=[
            _edge("<root>", "", "a", "1.0"),
            _edge("a", "1.0", "b", "1.0"),
            _edge("b", "1.0", "c", "1.0"),
        ],
        changed=["a", "c"],
    )
    assert status == 200
    packages = {p["name"]: p for p in body["packages"]}
    assert packages["a"]["distance"] == 0
    assert packages["c"]["distance"] == 0
    assert packages["b"]["distance"] == 1
    assert [_names(p) for p in packages["b"]["paths"]] == [["c", "b"]]
    # a depends on b, b depends on c: dependencies are scheduled first even
    # though a itself changed.
    assert body["stages"] == [
        [{"packages": ["c"], "cyclic": False}],
        [{"packages": ["b"], "cyclic": False}],
        [{"packages": ["a"], "cyclic": False}],
    ]


def test_changed_names_are_normalized_and_deduplicated():
    status, body = _plan(
        resolved={"foo-bar": "1.0", "app": "1.0"},
        dependency_graph=[
            _edge("<root>", "", "app", "1.0"),
            _edge("app", "1.0", "foo-bar", "1.0"),
        ],
        changed=["Foo_Bar", "foo-bar", "FOO-BAR"],
    )
    assert status == 200
    assert body["summary"]["changed"] == 1
    packages = {p["name"]: p for p in body["packages"]}
    assert packages["foo-bar"]["changed"] is True
    assert packages["app"]["changed"] is False


def test_empty_changed_produces_an_empty_plan():
    status, body = _plan(
        resolved=CHAIN_RESOLVED,
        dependency_graph=CHAIN_GRAPH,
        changed=[],
    )
    assert status == 200
    assert body["summary"] == {
        "changed": 0,
        "missing": 0,
        "affected": 0,
        "groups": 0,
        "stages": 0,
    }
    assert body["missing"] == []
    assert body["stages"] == []
    assert body["packages"] == []


def test_roundtrip_from_resolve_response():
    resolve_body = client.post(
        "/resolve",
        json={
            "packages": {
                "app": {"1.0": {"dependencies": {"web": ">=2.0", "db": ""}}},
                "web": {
                    "2.0": {"dependencies": {"common": "<2.0"}},
                    "3.0": {"dependencies": {"common": ">=2.0"}},
                },
                "db": {"1.0": {"dependencies": {"common": "==1.5"}}},
                "common": {"1.5": {}, "2.0": {}},
            },
            "root": {"app": ""},
        },
    ).json()
    assert resolve_body["status"] == "ok"

    status, body = _plan(
        resolved=resolve_body["resolved"],
        dependency_graph=resolve_body["dependency_graph"],
        changed=["common"],
    )
    assert status == 200
    assert body["stages"] == [
        [{"packages": ["common"], "cyclic": False}],
        [
            {"packages": ["db"], "cyclic": False},
            {"packages": ["web"], "cyclic": False},
        ],
        [{"packages": ["app"], "cyclic": False}],
    ]
    packages = {p["name"]: p for p in body["packages"]}
    paths = [_names(p) for p in packages["app"]["paths"]]
    assert paths == [
        ["common", "db", "app"],
        ["common", "web", "app"],
    ]


def test_deterministic_results():
    payload = {
        "resolved": DIAMOND_RESOLVED,
        "dependency_graph": DIAMOND_GRAPH,
        "changed": ["shared"],
    }
    results = [client.post("/plan/rebuild", json=payload).json() for _ in range(5)]
    assert all(r == results[0] for r in results)


# ---------------------------------------------------------------------------
# 400s
# ---------------------------------------------------------------------------


def test_invalid_resolved_version_returns_400():
    status, body = _plan(
        resolved={"x": "not-a-version"},
        dependency_graph=[],
        changed=["x"],
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    assert "PEP 440" in body["error"]["message"]


def test_dangling_target_returns_400():
    status, body = _plan(
        resolved={"app": "1.0"},
        dependency_graph=[_edge("<root>", "", "ghost", "1.0")],
        changed=["app"],
    )
    assert status == 400
    assert "dangling" in body["error"]["message"]


def test_dangling_source_returns_400():
    status, body = _plan(
        resolved={"app": "1.0"},
        dependency_graph=[
            _edge("<root>", "", "app", "1.0"),
            _edge("ghost", "1.0", "app", "1.0"),
        ],
        changed=["app"],
    )
    assert status == 400
    assert "dangling" in body["error"]["message"]


def test_edge_version_inconsistent_with_resolved_returns_400():
    status, body = _plan(
        resolved={"app": "1.0"},
        dependency_graph=[_edge("<root>", "", "app", "2.0")],
        changed=["app"],
    )
    assert status == 400
    assert "inconsistent" in body["error"]["message"]


def test_root_edge_with_source_version_returns_400():
    status, body = _plan(
        resolved={"app": "1.0"},
        dependency_graph=[_edge("<root>", "1.0", "app", "1.0")],
        changed=["app"],
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"


def test_root_in_changed_returns_400():
    status, body = _plan(
        resolved={"app": "1.0"},
        dependency_graph=[_edge("<root>", "", "app", "1.0")],
        changed=["<root>"],
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"


def test_extra_fields_rejected():
    response = client.post(
        "/plan/rebuild",
        json={"resolved": {}, "dependency_graph": [], "changed": [], "nope": 1},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Differential testing against brute-force references
# ---------------------------------------------------------------------------


def _brute_distances(packages, edges, changed):
    """BFS distances from the changed set along reverse dependency edges."""
    reverse = {p: set() for p in packages}
    for src, tgt in edges:
        if src != "<root>":
            reverse[tgt].add(src)
    dist = {}
    queue = collections.deque()
    for seed in sorted(set(changed)):
        if seed in packages and seed not in dist:
            dist[seed] = 0
            queue.append(seed)
    while queue:
        node = queue.popleft()
        for nxt in reverse[node]:
            if nxt not in dist:
                dist[nxt] = dist[node] + 1
                queue.append(nxt)
    return dist


def _brute_sccs(nodes, edges):
    """Strongly connected components via mutual reachability."""

    def reach(start):
        seen = {start}
        stack = [start]
        while stack:
            node = stack.pop()
            for nxt in forward[node]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    forward = {n: set() for n in nodes}
    for src, tgt in edges:
        if src in forward and tgt in forward:
            forward[src].add(tgt)
    components = set()
    unseen = set(nodes)
    for node in nodes:
        if node not in unseen:
            continue
        component = frozenset(m for m in reach(node) if node in reach(m))
        components.add(component)
        unseen -= component
    return components


def test_random_graphs_match_brute_force():
    rng = random.Random(20260918)
    for case in range(150):
        count = rng.randint(1, 7)
        packages = [f"p{i}" for i in range(count)]
        resolved = {p: "1.0" for p in packages}
        edges = set()
        for src in packages:
            for tgt in packages:
                if rng.random() < 0.25:
                    edges.add((src, tgt))
        for p in packages:  # root edges are dropped by the planner
            if rng.random() < 0.5:
                edges.add(("<root>", p))
        changed = [p for p in packages if rng.random() < 0.4]
        if rng.random() < 0.3:
            changed.append("not-installed")

        graph = [
            _edge(src, "" if src == "<root>" else "1.0", tgt, "1.0")
            for src, tgt in sorted(edges)
        ]
        status, body = _plan(
            resolved=resolved, dependency_graph=graph, changed=changed
        )
        assert status == 200

        dist = _brute_distances(packages, edges, changed)
        affected = set(dist)
        inner = {(s, t) for s, t in edges if s in affected and t in affected}

        # Affected set, distances and missing entries.
        assert {p["name"] for p in body["packages"]} == affected
        assert body["summary"]["affected"] == len(affected)
        assert body["missing"] == sorted(
            {c for c in changed if c not in resolved}
        )
        by_name = {p["name"]: p for p in body["packages"]}
        for name, entry in by_name.items():
            assert entry["distance"] == dist[name]
            assert entry["changed"] == (name in set(changed))

        # Groups are exactly the SCCs of the affected subgraph.
        plan_groups = [g["packages"] for stage in body["stages"] for g in stage]
        assert {frozenset(g) for g in plan_groups} == _brute_sccs(affected, inner)
        for stage in body["stages"]:
            for group in stage:
                assert group["cyclic"] == (
                    len(group["packages"]) > 1
                    or (group["packages"][0], group["packages"][0]) in inner
                )

        # Stage consistency: dependencies always land in earlier stages.
        group_of = {name: tuple(g) for g in plan_groups for name in g}
        stage_of = {
            tuple(g["packages"]): i
            for i, stage in enumerate(body["stages"])
            for g in stage
        }
        for src, tgt in inner:
            if group_of[src] != group_of[tgt]:
                assert stage_of[group_of[src]] > stage_of[group_of[tgt]]
        for name, entry in by_name.items():
            assert entry["stage"] == stage_of[group_of[name]]
            assert tuple(entry["group"]) == group_of[name]

        # Every reported path is a genuine shortest reverse path.
        forward = {(s, t) for s, t in edges if s != "<root>"}
        for name, entry in by_name.items():
            assert entry["paths"], name
            for path in entry["paths"]:
                names = _names(path)
                assert names[0] in set(changed) & set(packages)
                assert names[-1] == name
                assert [dist[n] for n in names] == list(range(len(names)))
                assert len(names) == dist[name] + 1
                assert all(
                    (names[i + 1], names[i]) in forward
                    for i in range(len(names) - 1)
                )

        # Identical input -> identical plan.
        again = client.post(
            "/plan/rebuild",
            json={
                "resolved": resolved,
                "dependency_graph": graph,
                "changed": changed,
            },
        ).json()
        assert again == body
