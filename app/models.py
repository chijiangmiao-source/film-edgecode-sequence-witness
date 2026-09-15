"""Pydantic request/response models for the assembly API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_FRAGMENTS = 20_000
MIN_K = 3
MAX_K = 12
# A complete assembly is the (k-1)-char start prefix plus one character per
# fragment, so it can never exceed (MAX_K - 1) + MAX_FRAGMENTS characters.
MAX_CANDIDATE_LENGTH = (MAX_K - 1) + MAX_FRAGMENTS


class AssembleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    k: int = Field(
        ge=MIN_K,
        le=MAX_K,
        description="fragment length; every fragment has exactly k characters",
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
