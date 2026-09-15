"""Resumable ingestion: chunked submission, idempotent retries, conflicts,
transactional completion and restart recovery."""

from __future__ import annotations

import concurrent.futures
import random

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

# k=3, start="AB": unique assembly "ABCABC" (duplicate copies kept by count).
FRAGMENTS = ["ABC", "ABC", "BCA", "CAB"]
EXPECTED = len(FRAGMENTS)


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(db_path=str(tmp_path / "ingest.db")))


def create_task(
    client: TestClient, *, k: int = 3, start: str = "AB", expected_total: int = EXPECTED
) -> str:
    response = client.post(
        "/tasks", json={"k": k, "start": start, "expected_total": expected_total}
    )
    assert response.status_code == 201, response.text
    return response.json()["task_id"]


def submit(
    client: TestClient,
    task_id: str,
    index: int,
    fragments: list[str],
    complete: bool = False,
):
    return client.post(
        f"/tasks/{task_id}/chunks",
        json={"index": index, "fragments": fragments, "complete": complete},
    )


def random_walk(rng: random.Random, k: int, n: int, alphabet: str):
    vertex = "".join(rng.choice(alphabet) for _ in range(k - 1))
    start = vertex
    fragments = []
    for _ in range(n):
        nxt = vertex[1:] + rng.choice(alphabet)
        fragments.append(vertex + nxt[-1])
        vertex = nxt
    return start, fragments


# ---------------------------------------------------------------------------
# path 1: out-of-order backfill
# ---------------------------------------------------------------------------


def test_out_of_order_backfill_completes(client: TestClient):
    task_id = create_task(client)
    # Chunks arrive out of order: 1, then 2, then 0 (declaring completion).
    r = submit(client, task_id, 1, ["BCA"])
    assert r.status_code == 201
    assert r.json() == {
        "task_id": task_id,
        "index": 1,
        "accepted": 1,
        "received_total": 1,
    }
    r = submit(client, task_id, 2, ["CAB"])
    assert r.status_code == 201 and r.json()["received_total"] == 2
    r = submit(client, task_id, 0, ["ABC", "ABC"], complete=True)
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "status": "unique",
        "assembly": "ABCABC",
        "length": 6,
        "fragment_count": 4,
    }
    # The completion response is exactly the one-shot /assemble body.
    one_shot = client.post(
        "/assemble", json={"k": 3, "start": "AB", "fragments": FRAGMENTS}
    ).json()
    assert body == one_shot


def test_missing_chunks_block_completion_then_backfill(client: TestClient):
    task_id = create_task(client)
    submit(client, task_id, 0, ["ABC", "ABC"])
    r = submit(client, task_id, 2, ["CAB"], complete=True)  # index 1 missing
    assert r.status_code == 409
    error = r.json()["error"]
    assert error["code"] == "MISSING_CHUNKS"
    assert error["details"]["task_id"] == task_id
    assert error["details"]["index"] == 2
    assert error["details"]["missing_indices"] == [1]
    # The rejected completion stored nothing: chunk 2 can be re-sent after
    # backfilling the gap.
    submit(client, task_id, 1, ["BCA"])
    r = submit(client, task_id, 2, ["CAB"], complete=True)
    assert r.status_code == 200 and r.json()["assembly"] == "ABCABC"


def test_total_mismatch_blocks_completion(client: TestClient):
    task_id = create_task(client, expected_total=5)
    submit(client, task_id, 0, ["ABC", "ABC"])
    r = submit(client, task_id, 1, ["BCA", "CAB"], complete=True)  # 4 != 5
    assert r.status_code == 409
    error = r.json()["error"]
    assert error["code"] == "TOTAL_MISMATCH"
    assert error["details"]["received_total"] == 4
    assert error["details"]["expected_total"] == 5
    assert error["details"]["task_id"] == task_id


def test_ambiguous_completion_matches_one_shot(client: TestClient):
    fragments = ["ABA", "BAB", "ABC", "BCA", "CAB"]
    task_id = create_task(client, expected_total=len(fragments))
    submit(client, task_id, 0, fragments[:3])
    r = submit(client, task_id, 1, fragments[3:], complete=True)
    assert r.status_code == 200 and r.json()["status"] == "ambiguous"
    one_shot = client.post(
        "/assemble", json={"k": 3, "start": "AB", "fragments": fragments}
    ).json()
    assert r.json() == one_shot


# ---------------------------------------------------------------------------
# path 2: concurrent identical retries are written exactly once
# ---------------------------------------------------------------------------


