"""Core assembly algorithm for film edge-code reconstruction.

Model
-----
Every fragment is a string of exactly ``k`` characters over the alphabet
``A-Z0-9``.  A fragment ``f`` induces one directed edge

    f[0 : k-1]  --f-->  f[1 : k]

between two ``(k-1)``-mers, so the fragment multiset defines a directed
multigraph.  Assembling the reel means walking a trail that starts at the
given ``start`` prefix and traverses every edge exactly once -- an Eulerian
trail.  The complete edge-code string is ``start`` followed by the last
character of every traversed fragment, hence has length ``(k-1) + n``.

Uniqueness is decided without enumerating permutations: the
lexicographically (ASCII) smallest and largest feasible assemblies are
constructed directly by ordered Hierholzer walks.  Every feasible assembly
lies between those two extremes, so the assembly is unique iff the extremes
coincide; otherwise the two extremes themselves are the ambiguity witnesses.
Parallel edges always carry identical labels here -- the label of an edge is
determined by its endpoints -- so permuting copies of the same fragment can
never change the assembled string and is therefore never ambiguity.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")

# Evidence payloads are capped so pathological inputs cannot produce
# unbounded responses; the accompanying counts always report true totals.
MAX_IMBALANCES_IN_EVIDENCE = 50
MAX_COMPONENTS_IN_EVIDENCE = 20
MAX_COMPONENT_SAMPLES = 12


@dataclass(frozen=True)
class Graph:
    """Directed multigraph of (k-1)-mers built from a fragment multiset."""

    start: str
    edge_count: int
    out_adj: dict[str, list[str]]  # vertex -> multiset of successors (duplicates kept)
    in_degree: dict[str, int]
    out_degree: dict[str, int]


def build_graph(k: int, start: str, fragments: list[str]) -> Graph:
    out_adj: dict[str, list[str]] = {}
    in_degree: dict[str, int] = {}
    out_degree: dict[str, int] = {}
    for frag in fragments:
        u, v = frag[: k - 1], frag[1:]
        out_adj.setdefault(u, []).append(v)
        out_degree[u] = out_degree.get(u, 0) + 1
        in_degree[v] = in_degree.get(v, 0) + 1
    return Graph(
        start=start,
        edge_count=len(fragments),
        out_adj=out_adj,
        in_degree=in_degree,
        out_degree=out_degree,
    )


def _weak_components(graph: Graph) -> list[list[str]]:
    """Weakly connected components of the non-zero-degree subgraph.

    Returned in deterministic order: larger components first, ties broken by
    the smallest vertex name.
    """
    neighbours: dict[str, set[str]] = {}
    for u, targets in graph.out_adj.items():
        bucket = neighbours.setdefault(u, set())
        for v in targets:
            bucket.add(v)
            neighbours.setdefault(v, set()).add(u)
    seen: set[str] = set()
    components: list[list[str]] = []
    for root in neighbours:
        if root in seen:
            continue
        seen.add(root)
        stack = [root]
        component: list[str] = []
        while stack:
            x = stack.pop()
            component.append(x)
            for y in neighbours[x]:
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        components.append(component)
    components.sort(key=lambda c: (-len(c), min(c)))
    return components


def find_obstruction(graph: Graph) -> dict | None:
    """Return structured graph evidence if no Eulerian trail from the
    designated start exists, else ``None``.

    Checks, in order: Euler's degree condition, the role of the designated
    start vertex, and weak connectivity of the non-zero-degree vertices.
    """
    n = graph.edge_count
    if n == 0:
        return None  # the empty assembly (just the start prefix) always works

    start = graph.start
    start_out = graph.out_degree.get(start, 0)
    start_in = graph.in_degree.get(start, 0)

    plus: list[str] = []    # out - in == +1  (forced trail start)
    minus: list[str] = []   # out - in == -1  (forced trail end)
    broken: list[str] = []  # |out - in| >= 2  (no Eulerian trail at all)
    for vertex in set(graph.out_degree) | set(graph.in_degree):
        delta = graph.out_degree.get(vertex, 0) - graph.in_degree.get(vertex, 0)
        if delta == 1:
            plus.append(vertex)
        elif delta == -1:
            minus.append(vertex)
        elif delta != 0:
            broken.append(vertex)

    if broken or len(plus) > 1 or len(minus) > 1:
        imbalanced = sorted(plus + minus + broken)
        return {
            "kind": "degree",
            "total_imbalanced": len(imbalanced),
            "imbalanced_vertices": [
                {
                    "vertex": v,
                    "in_degree": graph.in_degree.get(v, 0),
                    "out_degree": graph.out_degree.get(v, 0),
                    "balance": graph.out_degree.get(v, 0) - graph.in_degree.get(v, 0),
                }
                for v in imbalanced[:MAX_IMBALANCES_IN_EVIDENCE]
            ],
            "note": (
                "an Eulerian trail needs every vertex balanced (in == out), "
                "except optionally one start vertex with out-in == 1 and one "
                "end vertex with in-out == 1"
            ),
        }

    if start_in + start_out == 0:
        return {
            "kind": "start",
            "given_start": start,
            "required_start": None,
            "start_in_degree": 0,
            "start_out_degree": 0,
            "detail": (
                f"start prefix {start!r} is not the prefix of any fragment; "
                f"no assembly can begin there while {n} fragment(s) remain"
            ),
        }

    if plus and plus[0] != start:
        return {
            "kind": "start",
            "given_start": start,
            "required_start": plus[0],
            "start_in_degree": start_in,
            "start_out_degree": start_out,
            "detail": (
                f"degree balance forces every Eulerian trail to begin at "
                f"{plus[0]!r}, not at the requested start {start!r}"
            ),
        }

    components = _weak_components(graph)
    if len(components) > 1:
        return {
            "kind": "connectivity",
            "component_count": len(components),
            "components": [
                {
                    "vertex_count": len(component),
                    "edge_count": sum(graph.out_degree.get(v, 0) for v in component),
                    "sample_vertices": sorted(component)[:MAX_COMPONENT_SAMPLES],
                }
                for component in components[:MAX_COMPONENTS_IN_EVIDENCE]
            ],
            "note": (
                "all vertices with non-zero degree must lie in a single "
                "weakly connected component"
            ),
        }
    return None


def euler_string(graph: Graph, *, smallest: bool) -> str:
    """Lexicographically extreme complete edge-code string.

    Ordered Hierholzer walk: always follow the smallest (or largest, when
    ``smallest=False``) remaining outgoing edge; vertices are emitted on
    backtracking and the reversed emission order is an Eulerian trail.
    Every outgoing edge of a vertex ``u`` ends at ``u[1:] + c`` for some
    character ``c``, so ordering successors by name is the same as ordering
    them by edge-label character.  The greedy extremal choice together with
    the standard exchange argument for Hierholzer's algorithm yields the
    lexicographically minimal/maximal assembly over *all* Eulerian trails
    from the start vertex -- no permutation enumeration involved.
    """
    adjacency = {
        u: sorted(targets, reverse=not smallest)
        for u, targets in graph.out_adj.items()
    }
    cursor = {u: 0 for u in adjacency}
    stack = [graph.start]
    emission: list[str] = []
    while stack:
        vertex = stack[-1]
        targets = adjacency.get(vertex)
        if targets is None:
            emission.append(vertex)
            stack.pop()
            continue
        i = cursor[vertex]
        if i < len(targets):
            cursor[vertex] = i + 1
            stack.append(targets[i])
        else:
            emission.append(vertex)
            stack.pop()
    route = emission[::-1]
    # route[0] is the start vertex; every later vertex contributes the last
    # character of the fragment that led into it.
    return graph.start + "".join(vertex[-1] for vertex in route[1:])


@dataclass(frozen=True)
class AssemblyResult:
    status: str  # "unique" | "ambiguous" | "impossible"
    fragment_count: int
    assembly: str | None = None     # the only assembly, when unique
    min_witness: str | None = None  # ASCII-smallest assembly, when ambiguous
    max_witness: str | None = None  # ASCII-largest assembly, when ambiguous
    reason: str | None = None       # "degree" | "start" | "connectivity"
    evidence: dict | None = None    # structured graph evidence, when impossible


def assemble(k: int, start: str, fragments: list[str]) -> AssemblyResult:
    """Assemble a fragment multiset from ``start``; decide uniqueness."""
    graph = build_graph(k, start, fragments)
    obstruction = find_obstruction(graph)
    if obstruction is not None:
        return AssemblyResult(
            status="impossible",
            fragment_count=graph.edge_count,
            reason=obstruction["kind"],
            evidence=obstruction,
        )
    smallest = euler_string(graph, smallest=True)
    largest = euler_string(graph, smallest=False)
    if smallest == largest:
        return AssemblyResult(
            status="unique",
            fragment_count=graph.edge_count,
            assembly=smallest,
        )
    return AssemblyResult(
        status="ambiguous",
        fragment_count=graph.edge_count,
        min_witness=smallest,
        max_witness=largest,
    )


def validate_assembly(
    k: int, start: str, fragments: list[str], candidate: str
) -> dict:
    """Re-check a claimed assembly character by character.

    Returns ``{"valid": True, ...}`` or ``{"valid": False, "reason": ...,
    "detail": {...}}`` pinpointing the first offending position.
    """
    n = len(fragments)
    expected_length = (k - 1) + n
    if len(candidate) != expected_length:
        return {
            "valid": False,
            "reason": "length_mismatch",
            "detail": {
                "expected_length": expected_length,
                "actual_length": len(candidate),
            },
        }
    for i, ch in enumerate(candidate):
        if ch not in ALPHABET:
            return {
                "valid": False,
                "reason": "invalid_character",
                "detail": {"index": i, "character": ch},
            }
    if not candidate.startswith(start):
        mismatch_at = next(
            (i for i, (a, b) in enumerate(zip(candidate, start)) if a != b),
            min(len(candidate), len(start)),
        )
        return {
            "valid": False,
            "reason": "start_prefix_mismatch",
            "detail": {
                "index": mismatch_at,
                "expected_prefix": start,
                "candidate_prefix": candidate[: k - 1],
            },
        }
    remaining = Counter(fragments)
    for i in range(n):
        window = candidate[i : i + k]
        if remaining.get(window, 0) == 0:
            return {
                "valid": False,
                "reason": "fragment_unavailable",
                "detail": {
                    "index": i,
                    "window": window,
                    "message": (
                        f"fragment {window!r} at offset {i} is not present in "
                        f"the remaining fragment multiset"
                    ),
                },
            }
        remaining[window] -= 1
    return {"valid": True, "candidate_length": len(candidate), "fragment_count": n}
