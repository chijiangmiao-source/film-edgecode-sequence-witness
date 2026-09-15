"""One-shot acceptance run against a live API (compose service ``verify``).

Exercises the running container over HTTP: health, unique / ambiguous /
impossible outcomes, witness re-computation, structured errors, a
20 000-fragment stress case, and the resumable chunked ingestion flow
(out-of-order backfill, concurrent duplicate chunks, conflicting re-sends,
and resume/replay of completed responses).  Exits non-zero if any check
fails.
"""

from __future__ import annotations

import concurrent.futures
import os
import random
import sys
import time

import httpx

BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

failures: list[str] = []


def check(name: str, condition: bool, context: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" -- {context}" if context and not condition else ""))
    if not condition:
        failures.append(name)


def wait_for_api(client: httpx.Client, deadline_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    print(f"FAIL: API at {BASE_URL} did not become healthy within {deadline_seconds}s")
    sys.exit(1)


def random_walk(rng: random.Random, k: int, n: int, alphabet: str):
    vertex = "".join(rng.choice(alphabet) for _ in range(k - 1))
    start = vertex
    fragments = []
    for _ in range(n):
        nxt = vertex[1:] + rng.choice(alphabet)
        fragments.append(vertex + nxt[-1])
        vertex = nxt
    return start, fragments


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=60.0) as client:
        wait_for_api(client)

        # --- unique -------------------------------------------------------
        payload = {"k": 3, "start": "AB", "fragments": ["ABC", "BCA", "CAB"]}
        r = client.post("/assemble", json=payload)
        body = r.json()
        check(
            "unique cycle assembly",
            r.status_code == 200
            and body["status"] == "unique"
            and body["assembly"] == "ABCAB"
            and body["length"] == 5,
            repr(body),
        )
        rv = client.post("/verify", json=payload | {"candidate": "ABCAB"}).json()
        check("unique assembly re-verifies", rv.get("valid") is True, repr(rv))

        # --- duplicates are kept, parallel copies are not ambiguity -------
        payload = {"k": 3, "start": "AB", "fragments": ["ABC", "ABC", "BCA", "CAB"]}
        body = client.post("/assemble", json=payload).json()
        check(
            "duplicate fragments preserved",
            body.get("status") == "unique" and body.get("assembly") == "ABCABC",
            repr(body),
        )

        # --- ambiguous with recomputable witnesses ------------------------
        payload = {"k": 3, "start": "AB", "fragments": ["ABA", "BAB", "ABC", "BCA", "CAB"]}
        body = client.post("/assemble", json=payload).json()
        witnesses = body.get("witnesses", {})
        lo, hi = witnesses.get("min"), witnesses.get("max")
        check(
            "ambiguous figure-eight",
            body.get("status") == "ambiguous" and lo is not None and lo < hi,
            repr(body),
        )
        check(
            "witnesses are the ASCII extremes",
            (lo, hi) == ("ABABCAB", "ABCABAB"),
            f"min={lo} max={hi}",
        )
        for witness in (lo, hi):
            rv = client.post("/verify", json=payload | {"candidate": witness}).json()
            check(f"witness {witness!r} recomputes as valid", rv.get("valid") is True, repr(rv))
        diff = body.get("first_difference", {})
        check(
            "first_difference is consistent",
            lo[: diff.get("index", -1)] == hi[: diff.get("index", -1)]
            and lo[diff["index"]] == diff["min"]
            and hi[diff["index"]] == diff["max"],
            repr(diff),
        )

        # --- impossible: degree -------------------------------------------
        body = client.post(
            "/assemble", json={"k": 3, "start": "AB", "fragments": ["ABC", "ABD"]}
        ).json()
        check(
            "impossible: degree evidence",
            body.get("status") == "impossible"
            and body.get("reason") == "degree"
            and any(
                v["vertex"] == "AB" and v["balance"] == 2
                for v in body["evidence"]["imbalanced_vertices"]
            ),
            repr(body),
        )

        # --- impossible: start --------------------------------------------
        body = client.post(
            "/assemble", json={"k": 3, "start": "BC", "fragments": ["ABC", "BCD"]}
        ).json()
        check(
            "impossible: start evidence",
            body.get("status") == "impossible"
            and body.get("reason") == "start"
            and body["evidence"]["required_start"] == "AB",
            repr(body),
        )

        # --- impossible: connectivity --------------------------------------
        body = client.post(
            "/assemble",
            json={
                "k": 3,
                "start": "AB",
                "fragments": ["ABC", "BCA", "CAB", "DEF", "EFD", "FDE"],
            },
        ).json()
        check(
            "impossible: connectivity evidence",
            body.get("status") == "impossible"
            and body.get("reason") == "connectivity"
            and body["evidence"]["component_count"] == 2,
            repr(body),
        )

        # --- structured errors ---------------------------------------------
        r = client.post("/assemble", json={"k": 2, "start": "A", "fragments": ["AB"]})
        check(
            "k out of range -> 422 envelope",
            r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR",
            repr(r.json()),
        )
        r = client.post("/assemble", json={"k": "3", "start": "AB", "fragments": ["ABC"]})
        check(
            "k as string -> 422 type error (no coercion)",
            r.status_code == 422
            and r.json()["error"]["code"] == "VALIDATION_ERROR"
            and any(
                "k" in d["loc"] and d["type"] == "int_type"
                for d in r.json()["error"]["details"]
            ),
            repr(r.json()),
        )
        r = client.post("/assemble", json={"k": 3, "start": "AB", "fragments": ["abC"]})
        check(
            "lowercase fragment -> INVALID_FRAGMENTS",
            r.status_code == 422 and r.json()["error"]["code"] == "INVALID_FRAGMENTS",
            repr(r.json()),
        )
        r = client.post("/assemble", json={"k": 3, "start": "ab", "fragments": ["ABC"]})
        check(
            "lowercase start -> INVALID_START",
            r.status_code == 422 and r.json()["error"]["code"] == "INVALID_START",
            repr(r.json()),
        )
        r = client.get("/definitely-not-a-route")
        check(
            "unknown route -> 404 envelope",
            r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND",
            repr(r.json()),
        )

        # --- verify rejects tampering --------------------------------------
        rv = client.post(
            "/verify",
            json={
                "k": 3,
                "start": "AB",
                "fragments": ["ABC", "BCA", "CAB"],
                "candidate": "ABCBA",
            },
        ).json()
        check(
            "tampered candidate rejected with position",
            rv.get("valid") is False
            and rv.get("reason") == "fragment_unavailable"
            and rv["detail"]["index"] == 1,
            repr(rv),
        )

        # --- 20 000 fragments ----------------------------------------------
        rng = random.Random(20260915)
        start, fragments = random_walk(rng, 12, 20_000, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
        t0 = time.perf_counter()
        r = client.post("/assemble", json={"k": 12, "start": start, "fragments": fragments})
        elapsed = time.perf_counter() - t0
        body = r.json()
        check(
            f"20 000 fragments handled in {elapsed:.2f}s",
            r.status_code == 200 and body.get("status") in {"unique", "ambiguous"},
            repr(body)[:400],
        )
        if body.get("status") == "ambiguous":
            for witness in (body["witnesses"]["min"], body["witnesses"]["max"]):
                rv = client.post(
                    "/verify",
                    json={"k": 12, "start": start, "fragments": fragments, "candidate": witness},
                ).json()
                check("20k witness recomputes as valid", rv.get("valid") is True)

        # ==================================================================
        # resumable chunked ingestion
        #
        # Process-restart durability itself is covered by pytest
        # (tests/test_ingest.py::test_restart_resume_and_replay, which runs
        # in this compose verify step); over plain HTTP the client-visible
        # half is resuming from server state and replaying the completed
        # response, checked below.
        # ==================================================================

        one_shot = client.post(
            "/assemble",
            json={"k": 3, "start": "AB", "fragments": ["ABC", "ABC", "BCA", "CAB"]},
        ).json()

        # --- path 1: out-of-order backfill --------------------------------
        r = client.post("/tasks", json={"k": 3, "start": "AB", "expected_total": 4})
        task = r.json()
        task_id = task.get("task_id", "")
        check(
            "ingest: task created (k, start, expected total)",
            r.status_code == 201
            and task.get("status") == "open"
            and task.get("received_total") == 0
            and task.get("expected_total") == 4
            and bool(task_id),
            repr(task),
        )
        r1 = client.post(f"/tasks/{task_id}/chunks", json={"index": 1, "fragments": ["BCA", "CAB"]})
        r0 = client.post(
            f"/tasks/{task_id}/chunks",
            json={"index": 0, "fragments": ["ABC", "ABC"], "complete": True},
        )
        check(
            "ingest: out-of-order chunks, final chunk completes with the one-shot result",
            r1.status_code == 201 and r0.status_code == 200 and r0.json() == one_shot,
            f"chunk1={r1.status_code} final={r0.status_code} body={r0.text[:200]}",
        )
        replay = client.post(
            f"/tasks/{task_id}/chunks",
            json={"index": 0, "fragments": ["ABC", "ABC"], "complete": True},
        )
        check(
            "ingest: completed response replays identically",
            replay.status_code == 200 and replay.json() == r0.json(),
            replay.text[:200],
        )
        state = client.get(f"/tasks/{task_id}").json()
        check(
            "ingest: completed task state carries the stored result",
            state.get("status") == "completed" and state.get("result") == r0.json(),
            repr(state)[:300],
        )
        late = client.post(f"/tasks/{task_id}/chunks", json={"index": 2, "fragments": ["ABC"]})
        check(
            "ingest: write after completion -> 409 TASK_COMPLETED",
            late.status_code == 409 and late.json()["error"]["code"] == "TASK_COMPLETED",
            late.text[:200],
        )

        # --- path 2: concurrent identical chunk written once ---------------
        tid = client.post("/tasks", json={"k": 3, "start": "AB", "expected_total": 2}).json()["task_id"]

        def send_chunk(_: int) -> httpx.Response:
            return client.post(
                f"/tasks/{tid}/chunks", json={"index": 0, "fragments": ["ABC", "ABC"]}
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(send_chunk, range(8)))
        codes = [r.status_code for r in responses]
        state = client.get(f"/tasks/{tid}").json()
        check(
            "ingest: concurrent identical chunk written exactly once",
            codes.count(201) == 1
            and codes.count(200) == 7
            and len({r.text for r in responses}) == 1
            and state.get("received_total") == 2
            and state.get("chunk_count") == 1,
            f"codes={sorted(codes)} state={state}",
        )

        # --- path 3: conflicting re-send -----------------------------------
        identical = client.post(f"/tasks/{tid}/chunks", json={"index": 0, "fragments": ["ABC", "ABC"]})
        conflict = client.post(f"/tasks/{tid}/chunks", json={"index": 0, "fragments": ["ABD", "ABD"]})
        details = conflict.json().get("error", {}).get("details", {})
        check(
            "ingest: identical retry replays ack, different content -> 409 CHUNK_CONFLICT",
            identical.status_code == 200
            and conflict.status_code == 409
            and conflict.json()["error"]["code"] == "CHUNK_CONFLICT"
            and details.get("task_id") == tid
            and details.get("index") == 0,
            conflict.text[:300],
        )

        # --- path 4: gap blocks completion, backfill resumes and completes --
        tid = client.post("/tasks", json={"k": 3, "start": "AB", "expected_total": 4}).json()["task_id"]
        client.post(f"/tasks/{tid}/chunks", json={"index": 0, "fragments": ["ABC", "ABC"]})
        gap = client.post(
            f"/tasks/{tid}/chunks", json={"index": 2, "fragments": ["CAB"], "complete": True}
        )
        check(
            "ingest: completion with a gap -> 409 MISSING_CHUNKS (locatable)",
            gap.status_code == 409
            and gap.json()["error"]["code"] == "MISSING_CHUNKS"
            and gap.json()["error"]["details"].get("missing_indices") == [1]
            and gap.json()["error"]["details"].get("task_id") == tid,
            gap.text[:300],
        )
        client.post(f"/tasks/{tid}/chunks", json={"index": 1, "fragments": ["BCA"]})
        done = client.post(
            f"/tasks/{tid}/chunks", json={"index": 2, "fragments": ["CAB"], "complete": True}
        )
        check(
            "ingest: backfilled task completes with the one-shot result",
            done.status_code == 200 and done.json() == one_shot,
            done.text[:200],
        )
        overflow = client.post(
            "/tasks", json={"k": 3, "start": "AB", "expected_total": 1}
        ).json()["task_id"]
        over = client.post(f"/tasks/{overflow}/chunks", json={"index": 0, "fragments": ["ABC", "ABC"]})
        check(
            "ingest: chunk beyond expected total -> 409 CHUNK_OVERFLOW",
            over.status_code == 409 and over.json()["error"]["code"] == "CHUNK_OVERFLOW",
            over.text[:200],
        )
        missing = client.get("/tasks/definitely-not-a-task")
        check(
            "ingest: unknown task -> 404 TASK_NOT_FOUND",
            missing.status_code == 404 and missing.json()["error"]["code"] == "TASK_NOT_FOUND",
            missing.text[:200],
        )

    print()
    if failures:
        print(f"ACCEPTANCE FAILED: {len(failures)} check(s) failed: {failures}")
        return 1
    print("ACCEPTANCE OK: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
