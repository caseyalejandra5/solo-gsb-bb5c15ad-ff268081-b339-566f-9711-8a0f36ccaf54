"""Deterministic rebuild plan for locally changed packages.

Consumes the ``resolved`` mapping and ``dependency_graph`` edges emitted by a
successful ``/resolve`` call plus a list of locally changed package names,
and works out what has to be rebuilt, in which order:

* Every package that transitively depends on a changed package is affected;
  the affected set is discovered by walking the *reverse* dependency edges
  outward from the changed set.
* Cyclic dependencies are compressed into strongly connected components:
  the packages of a component mutually depend on each other and must be
  rebuilt in the same batch.
* Components are scheduled into stages, dependencies before dependents.
  Components in the same stage do not depend on each other and may be
  rebuilt in parallel.
* Every affected package also reports all shortest reverse paths to the
  nearest changed package (why it is being rebuilt).  Paths are enumerated
  over the BFS level DAG exactly like the vulnerability audit, so cycles
  can never cause unbounded traversal and every reported path is simple.

Changed entries that are not part of the resolved set are reported as
``missing`` and take no part in the traversal.

Validation of ``resolved`` and ``dependency_graph`` reuses the audit
criteria (:func:`app.audit.parse_resolved` and :func:`app.audit.parse_graph`),
so name normalization, dangling-edge and version-consistency checks behave
identically.  All output ordering is deterministic: identical input always
yields identical groups, stages and path order.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from packaging.utils import canonicalize_name

from .audit import _EdgeLike, parse_graph, parse_resolved
from .resolver import ROOT, ResolutionInputError

# ---------------------------------------------------------------------------
# Input validation (all failures -> ResolutionInputError -> HTTP 400)
# ---------------------------------------------------------------------------


def parse_changed(raw: Iterable[str]) -> list[str]:
    """Normalize the changed package list.

    Names go through the same canonicalization as every other input and
    ``<root>`` stays reserved; duplicate entries collapse.  Returns the
    sorted set of canonical names.
    """
    names: set[str] = set()
    for entry in raw:
        canonical = canonicalize_name(entry)
        if canonical == ROOT:
            raise ResolutionInputError(
                f"changed: {ROOT!r} is reserved and cannot name a package"
            )
        names.add(canonical)
    return sorted(names)


# ---------------------------------------------------------------------------
# Reverse graph and affected set
# ---------------------------------------------------------------------------


def _reverse_adjacency(adjacency: dict[str, list[str]]) -> dict[str, list[str]]:
    """Direct dependents of each package; the synthetic ``<root>`` is dropped.

    ``parse_graph`` already deduplicates and sorts the forward edges, so the
    reverse lists are unique as well and only need sorting.
    """
    reverse: dict[str, list[str]] = {}
    for source, targets in adjacency.items():
        if source == ROOT:
            continue
        for target in targets:
            reverse.setdefault(target, []).append(source)
    for node in reverse:
        reverse[node].sort()
    return reverse


def _reverse_distances(
    reverse: dict[str, list[str]], seeds: list[str]
) -> dict[str, int]:
    """Hop distance from the nearest changed package along reverse edges."""
    distance: dict[str, int] = {}
    queue: deque[str] = deque()
    for seed in seeds:
        if seed not in distance:
            distance[seed] = 0
            queue.append(seed)
    while queue:
        node = queue.popleft()
        for dependent in reverse.get(node, ()):
            if dependent not in distance:
                distance[dependent] = distance[node] + 1
                queue.append(dependent)
    return distance


def _shortest_reverse_paths(
    adjacency: dict[str, list[str]], distance: dict[str, int], target: str
) -> list[list[str]]:
    """Every shortest changed -> ``target`` path along reverse edges.

    Enumerated from ``target`` towards distance 0, following forward edges
    whose other endpoint sits exactly one BFS level closer to the changed
    set.  The search space is therefore a DAG even when the underlying
    graph has cycles: traversal always terminates and no path repeats a
    node.  Paths are deduplicated and sorted by node sequence.
    """
    if target not in distance:
        return []
    paths: list[list[str]] = []
    stack: list[tuple[str, list[str]]] = [(target, [target])]
    while stack:
        node, suffix = stack.pop()
        if distance[node] == 0:
            paths.append(list(reversed(suffix)))
            continue
        for dependency in adjacency.get(node, ()):
            if distance.get(dependency) == distance[node] - 1:
                stack.append((dependency, suffix + [dependency]))
    unique = {tuple(path) for path in paths}
    return [list(path) for path in sorted(unique)]


# ---------------------------------------------------------------------------
# Strongly connected components (cycle compression)
# ---------------------------------------------------------------------------


def _strongly_connected_components(
    nodes: list[str], adjacency: dict[str, list[str]]
) -> list[list[str]]:
    """Tarjan's algorithm, iterative so deep graphs cannot exhaust the stack.

    ``nodes`` must be sorted and every adjacency list sorted; the traversal
    order (and therefore the output) is then fully deterministic.  Each
    returned component is sorted.
    """
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for start in nodes:
        if start in index_of:
            continue
        work: list[list[Any]] = [[start, 0]]  # [node, next neighbor index]
        while work:
            frame = work[-1]
            node, child = frame[0], frame[1]
            if child == 0:
                index_of[node] = lowlink[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            descended = False
            neighbors = adjacency.get(node, ())
            while child < len(neighbors):
                nxt = neighbors[child]
                child += 1
                if nxt not in index_of:
                    frame[1] = child
                    work.append([nxt, 0])
                    descended = True
                    break
                if nxt in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[nxt])
            if descended:
                continue
            if lowlink[node] == index_of[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(sorted(component))
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
    return components


def _group_levels(deps: list[set[int]]) -> list[int]:
    """Stage index of every group: 0 without dependencies, else 1 + the
    highest stage of a group it depends on (longest-path layering).

    The condensation of a graph is always a DAG, so the Kahn-style
    propagation terminates; the resulting levels are order-independent.
    """
    dependents: dict[int, list[int]] = {}
    remaining = [len(group_deps) for group_deps in deps]
    for group, group_deps in enumerate(deps):
        for dependency in group_deps:
            dependents.setdefault(dependency, []).append(group)
    levels = [0] * len(deps)
    ready: deque[int] = deque(
        group for group, pending in enumerate(remaining) if pending == 0
    )
    while ready:
        node = ready.popleft()
        for dependent in dependents.get(node, ()):
            levels[dependent] = max(levels[dependent], levels[node] + 1)
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)
    return levels


# ---------------------------------------------------------------------------
# Rebuild plan
# ---------------------------------------------------------------------------


def run_rebuild_plan(
    resolved_raw: dict[str, str],
    edges_raw: Iterable[_EdgeLike],
    changed_raw: Iterable[str],
) -> dict[str, Any]:
    """Build the deterministic rebuild plan for the changed packages."""
    resolved = parse_resolved(resolved_raw)
    adjacency = parse_graph(edges_raw, resolved)
    changed = parse_changed(changed_raw)
    changed_set = set(changed)

    installed = [name for name in changed if name in resolved]
    missing = [name for name in changed if name not in resolved]

    reverse = _reverse_adjacency(adjacency)
    distance = _reverse_distances(reverse, installed)
    affected = sorted(distance)
    affected_set = set(affected)

    # Forward subgraph induced by the affected packages: dependencies that
    # are not themselves affected are already built and need no stage.
    subgraph = {
        node: [t for t in adjacency.get(node, ()) if t in affected_set]
        for node in affected
    }

    components = _strongly_connected_components(affected, subgraph)
    groups = sorted(components, key=lambda members: tuple(members))
    group_of: dict[str, int] = {}
    for idx, members in enumerate(groups):
        for member in members:
            group_of[member] = idx

    # Dependencies between groups (the condensation DAG).
    deps: list[set[int]] = [set() for _ in groups]
    for node in affected:
        own = group_of[node]
        for target in subgraph[node]:
            other = group_of[target]
            if other != own:
                deps[own].add(other)
    levels = _group_levels(deps)

    cyclic: list[bool] = []
    for members in groups:
        if len(members) > 1:
            cyclic.append(True)
        else:
            (only,) = members
            cyclic.append(only in subgraph.get(only, ()))

    staged: dict[int, list[int]] = {}
    for idx in range(len(groups)):
        staged.setdefault(levels[idx], []).append(idx)
    stages = [
        [
            {"packages": groups[idx], "cyclic": cyclic[idx]}
            for idx in staged[level]  # group order follows the sorted groups
        ]
        for level in sorted(staged)
    ]

    packages: list[dict[str, Any]] = []
    for name in affected:
        group = group_of[name]
        paths = _shortest_reverse_paths(adjacency, distance, name)
        packages.append(
            {
                "name": name,
                "version": str(resolved[name]),
                "changed": name in changed_set,
                "distance": distance[name],
                "stage": levels[group],
                "group": groups[group],
                "paths": [
                    [
                        {"name": hop, "version": str(resolved[hop])}
                        for hop in path
                    ]
                    for path in paths
                ],
            }
        )

    return {
        "status": "ok",
        "summary": {
            "changed": len(changed),
            "missing": len(missing),
            "affected": len(affected),
            "groups": len(groups),
            "stages": len(stages),
        },
        "missing": missing,
        "stages": stages,
        "packages": packages,
    }
