"""API-level tests through FastAPI's TestClient."""

from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def assemble(payload: dict):
    return client.post("/assemble", json=payload)


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unique_cycle():
    response = assemble({"k": 3, "start": "AB", "fragments": ["ABC", "BCA", "CAB"]})
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "unique",
        "assembly": "ABCAB",
        "length": 5,
        "fragment_count": 3,
    }


def test_empty_fragment_list():
    response = assemble({"k": 5, "start": "ABCD", "fragments": []})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unique"
    assert body["assembly"] == "ABCD"
    assert body["length"] == 4


def test_duplicates_are_preserved_by_count():
    # If duplicates were collapsed, the answer would be "ABCAB".
    response = assemble(
        {"k": 3, "start": "AB", "fragments": ["ABC", "ABC", "BCA", "CAB"]}
    )
    body = response.json()
    assert body["status"] == "unique"
    assert body["assembly"] == "ABCABC"


def test_ambiguous_figure_eight():
    response = assemble(
        {"k": 3, "start": "AB", "fragments": ["ABA", "BAB", "ABC", "BCA", "CAB"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ambiguous"
    lo, hi = body["witnesses"]["min"], body["witnesses"]["max"]
    assert (lo, hi) == ("ABABCAB", "ABCABAB")
    diff = body["first_difference"]
    assert diff == {"index": 2, "min": "A", "max": "C"}
    assert lo[: diff["index"]] == hi[: diff["index"]]
    # Both witnesses re-compute as valid assemblies of the same multiset.
    for witness in (lo, hi):
        outcome = client.post(
            "/verify",
            json={
                "k": 3,
                "start": "AB",
                "fragments": ["ABA", "BAB", "ABC", "BCA", "CAB"],
                "candidate": witness,
            },
        ).json()
        assert outcome["valid"] is True


# ---------------------------------------------------------------------------
# impossible cases with graph evidence
# ---------------------------------------------------------------------------


def test_impossible_degree():
    response = assemble({"k": 3, "start": "AB", "fragments": ["ABC", "ABD"]})
    body = response.json()
    assert body["status"] == "impossible"
    assert body["reason"] == "degree"
    assert body["evidence"]["kind"] == "degree"
    vertices = {v["vertex"]: v for v in body["evidence"]["imbalanced_vertices"]}
    assert vertices["AB"] == {
        "vertex": "AB",
        "in_degree": 0,
        "out_degree": 2,
        "balance": 2,
    }


def test_impossible_start():
    response = assemble({"k": 3, "start": "BC", "fragments": ["ABC", "BCD"]})
    body = response.json()
    assert body["status"] == "impossible"
    assert body["reason"] == "start"
    assert body["evidence"]["given_start"] == "BC"
    assert body["evidence"]["required_start"] == "AB"


def test_impossible_connectivity():
    response = assemble(
        {
            "k": 3,
            "start": "AB",
            "fragments": ["ABC", "BCA", "CAB", "DEF", "EFD", "FDE"],
        }
    )
    body = response.json()
    assert body["status"] == "impossible"
    assert body["reason"] == "connectivity"
    assert body["evidence"]["component_count"] == 2
    samples = [set(c["sample_vertices"]) for c in body["evidence"]["components"]]
    assert {"AB", "BC", "CA"} in samples
    assert {"DE", "EF", "FD"} in samples


# ---------------------------------------------------------------------------
# /verify
# ---------------------------------------------------------------------------


def test_verify_valid_candidate():
    response = client.post(
        "/verify",
        json={
            "k": 3,
            "start": "AB",
            "fragments": ["ABC", "BCA", "CAB"],
            "candidate": "ABCAB",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "valid": True,
        "candidate_length": 5,
        "fragment_count": 3,
    }


@pytest.mark.parametrize(
    "candidate, reason",
    [
        ("ABCABA", "length_mismatch"),
        ("ABCA", "length_mismatch"),
        ("ABcAB", "invalid_character"),
        ("BBCAB", "start_prefix_mismatch"),
        ("ABCBA", "fragment_unavailable"),
    ],
)
def test_verify_invalid_candidates(candidate: str, reason: str):
    response = client.post(
        "/verify",
        json={
            "k": 3,
            "start": "AB",
            "fragments": ["ABC", "BCA", "CAB"],
            "candidate": candidate,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["reason"] == reason
    assert isinstance(body["detail"], dict)


# ---------------------------------------------------------------------------
# structured errors
# ---------------------------------------------------------------------------


def assert_envelope(response, status_code: int, code: str):
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    return body["error"]


@pytest.mark.parametrize("k", [0, 2, 13, 100])
def test_k_out_of_range(k: int):
    error = assert_envelope(
        assemble({"k": k, "start": "AB", "fragments": ["ABC"]}),
        422,
        "VALIDATION_ERROR",
    )
    assert any("k" in detail["loc"] for detail in error["details"])


def test_k_wrong_type():
    assert_envelope(
        assemble({"k": "three", "start": "AB", "fragments": ["ABC"]}),
        422,
        "VALIDATION_ERROR",
    )


@pytest.mark.parametrize("k", ["3", "12", 3.0, 3.5, True, False, None, [3]])
def test_k_must_be_a_json_integer(k):
    # Numeric strings, floats and booleans are type errors, never coerced.
    error = assert_envelope(
        assemble({"k": k, "start": "AB", "fragments": ["ABC"]}),
        422,
        "VALIDATION_ERROR",
    )
    k_errors = [d for d in error["details"] if "k" in d["loc"]]
    assert k_errors and k_errors[0]["type"] in {"int_type", "int_from_float"}


def test_start_wrong_type():
    error = assert_envelope(
        assemble({"k": 3, "start": 12, "fragments": ["ABC"]}),
        422,
        "VALIDATION_ERROR",
    )
    assert any("start" in detail["loc"] for detail in error["details"])


def test_start_wrong_length():
    error = assert_envelope(
        assemble({"k": 3, "start": "ABC", "fragments": ["ABC"]}),
        422,
        "INVALID_START",
    )
    assert error["details"] == {"expected_length": 2, "actual_length": 3}


def test_start_invalid_characters():
    error = assert_envelope(
        assemble({"k": 3, "start": "ab", "fragments": ["ABC"]}),
        422,
        "INVALID_START",
    )
    assert error["details"]["invalid_characters"][0]["character"] == "a"


def test_fragment_wrong_length():
    error = assert_envelope(
        assemble({"k": 3, "start": "AB", "fragments": ["ABC", "AB"]}),
        422,
        "INVALID_FRAGMENTS",
    )
    problem = error["details"]["problems"][0]
    assert problem["index"] == 1
    assert problem["actual_length"] == 2


def test_fragment_invalid_characters():
    error = assert_envelope(
        assemble({"k": 3, "start": "AB", "fragments": ["abC"]}),
        422,
        "INVALID_FRAGMENTS",
    )
    assert error["details"]["problems"][0]["invalid_characters"] == ["a", "b"]


def test_too_many_fragments():
    payload = {"k": 3, "start": "AB", "fragments": ["ABC"] * 20_001}
    assert_envelope(assemble(payload), 422, "VALIDATION_ERROR")


def test_extra_field_rejected():
    assert_envelope(
        assemble({"k": 3, "start": "AB", "fragments": [], "debug": True}),
        422,
        "VALIDATION_ERROR",
    )


def test_missing_field():
    assert_envelope(assemble({"k": 3, "start": "AB"}), 422, "VALIDATION_ERROR")


def test_fragment_item_wrong_type():
    assert_envelope(
        assemble({"k": 3, "start": "AB", "fragments": ["ABC", 7]}),
        422,
        "VALIDATION_ERROR",
    )


def test_malformed_json():
    response = client.post(
        "/assemble",
        content="{not json",
        headers={"content-type": "application/json"},
    )
    assert_envelope(response, 422, "VALIDATION_ERROR")


def test_unknown_route():
    assert_envelope(client.post("/nope", json={}), 404, "NOT_FOUND")


def test_method_not_allowed():
    assert_envelope(client.get("/assemble"), 405, "METHOD_NOT_ALLOWED")


# ---------------------------------------------------------------------------
# end-to-end scale through HTTP
# ---------------------------------------------------------------------------


def test_large_request_through_api():
    rng = random.Random(99)
    k = 6
    alphabet = "ABCD0123"
    vertex = "".join(rng.choice(alphabet) for _ in range(k - 1))
    start = vertex
    fragments = []
    for _ in range(5_000):
        nxt = vertex[1:] + rng.choice(alphabet)
        fragments.append(vertex + nxt[-1])
        vertex = nxt
    response = assemble({"k": k, "start": start, "fragments": fragments})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"unique", "ambiguous"}
    assert body["fragment_count"] == 5_000
