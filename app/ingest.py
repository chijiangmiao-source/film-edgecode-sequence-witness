"""Resumable chunked ingestion backed by embedded SQLite.

The archive bulk importer splits one reel's fragments (up to 20 000) into
many requests and re-sends them after network timeouts.  This module makes
that safe:

* a *task* pins down ``k``, the start prefix and the expected fragment
  total (``POST /tasks``);
* fragments arrive in *chunks* addressed by a zero-based index, in any
  order (``POST /tasks/{task_id}/chunks``);
* re-sending the same chunk with identical content replays the original
  acknowledgement instead of writing twice, while re-using an index with
  different content is a ``409 CHUNK_CONFLICT``;
* every write happens inside a single ``BEGIN IMMEDIATE`` transaction, so
  concurrent requests are serialised, a chunk can only be written once and
  the received total can never exceed the expected total;
* the final chunk may declare ``complete`` in the same request.  Completion
  is accepted only when the chunk indices are contiguous (``0..n-1``) and
  the received total equals the expected total; it freezes the input inside
  the transaction, calls the existing assembly service and stores the
  result -- including the business result ``impossible`` -- so completed
  responses can be replayed after a process restart.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from app.assembly import assemble
from app.errors import ApiError
from app.models import ChunkAck, TaskState, render_assembly_result
from app.validation import validate_fragments

logger = logging.getLogger("assembly.ingest")

ENV_DB_PATH = "EDGECODE_DB_PATH"
DEFAULT_DB_PATH = "edgecode_ingest.db"

# Missing-index lists in error details and task state are capped so a
# pathological index cannot produce an unbounded response.
MAX_MISSING_REPORTED = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id        TEXT PRIMARY KEY,
    k              INTEGER NOT NULL,
    start          TEXT NOT NULL,
    expected_total INTEGER NOT NULL,
    received_total INTEGER NOT NULL DEFAULT 0,
    chunk_count    INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'completed')),
    result_json    TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at   TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    task_id              TEXT NOT NULL REFERENCES tasks (task_id),
    chunk_index          INTEGER NOT NULL,
    content_hash         TEXT NOT NULL,
    fragment_count       INTEGER NOT NULL,
    received_total_after INTEGER NOT NULL,
    declared_complete    INTEGER NOT NULL DEFAULT 0,
    fragments_json       TEXT NOT NULL,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (task_id, chunk_index)
);
"""


def resolve_db_path(db_path: str | None = None) -> str:
    """Explicit argument wins, then ``EDGECODE_DB_PATH``, then the default."""
    return db_path or os.environ.get(ENV_DB_PATH) or DEFAULT_DB_PATH