def test_concurrent_identical_chunk_written_once(tmp_path):
    app = create_app(db_path=str(tmp_path / "ingest.db"))
    task_id = create_task(TestClient(app), expected_total=2)

    def send(_: int):
        # One client per thread; the app (and its SQLite file) is shared.
        return TestClient(app).post(
            f"/tasks/{task_id}/chunks",
            json={"index": 0, "fragments": ["ABC", "ABC"], "complete": False},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(send, range(8)))

    codes = sorted(r.status_code for r in responses)
    assert codes.count(201) == 1, codes  # exactly one request wrote the chunk
    assert codes.count(200) == 7, codes  # the rest replayed the acknowledgement
    assert len({r.text for r in responses}) == 1  # identical acknowledgements
    state = TestClient(app).get(f"/tasks/{task_id}").json()
    assert state["received_total"] == 2
    assert state["chunk_count"] == 1


# ---------------------------------------------------------------------------
# path 3: conflicting re-send
# ---------------------------------------------------------------------------


def test_conflicting_resend_returns_409_and_keeps_original(client: TestClient):
    task_id = create_task(client)
    first = submit(client, task_id, 0, ["ABC"])
    assert first.status_code == 201
    # Identical retry replays the stored acknowledgement.
    replay = submit(client, task_id, 0, ["ABC"])
    assert replay.status_code == 200
    assert replay.json() == first.json()
    # Different content on the same index is a locatable conflict.
    conflict = submit(client, task_id, 0, ["ABD"])
    assert conflict.status_code == 409
    error = conflict.json()["error"]
    assert error["code"] == "CHUNK_CONFLICT"
    assert error["details"]["task_id"] == task_id
    assert error["details"]["index"] == 0
    # The stored chunk is untouched.
    state = client.get(f"/tasks/{task_id}").json()
    assert state["received_total"] == 1
    assert state["chunk_count"] == 1


# ---------------------------------------------------------------------------
# path 4: resume and replay after a process restart
# ---------------------------------------------------------------------------


def test_restart_resume_and_replay(tmp_path):
    db_path = str(tmp_path / "ingest.db")
    first = TestClient(create_app(db_path=db_path))
    task_id = create_task(first)
    assert submit(first, task_id, 0, ["ABC", "ABC"]).status_code == 201
    assert submit(first, task_id, 1, ["BCA"]).status_code == 201

    # Simulate a process restart: a fresh application over the same file.
    second = TestClient(create_app(db_path=db_path))
    state = second.get(f"/tasks/{task_id}").json()
    assert state["status"] == "open"
    assert state["received_total"] == 3
    assert state["chunk_count"] == 2
    assert state["missing_indices"] == []
    assert state["result"] is None

    result = submit(second, task_id, 2, ["CAB"], complete=True)
    assert result.status_code == 200
    assert result.json()["assembly"] == "ABCABC"

    # The completed response replays identically after the restart.
    replay = submit(second, task_id, 2, ["CAB"], complete=True)
    assert replay.status_code == 200
    assert replay.json() == result.json()
    state = second.get(f"/tasks/{task_id}").json()
    assert state["status"] == "completed"
    assert state["result"] == result.json()


# ---------------------------------------------------------------------------
# further guarantees
# ---------------------------------------------------------------------------


def test_overflow_is_rejected_and_never_exceeds_expected(client: TestClient):
    task_id = create_task(client, expected_total=2)
    submit(client, task_id, 0, ["ABC", "ABC"])
    r = submit(client, task_id, 1, ["BCA"])
    assert r.status_code == 409
    error = r.json()["error"]
    assert error["code"] == "CHUNK_OVERFLOW"
    details = error["details"]
    assert details["task_id"] == task_id and details["index"] == 1
    assert details["received_total"] == 2 and details["expected_total"] == 2
    state = client.get(f"/tasks/{task_id}").json()
    assert state["received_total"] == 2  # unchanged


def test_write_after_completion_is_rejected(client: TestClient):
    task_id = create_task(client)
    submit(client, task_id, 0, ["ABC", "ABC"])
    submit(client, task_id, 1, ["BCA"])
    assert submit(client, task_id, 2, ["CAB"], complete=True).status_code == 200
    late = submit(client, task_id, 3, ["ABC"])
    assert late.status_code == 409
    error = late.json()["error"]
    assert error["code"] == "TASK_COMPLETED"
    assert error["details"]["task_id"] == task_id
    assert error["details"]["index"] == 3


def test_impossible_result_is_stored_and_replayed(client: TestClient):
    task_id = create_task(client, expected_total=2)
    submit(client, task_id, 0, ["ABC"])
    r = submit(client, task_id, 1, ["ABD"], complete=True)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "impossible" and body["reason"] == "degree"
    replay = submit(client, task_id, 1, ["ABD"], complete=True)
    assert replay.status_code == 200 and replay.json() == body
    state = client.get(f"/tasks/{task_id}").json()
    assert state["status"] == "completed" and state["result"] == body


