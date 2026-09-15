"""Semantic input-model checks that depend on ``k`` (run after schema validation)."""

from __future__ import annotations

from app.assembly import ALPHABET
from app.errors import ApiError

MAX_REPORTED_PROBLEMS = 20


def validate_start(k: int, start: str) -> None:
    expected = k - 1
    if len(start) != expected:
        raise ApiError(
            422,
            "INVALID_START",
            f"start prefix must have length k-1 = {expected}",
            {"expected_length": expected, "actual_length": len(start)},
        )
    bad = [
        {"index": i, "character": c}
        for i, c in enumerate(start)
        if c not in ALPHABET
    ]
    if bad:
        raise ApiError(
            422,
            "INVALID_START",
            "start prefix may only contain ASCII A-Z and 0-9",
            {"invalid_characters": bad[:MAX_REPORTED_PROBLEMS]},
        )


def validate_fragments(k: int, fragments: list[str]) -> None:
    problems: list[dict] = []
    for index, frag in enumerate(fragments):
        if len(frag) != k:
            problems.append(
                {
                    "index": index,
                    "fragment": frag,
                    "problem": f"length must equal k = {k}",
                    "actual_length": len(frag),
                }
            )
        else:
            bad = sorted({c for c in frag if c not in ALPHABET})
            if bad:
                problems.append(
                    {
                        "index": index,
                        "fragment": frag,
                        "problem": "characters outside A-Z0-9",
                        "invalid_characters": bad,
                    }
                )
        if len(problems) >= MAX_REPORTED_PROBLEMS:
            break
    if problems:
        raise ApiError(
            422,
            "INVALID_FRAGMENTS",
            "every fragment must have length k and use only ASCII A-Z / 0-9",
            {
                "problems": problems,
                "truncated": len(problems) >= MAX_REPORTED_PROBLEMS,
            },
        )
