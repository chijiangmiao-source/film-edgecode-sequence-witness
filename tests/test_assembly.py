"""Algorithm-level tests.

Small instances are cross-validated against exhaustive enumeration of every
assembly string (the tests may enumerate; the implementation must not).
"""

from __future__ import annotations

import random
import time
from collections import Counter

import pytest

from app.assembly import (
    assemble,
    build_graph,
    euler_string,
    find_obstruction,
    validate_assembly,
)


def brute_force_assemblies(k: int, start: str, fragments: list[str]) -> set[str]:
    """Enumerate every assembly string by exhaustive DFS (tiny inputs only)."""
    n = len(fragments)
    remaining: dict[str, Counter] = {}
    for frag in fragments:
        remaining.setdefault(frag[: k - 1], Counter())[frag[1:]] += 1
    found: set[str] = set()

    def dfs(vertex: str, text: str) -> None:
        if len(text) == (k - 1) + n:
            found.add(text)
            return
        for nxt, count in remaining.get(vertex, {}).items():
            if count:
                remaining[vertex][nxt] -= 1
                dfs(nxt, text + nxt[-1])
                remaining[vertex][nxt] += 1

    dfs(start, start)
    return found


def random_walk_instance(rng: random.Random, k: int, n: int, alphabet: str):
    """Fragments of a random walk over (k-1)-mers: feasible by construction."""
    start = "".join(rng.choice(alphabet) for _ in range(k - 1))
    vertex = start
    fragments = []
    for _ in range(n):
        nxt = vertex[1:] + rng.choice(alphabet)
        fragments.append(vertex + nxt[-1])
        vertex = nxt
    return start, fragments


# ---------------------------------------------------------------------------
# deterministic cases
# ---------------------------------------------------------------------------


def test_empty_fragment_list_is_unique():
    result = assemble(3, "AB", [])
    assert result.status == "unique"
    assert result.assembly == "AB"


def test_single_fragment():
    result = assemble(3, "AB", ["ABC"])
    assert result.status == "unique"
    assert result.assembly == "ABC"


def test_simple_cycle_is_unique():
    result = assemble(3, "AB", ["ABC", "BCA", "CAB"])
    assert result.status == "unique"
    assert result.assembly == "ABCAB"


def test_parallel_identical_fragments_are_not_ambiguity():
    # Swapping the two copies of "ABC" changes nothing: still unique.
    result = assemble(3, "AB", ["ABC", "ABC", "BCA", "CAB"])
    assert result.status == "unique"
    assert result.assembly == "ABCABC"


def test_figure_eight_is_ambiguous_with_extremal_witnesses():
    # Two loops rejoining at AB: AB->BA->AB and AB->BC->CA->AB.
    result = assemble(3, "AB", ["ABA", "BAB", "ABC", "BCA", "CAB"])
    assert result.status == "ambiguous"
    assert result.min_witness == "ABABCAB"
    assert result.max_witness == "ABCABAB"
    assert result.min_witness < result.max_witness


def test_self_loops():
    result = assemble(3, "AA", ["AAA", "AAA"])
    assert result.status == "unique"
    assert result.assembly == "AAAA"


def test_digits_are_valid_characters():
    result = assemble(4, "A01", ["A01B", "01B2", "1B2A"])
    assert result.status == "unique"
    assert result.assembly == "A01B2A"


def test_max_k():
    fragments = ["ABCDEFGHIJKL", "BCDEFGHIJKLM", "CDEFGHIJKLMA"]
    result = assemble(12, "ABCDEFGHIJK", fragments)
    assert result.status == "unique"
    assert result.assembly == "ABCDEFGHIJKLMA"


def test_degree_obstruction():
    # out(AB)=2, in(BC)=in(BD)=1: no Eulerian trail exists at all.
    result = assemble(3, "AB", ["ABC", "ABD"])
    assert result.status == "impossible"
    assert result.reason == "degree"
    vertices = {v["vertex"]: v for v in result.evidence["imbalanced_vertices"]}
    assert vertices["AB"]["balance"] == 2
    assert vertices["BC"]["balance"] == -1
    assert vertices["BD"]["balance"] == -1


def test_start_mismatch_reports_required_start():
    result = assemble(3, "BC", ["ABC", "BCD"])
    assert result.status == "impossible"
    assert result.reason == "start"
    assert result.evidence["given_start"] == "BC"
    assert result.evidence["required_start"] == "AB"


def test_isolated_start_vertex():
    result = assemble(3, "ZZ", ["ABC", "BCA", "CAB"])
    assert result.status == "impossible"
    assert result.reason == "start"
    assert result.evidence["required_start"] is None
    assert result.evidence["start_out_degree"] == 0


def test_disconnected_graph():
    fragments = ["ABC", "BCA", "CAB", "DEF", "EFD", "FDE"]
    result = assemble(3, "AB", fragments)
    assert result.status == "impossible"
    assert result.reason == "connectivity"
    assert result.evidence["component_count"] == 2
    assert sum(c["edge_count"] for c in result.evidence["components"]) == 6


def test_euler_string_extremes_are_ordered():
    graph = build_graph(3, "AB", ["ABA", "BAB", "ABC", "BCA", "CAB"])
    assert find_obstruction(graph) is None
    lo = euler_string(graph, smallest=True)
    hi = euler_string(graph, smallest=False)
    assert lo <= hi


