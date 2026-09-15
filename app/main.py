"""FastAPI application: film edge-code assembly service."""

from __future__ import annotations

from fastapi import FastAPI

from app.assembly import assemble, validate_assembly
from app.errors import register_exception_handlers
from app.models import (
    AmbiguousResponse,
    AssembleRequest,
    AssembleResponse,
    ConnectivityEvidence,
    DegreeEvidence,
    FirstDifference,
    ImpossibleResponse,
    StartEvidence,
    UniqueResponse,
    VerifyInvalidResponse,
    VerifyRequest,
    VerifyResponse,
    VerifyValidResponse,
    Witnesses,
)
from app.validation import validate_fragments, validate_start

_EVIDENCE_MODELS = {
    "degree": DegreeEvidence,
    "start": StartEvidence,
    "connectivity": ConnectivityEvidence,
}


def create_app() -> FastAPI:
    app = FastAPI(
        title="Film Edge-Code Assembly API",
        version="1.0.0",
        summary=(
            "Assemble a multiset of overlapping film edge-code fragments into "
            "the complete edge-code string, or prove that the reel order is "
            "ambiguous / impossible."
        ),
    )
    register_exception_handlers(app)

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
        result = assemble(request.k, request.start, request.fragments)
        if result.status == "unique":
            assert result.assembly is not None
            return UniqueResponse(
                assembly=result.assembly,
                length=len(result.assembly),
                fragment_count=result.fragment_count,
            )
        if result.status == "ambiguous":
            assert result.min_witness is not None and result.max_witness is not None
            lo, hi = result.min_witness, result.max_witness
            index = next(
                i for i, (a, b) in enumerate(zip(lo, hi)) if a != b
            )
            return AmbiguousResponse(
                witnesses=Witnesses(min=lo, max=hi),
                first_difference=FirstDifference(index=index, min=lo[index], max=hi[index]),
                fragment_count=result.fragment_count,
            )
        assert result.evidence is not None and result.reason is not None
        return ImpossibleResponse(
            reason=result.reason,  # type: ignore[arg-type]
            evidence=_EVIDENCE_MODELS[result.reason](**result.evidence),
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

    return app


app = create_app()