def _content_hash(fragments: list[str]) -> str:
    canonical = json.dumps(fragments, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChunkOutcome:
    """What the chunk endpoint should send back."""

    status_code: int
    body: dict[str, Any]


class IngestionService:
    """Task/chunk persistence plus the transactional ingestion rules."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = resolve_db_path(db_path)
        self._init_schema()

    # ------------------------------------------------------------------
    # connection handling
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # Autocommit mode: transactions are driven explicitly with
        # BEGIN IMMEDIATE / COMMIT / ROLLBACK.  A fresh connection per call
        # keeps the service thread-safe; SQLite serialises writers.
        conn = sqlite3.connect(self._db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # task creation / inspection
    # ------------------------------------------------------------------

    def create_task(self, *, k: int, start: str, expected_total: int) -> TaskState:
        task_id = uuid.uuid4().hex
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise self._storage_error(exc, task_id=task_id) from exc
        try:
            conn.execute(
                "INSERT INTO tasks (task_id, k, start, expected_total)"
                " VALUES (?, ?, ?, ?)",
                (task_id, k, start, expected_total),
            )
        except sqlite3.Error as exc:
            raise self._storage_error(exc, task_id=task_id) from exc
        finally:
            conn.close()
        return TaskState(
            task_id=task_id,
            k=k,
            start=start,
            expected_total=expected_total,
            received_total=0,
            chunk_count=0,
            status="open",
            missing_indices=[],
            result=None,
        )

    def get_task(self, task_id: str) -> TaskState:
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise self._storage_error(exc, task_id=task_id) from exc
        try:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise self._task_not_found(task_id)
            missing = (
                []
                if row["status"] == "completed"
                else self._missing_indices(conn, task_id)
            )
            result = (
                json.loads(row["result_json"])
                if row["result_json"] is not None
                else None
            )
            return TaskState(
                task_id=task_id,
                k=row["k"],
                start=row["start"],
                expected_total=row["expected_total"],
                received_total=row["received_total"],
                chunk_count=row["chunk_count"],
                status=row["status"],
                missing_indices=missing,
                result=result,
            )
        except sqlite3.Error as exc:
            raise self._storage_error(exc, task_id=task_id) from exc
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # chunk submission (the one write path)
    # ------------------------------------------------------------------

    def submit_chunk(
        self,
        task_id: str,
        *,
        index: int,
        fragments: list[str],
        complete: bool,
    ) -> ChunkOutcome:
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise self._storage_error(exc, task_id=task_id, index=index) from exc
        try:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT k, start, expected_total, received_total, status,"
                "       result_json"
                " FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise self._task_not_found(task_id)
            # Semantic validation happens before any write, inside the
            # transaction so the task row cannot change underneath us.
            validate_fragments(task["k"], fragments)
            digest = _content_hash(fragments)
            existing = conn.execute(
                "SELECT content_hash, fragment_count, received_total_after,"
                "       declared_complete"
                " FROM chunks WHERE task_id = ? AND chunk_index = ?",
                (task_id, index),
            ).fetchone()
            if existing is not None:
                outcome = self._replay_or_finalize(
                    conn, task, task_id, index, complete, existing, digest
                )
            else:
                outcome = self._insert_chunk(
                    conn, task, task_id, index, fragments, complete, digest
                )
            conn.execute("COMMIT")
            return outcome
        except ApiError:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise self._storage_error(exc, task_id=task_id, index=index) from exc
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # transaction internals (must only be called between BEGIN and COMMIT)
    # ------------------------------------------------------------------

    def _replay_or_finalize(
        self,
        conn: sqlite3.Connection,
        task: sqlite3.Row,
        task_id: str,
        index: int,
        complete: bool,
        existing: sqlite3.Row,
        digest: str,
    ) -> ChunkOutcome:
        """The (task, index) slot is taken: replay, conflict or finalize."""
        if existing["content_hash"] != digest:
            raise ApiError(
                409,
                "CHUNK_CONFLICT",
                f"chunk {index} of task {task_id!r} already holds different"
                " content; the stored chunk was kept",
                {"task_id": task_id, "index": index},
            )
        if complete:
            if task["status"] == "completed":
                # Identical re-send of the completing chunk: replay the
                # stored assembly result (also after a process restart).
                return ChunkOutcome(200, json.loads(task["result_json"]))
            # Identical content, now declaring completion: finalize.
            return ChunkOutcome(200, self._complete(conn, task, task_id, index))
        if existing["declared_complete"]:
            # The original request completed the task, so its
            # acknowledgement *is* the stored assembly result.
            return ChunkOutcome(200, json.loads(task["result_json"]))
        ack = ChunkAck(
            task_id=task_id,
            index=index,
            accepted=existing["fragment_count"],
            received_total=existing["received_total_after"],
        )
        return ChunkOutcome(200, ack.model_dump(mode="json"))

    def _insert_chunk(
        self,
        conn: sqlite3.Connection,
        task: sqlite3.Row,
        task_id: str,
        index: int,
        fragments: list[str],
        complete: bool,
        digest: str,
    ) -> ChunkOutcome:
        if task["status"] == "completed":
            raise ApiError(
                409,
                "TASK_COMPLETED",
                f"task {task_id!r} is already completed; no further chunks"
                " are accepted",
                {"task_id": task_id, "index": index},
            )
        new_total = task["received_total"] + len(fragments)
        if new_total > task["expected_total"]:
            raise ApiError(
                409,
                "CHUNK_OVERFLOW",
                f"chunk {index} would raise the received total of task"
                f" {task_id!r} to {new_total}, above the expected"
                f" {task['expected_total']}",
                {
                    "task_id": task_id,
                    "index": index,
                    "received_total": task["received_total"],
                    "chunk_fragments": len(fragments),
                    "expected_total": task["expected_total"],
                },
            )
        conn.execute(
            "INSERT INTO chunks (task_id, chunk_index, content_hash,"
            " fragment_count, received_total_after, declared_complete,"
            " fragments_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                index,
                digest,
                len(fragments),
                new_total,
                1 if complete else 0,
                json.dumps(fragments, separators=(",", ":")),
            ),
        )
        conn.execute(
            "UPDATE tasks SET received_total = ?, chunk_count = chunk_count + 1"
            " WHERE task_id = ?",
            (new_total, task_id),
        )
        if not complete:
            ack = ChunkAck(
                task_id=task_id,
                index=index,
                accepted=len(fragments),
                received_total=new_total,
            )
            return ChunkOutcome(201, ack.model_dump(mode="json"))
        return ChunkOutcome(200, self._complete(conn, task, task_id, index))

    def _complete(
        self,
        conn: sqlite3.Connection,
        task: sqlite3.Row,
        task_id: str,
        index: int,
    ) -> dict[str, Any]:
        """Freeze the task and assemble; returns the response body.

        Only reachable inside the write transaction.  Any failure raises
        and rolls the whole submission back, so a rejected completion
        stores nothing and can simply be retried after backfilling.
        """
        stats = conn.execute(
            "SELECT COUNT(*) AS n, MAX(chunk_index) AS hi,"
            " COALESCE(SUM(fragment_count), 0) AS total"
            " FROM chunks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        # Indices are validated >= 0 at the schema boundary, so the set is
        # exactly {0..n-1} iff count == max + 1.
        if stats["n"] != stats["hi"] + 1:
            raise ApiError(
                409,
                "MISSING_CHUNKS",
                f"task {task_id!r} cannot complete: chunk indices are not"
                " contiguous",
                {
                    "task_id": task_id,
                    "index": index,
                    "received_chunks": stats["n"],
                    "missing_indices": self._missing_indices(conn, task_id),
                },
            )
        if stats["total"] != task["expected_total"]:
            raise ApiError(
                409,
                "TOTAL_MISMATCH",
                f"task {task_id!r} cannot complete: received"
                f" {stats['total']} fragment(s), expected"
                f" {task['expected_total']}",
                {
                    "task_id": task_id,
                    "index": index,
                    "received_total": stats["total"],
                    "expected_total": task["expected_total"],
                },
            )
        # Freeze the input inside the transaction, then call the existing
        # assembly service.  An ``impossible`` verdict is a normal business
        # result and is stored exactly like ``unique`` / ``ambiguous``.
        conn.execute(
            "UPDATE tasks SET status = 'completed',"
            " completed_at = datetime('now') WHERE task_id = ?",
            (task_id,),
        )
        rows = conn.execute(
            "SELECT fragments_json FROM chunks WHERE task_id = ?"
            " ORDER BY chunk_index",
            (task_id,),
        ).fetchall()
        fragments = [
            fragment for row in rows for fragment in json.loads(row["fragments_json"])
        ]
        result = render_assembly_result(
            assemble(task["k"], task["start"], fragments)
        )
        body = result.model_dump(mode="json")
        conn.execute(
            "UPDATE tasks SET result_json = ? WHERE task_id = ?",
            (json.dumps(body, separators=(",", ":")), task_id),
        )
        return body

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _missing_indices(conn: sqlite3.Connection, task_id: str) -> list[int]:
        """Gaps below the highest received chunk index (capped)."""
        present = [
            row[0]
            for row in conn.execute(
                "SELECT chunk_index FROM chunks WHERE task_id = ?"
                " ORDER BY chunk_index",
                (task_id,),
            )
        ]
        missing: list[int] = []
        nxt = 0
        for idx in present:
            while nxt < idx and len(missing) < MAX_MISSING_REPORTED:
                missing.append(nxt)
                nxt += 1
            nxt = idx + 1
            if len(missing) >= MAX_MISSING_REPORTED:
                break
        return missing

    @staticmethod
    def _task_not_found(task_id: str) -> ApiError:
        return ApiError(
            404,
            "TASK_NOT_FOUND",
            f"no ingestion task {task_id!r}",
            {"task_id": task_id},
        )

    @staticmethod
    def _storage_error(
        exc: sqlite3.Error,
        *,
        task_id: str | None = None,
        index: int | None = None,
    ) -> ApiError:
        logger.exception(
            "ingestion storage failure (task_id=%s, index=%s): %s",
            task_id,
            index,
            exc,
        )
        details: dict[str, Any] = {}
        if task_id is not None:
            details["task_id"] = task_id
        if index is not None:
            details["index"] = index
        return ApiError(
            500,
            "STORAGE_ERROR",
            "ingestion storage failure; no state was changed,"
            " the request can be retried",
            details or None,
        )