# ---------------------------------------------------------------------------
# brute-force cross-validation on small instances
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(150))
def test_walk_instances_match_brute_force(seed: int):
    rng = random.Random(seed)
    k = rng.choice([3, 4])
    n = rng.randint(1, 9)
    start, fragments = random_walk_instance(rng, k, n, "AB0")
    expected = brute_force_assemblies(k, start, fragments)
    assert expected, "a walk always yields at least one assembly"

    result = assemble(k, start, fragments)
    if len(expected) == 1:
        assert result.status == "unique"
        assert result.assembly == min(expected)
    else:
        assert result.status == "ambiguous"
        assert result.min_witness == min(expected)
        assert result.max_witness == max(expected)
        assert validate_assembly(k, start, fragments, result.min_witness)["valid"]
        assert validate_assembly(k, start, fragments, result.max_witness)["valid"]


@pytest.mark.parametrize("seed", range(150, 350))
def test_arbitrary_multisets_match_brute_force(seed: int):
    rng = random.Random(seed)
    k = 3
    n = rng.randint(1, 8)
    alphabet = "AB"
    start = "".join(rng.choice(alphabet) for _ in range(k - 1))
    fragments = ["".join(rng.choice(alphabet) for _ in range(k)) for _ in range(n)]
    expected = brute_force_assemblies(k, start, fragments)

    result = assemble(k, start, fragments)
    if not expected:
        assert result.status == "impossible"
        assert result.reason in {"degree", "start", "connectivity"}
    elif len(expected) == 1:
        assert result.status == "unique"
        assert result.assembly == min(expected)
    else:
        assert result.status == "ambiguous"
        assert result.min_witness == min(expected)
        assert result.max_witness == max(expected)


# ---------------------------------------------------------------------------
# scale: 20 000 fragments
# ---------------------------------------------------------------------------


def test_twenty_thousand_fragments_unique_scale():
    rng = random.Random(20260915)
    k = 12
    start, fragments = random_walk_instance(
        rng, k, 20_000, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    )
    t0 = time.perf_counter()
    result = assemble(k, start, fragments)
    elapsed = time.perf_counter() - t0
    assert result.status in {"unique", "ambiguous"}
    if result.status == "unique":
        assert validate_assembly(k, start, fragments, result.assembly)["valid"]
    else:
        assert result.min_witness < result.max_witness
        assert validate_assembly(k, start, fragments, result.min_witness)["valid"]
        assert validate_assembly(k, start, fragments, result.max_witness)["valid"]
    assert elapsed < 10.0


def test_twenty_thousand_fragments_ambiguous_scale():
    rng = random.Random(7)
    k = 4
    start, fragments = random_walk_instance(rng, k, 20_000, "AB")
    result = assemble(k, start, fragments)
    assert result.status == "ambiguous"
    assert result.min_witness < result.max_witness
    assert validate_assembly(k, start, fragments, result.min_witness)["valid"]
    assert validate_assembly(k, start, fragments, result.max_witness)["valid"]


# ---------------------------------------------------------------------------
# validate_assembly (witness re-computation)
# ---------------------------------------------------------------------------


def test_validate_assembly_accepts_valid():
    outcome = validate_assembly(3, "AB", ["ABC", "BCA", "CAB"], "ABCAB")
    assert outcome["valid"] is True
    assert outcome["candidate_length"] == 5


def test_validate_assembly_rejects_wrong_length():
    outcome = validate_assembly(3, "AB", ["ABC"], "ABCD")
    assert outcome["valid"] is False
    assert outcome["reason"] == "length_mismatch"


def test_validate_assembly_rejects_bad_character():
    outcome = validate_assembly(3, "AB", ["ABC"], "ABc")
    assert outcome["valid"] is False
    assert outcome["reason"] == "invalid_character"
    assert outcome["detail"]["index"] == 2


def test_validate_assembly_rejects_wrong_start():
    outcome = validate_assembly(3, "AB", ["ABC"], "BBC")
    assert outcome["valid"] is False
    assert outcome["reason"] == "start_prefix_mismatch"


def test_validate_assembly_rejects_unavailable_fragment():
    outcome = validate_assembly(3, "AB", ["ABC", "BCA", "CAB"], "ABCBA")
    assert outcome["valid"] is False
    assert outcome["reason"] == "fragment_unavailable"
    assert outcome["detail"]["index"] == 1
    assert outcome["detail"]["window"] == "BCB"


def test_validate_assembly_respects_multiplicity():
    # Candidate longer than (k-1)+n is rejected on length alone.
    outcome = validate_assembly(3, "AB", ["ABC", "BCA"], "ABCAB")
    assert outcome["valid"] is False
    assert outcome["reason"] == "length_mismatch"
    # "ABCA" needs fragments ABC and BCA, but the multiset holds ABC and CAB.
    outcome = validate_assembly(3, "AB", ["ABC", "CAB"], "ABCA")
    assert outcome["valid"] is False
    assert outcome["reason"] == "fragment_unavailable"
    assert outcome["detail"]["index"] == 1
    assert outcome["detail"]["window"] == "BCA"
