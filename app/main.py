"""FastAPI application: film edge-code assembly service."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.assembly import assemble, validate_assembly
from app.errors import register_exception_handlers
from app.ingest import IngestionService
from app.models import (
    AmbiguousResponse,
    AssembleRequest,
    AssembleResponse,
    ChunkAck,
    ChunkSubmitRequest,
    ImpossibleResponse,
    TaskCreateRequest,
    TaskState,
    UniqueResponse,
    VerifyInvalidResponse,
    VerifyRequest,
    VerifyResponse,
    VerifyValidResponse,
    render_assembly_result,
)
from app.validation import validate_fragments, validate_start

# A chunk submission either stores a new chunk (201, ChunkAck), replays an
# identical re-send (200, ChunkAck), or -- when it declares completion --
# returns the same body a one-shot /assemble would have produced (200).
ChunkResponse = ChunkAck | UniqueResponse | AmbiguousResponse | ImpossibleResponse


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="Film Edge-Code Assembly API",
        version="1.1.0",
        summary=(
            "Assemble a multiset of overlapping film edge-code fragments into "
            "the complete edge-code string, or prove that the reel order is "
            "ambiguous / impossible.  Bulk imports can use the resumable "
            "chunked ingestion flow backed by embedded SQLite."
        ),
    )
    register_exception_handlers(app)
    ingestion = IngestionService(db_path)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/assemble",
        response_model=AssembleResponse,
        tags=["assembly"],
        summary="Decide whether the fragment multiset assembles uniquely",
    )
    def assemble_endpoint(request: AssembleRequest) -> AssembleResponse:
        validate_start(request.k, request.start)
        validate_fragments(request.k, request.fragments)
        return render_assembly_result(
            assemble(request.k, request.start, request.fragments)
        )

    @app.post(
        "/verify",
        response_model=VerifyResponse,
        tags=["assembly"],
        summary="Re-check a claimed complete edge-code string character by character",
    )
    def verify_endpoint(request: VerifyRequest) -> VerifyResponse:
        validate_start(request.k, request.start)
        validate_fragments(request.k, request.fragments)
        outcome = validate_assembly(
            request.k, request.start, request.fragments, request.candidate
        )
        if outcome["valid"]:
            return VerifyValidResponse(
                candidate_length=outcome["candidate_length"],
                fragment_count=outcome["fragment_count"],
            )
        return VerifyInvalidResponse(
            reason=outcome["reason"], detail=outcome["detail"]
        )

    @app.post(
        "/tasks",
        response_model=TaskState,
        status_code=201,
        tags=["ingestion"],
        summary="Create a resumable ingestion task",
    )
    def create_task_endpoint(request: TaskCreateRequest) -> TaskState:
        validate_start(request.k, request.start)
        return ingestion.create_task(
            k=request.k, start=request.start, expected_total=request.expected_total
        )

    @app.get(
        "/tasks/{task_id}",
        response_model=TaskState,
        tags=["ingestion"],
        summary="Inspect an ingestion task (resume after restart)",
    )
    def get_task_endpoint(task_id: str) -> TaskState:
        return ingestion.get_task(task_id)

    @app.post(
        "/tasks/{task_id}/chunks",
        tags=["ingestion"],
        summary="Submit one zero-based chunk; the final chunk may declare completion",
        responses={
            200: {
                "model": ChunkResponse,
                "description": "Replayed acknowledgement for an identical "
                "re-send, or the assembly result when completion is declared.",
            },
            201: {"model": ChunkAck, "description": "Chunk stored."},
        },
    )
    def submit_chunk_endpoint(
        task_id: str, request: ChunkSubmitRequest
    ) -> JSONResponse:
        outcome = ingestion.submit_chunk(
            task_id,
            index=request.index,
            fragments=request.fragments,
            complete=request.complete,
        )
        return JSONResponse(status_code=outcome.status_code, content=outcome.body)

    return app


app = create_app()