def test_finalize_by_identical_resend_with_complete_flag(client: TestClient):
    task_id = create_task(client)
    submit(client, task_id, 0, ["ABC", "ABC"])
    submit(client, task_id, 1, ["BCA"])
    submit(client, task_id, 2, ["CAB"])  # not declared complete yet
    r = submit(client, task_id, 2, ["CAB"], complete=True)
    assert r.status_code == 200 and r.json()["assembly"] == "ABCABC"


def test_finalize_by_identical_resend_reports_gaps(client: TestClient):
    task_id = create_task(client)
    submit(client, task_id, 0, ["ABC", "ABC"])
    submit(client, task_id, 2, ["CAB"])  # index 1 still missing
    r = submit(client, task_id, 2, ["CAB"], complete=True)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "MISSING_CHUNKS"
    assert r.json()["error"]["details"]["missing_indices"] == [1]
    submit(client, task_id, 1, ["BCA"])
    r = submit(client, task_id, 2, ["CAB"], complete=True)
    assert r.status_code == 200 and r.json()["assembly"] == "ABCABC"


def test_empty_task_completes_with_start_only(client: TestClient):
    task_id = create_task(client, expected_total=0)
    r = submit(client, task_id, 0, [], complete=True)
    assert r.status_code == 200
    assert r.json() == {
        "status": "unique",
        "assembly": "AB",
        "length": 2,
        "fragment_count": 0,
    }


def test_unknown_task(client: TestClient):
    r = client.get("/tasks/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TASK_NOT_FOUND"
    assert r.json()["error"]["details"] == {"task_id": "nope"}
    r = submit(client, "nope", 0, ["ABC"])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TASK_NOT_FOUND"


def test_storage_failure_is_locatable(tmp_path):
    db_path = tmp_path / "ingest.db"
    client = TestClient(create_app(db_path=str(db_path)))
    task_id = create_task(client)
    # Break the storage mid-flight: connections are opened per request, so
    # replacing the database file with a directory fails the next request.
    db_path.unlink()
    db_path.mkdir()
    r = submit(client, task_id, 0, ["ABC"])
    assert r.status_code == 500
    error = r.json()["error"]
    assert error["code"] == "STORAGE_ERROR"
    assert error["details"]["task_id"] == task_id
    assert error["details"]["index"] == 0
    r = client.get(f"/tasks/{task_id}")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "STORAGE_ERROR"
    assert r.json()["error"]["details"]["task_id"] == task_id


def test_chunk_fragments_validated_against_task_k(client: TestClient):
    task_id = create_task(client)
    r = submit(client, task_id, 0, ["abC"])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_FRAGMENTS"
    r = submit(client, task_id, 0, ["AB"])  # wrong length for k=3
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_FRAGMENTS"
    # Nothing was stored.
    assert client.get(f"/tasks/{task_id}").json()["received_total"] == 0


def test_chunk_request_schema(client: TestClient):
    task_id = create_task(client)
    assert submit(client, task_id, -1, ["ABC"]).status_code == 422
    r = client.post(
        f"/tasks/{task_id}/chunks",
        json={"index": 0, "fragments": ["ABC"], "complete": "yes"},
    )
    assert r.status_code == 422  # complete must be a JSON boolean
    r = client.post(
        f"/tasks/{task_id}/chunks", json={"index": 0, "fragments": ["ABC"], "x": 1}
    )
    assert r.status_code == 422  # extra fields are forbidden


def test_task_creation_validation(client: TestClient):
    r = client.post("/tasks", json={"k": 3, "start": "ab", "expected_total": 4})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_START"
    r = client.post("/tasks", json={"k": 3, "start": "AB", "expected_total": 20_001})
    assert r.status_code == 422
    r = client.post("/tasks", json={"k": 3, "start": "AB", "expected_total": -1})
    assert r.status_code == 422
    r = client.post("/tasks", json={"k": "3", "start": "AB", "expected_total": 4})
    assert r.status_code == 422


def test_twenty_thousand_fragments_ingested_in_chunks(tmp_path):
    rng = random.Random(20260915)
    k = 12
    start, fragments = random_walk(
        rng, k, 20_000, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    )
    client = TestClient(create_app(db_path=str(tmp_path / "ingest.db")))
    task_id = create_task(client, k=k, start=start, expected_total=20_000)
    chunks = [fragments[i : i + 5_000] for i in range(0, 20_000, 5_000)]
    body = None
    for pos, idx in enumerate([2, 0, 3, 1]):  # out of order, 1 completes
        r = submit(client, task_id, idx, chunks[idx], complete=(pos == 3))
        assert r.status_code in (200, 201), r.text
        body = r.json()
    assert body is not None and body["fragment_count"] == 20_000
    one_shot = client.post(
        "/assemble", json={"k": k, "start": start, "fragments": fragments}
    ).json()
    assert body == one_shot
