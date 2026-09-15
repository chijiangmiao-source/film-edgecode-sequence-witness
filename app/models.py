"""Pydantic request/response models for the assembly API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from app.assembly import AssemblyResult

from app.assembly import AssemblyResult

MAX_FRAGMENTS = 20_000
MIN_K = 3
MAX_K = 12
# A complete assembly is the (k-1)-char start prefix plus one character per
# fragment, so it can never exceed (MAX_K - 1) + MAX_FRAGMENTS characters.
MAX_CANDIDATE_LENGTH = (MAX_K - 1) + MAX_FRAGMENTS


class AssembleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    k: StrictInt = Field(
        ge=MIN_K,
        le=MAX_K,
        description="fragment length; every fragment has exactly k characters "
        "(must be a JSON integer, not a numeric string)",
    )
    start: str = Field(
        description="designated start prefix: exactly k-1 characters of A-Z / 0-9"
    )
    fragments: list[str] = Field(
        max_length=MAX_FRAGMENTS,
        description="fragment multiset; duplicates are kept by occurrence count",
    )


class VerifyRequest(AssembleRequest):
    candidate: str = Field(
        max_length=MAX_CANDIDATE_LENGTH,
        description="claimed complete edge-code string to re-check",
    )


class UniqueResponse(BaseModel):
    status: Literal["unique"] = "unique"
    assembly: str
    length: int
    fragment_count: int


class Witnesses(BaseModel):
    min: str = Field(description="ASCII-smallest complete assembly")
    max: str = Field(description="ASCII-largest complete assembly")


class FirstDifference(BaseModel):
    index: int
    min: str
    max: str


class AmbiguousResponse(BaseModel):
    status: Literal["ambiguous"] = "ambiguous"
    witnesses: Witnesses
    first_difference: FirstDifference
    fragment_count: int


class Imbalance(BaseModel):
    vertex: str
    in_degree: int
    out_degree: int
    balance: int = Field(description="out_degree - in_degree")


class DegreeEvidence(BaseModel):
    kind: Literal["degree"] = "degree"
    total_imbalanced: int
    imbalanced_vertices: list[Imbalance]
    note: str


class StartEvidence(BaseModel):
    kind: Literal["start"] = "start"
    given_start: str
    required_start: str | None = Field(
        description="the only vertex an Eulerian trail may begin at; null when "
        "the given start is not incident to any fragment"
    )
    start_in_degree: int
    start_out_degree: int
    detail: str


class ComponentSummary(BaseModel):
    vertex_count: int
    edge_count: int
    sample_vertices: list[str]


class ConnectivityEvidence(BaseModel):
    kind: Literal["connectivity"] = "connectivity"
    component_count: int
    components: list[ComponentSummary]
    note: str


class ImpossibleResponse(BaseModel):
    status: Literal["impossible"] = "impossible"
    reason: Literal["degree", "start", "connectivity"]
    evidence: DegreeEvidence | StartEvidence | ConnectivityEvidence


AssembleResponse = UniqueResponse | AmbiguousResponse | ImpossibleResponse


class VerifyValidResponse(BaseModel):
    valid: Literal[True] = True
    candidate_length: int
    fragment_count: int


class VerifyInvalidResponse(BaseModel):
    valid: Literal[False] = False
    reason: str
    detail: dict[str, Any]


VerifyResponse = VerifyValidResponse | VerifyInvalidResponse


_EVIDENCE_MODELS = {
    "degree": DegreeEvidence,
    "start": StartEvidence,
    "connectivity": ConnectivityEvidence,
}


def render_assembly_result(result: AssemblyResult) -> AssembleResponse:
    """Render an :class:`AssemblyResult` with the public response models.

    Shared by the one-shot ``/assemble`` endpoint and the ingestion
    completion path, so both always emit the exact same body for the same
    fragment multiset.
    """
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
        index = next(i for i, (a, b) in enumerate(zip(lo, hi)) if a != b)
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


# ---------------------------------------------------------------------------
# resumable ingestion
# ---------------------------------------------------------------------------


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    k: StrictInt = Field(
        ge=MIN_K,
        le=MAX_K,
        description="fragment length; every fragment has exactly k characters "
        "(must be a JSON integer, not a numeric string)",
    )
    start: str = Field(
        description="designated start prefix: exactly k-1 characters of A-Z / 0-9"
    )
    expected_total: StrictInt = Field(
        ge=0,
        le=MAX_FRAGMENTS,
        description="expected total number of fragments across all chunks; "
        "the received total may never exceed it",
    )


class ChunkSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: StrictInt = Field(
        ge=0,
        le=MAX_FRAGMENTS,
        description="zero-based chunk index; chunks may arrive in any order",
    )
    fragments: list[str] = Field(
        max_length=MAX_FRAGMENTS,
        description="fragments of this chunk; duplicates are kept by occurrence count",
    )
    complete: StrictBool = Field(
        default=False,
        description="declare this the final chunk: validate contiguity and "
        "totals, freeze the input, and return the assembly result directly",
    )


class ChunkAck(BaseModel):
    """Acknowledgement for a stored (or identically re-sent) chunk."""

    task_id: str
    index: int
    accepted: int = Field(description="fragments accepted from this chunk")
    received_total: int = Field(
        description="task-wide received fragment total right after this chunk"
    )


class TaskState(BaseModel):
    task_id: str
    k: int
    start: str
    expected_total: int
    received_total: int
    chunk_count: int
    status: Literal["open", "completed"]
    missing_indices: list[int] = Field(
        description="gaps below the highest received chunk index (capped); "
        "empty once the task is completed"
    )
    result: UniqueResponse | AmbiguousResponse | ImpossibleResponse | None = Field(
        default=None,
        description="stored assembly result, present once the task is completed",
    )
