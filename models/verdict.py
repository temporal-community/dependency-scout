from typing import Literal
from pydantic import BaseModel, Field


class Verdict(BaseModel):
    classification: Literal["green", "yellow", "red"]
    confidence: float
    reasoning: str = Field(
        description=(
            "One to three sentences summarising the key risk factors. "
            "Plain text only — no markdown, no bullet points, no numbered lists. "
            "Single paragraph."
        )
    )
    flags: list[str] = Field(
        description=(
            "Short human-readable phrases describing specific concerns, "
            "e.g. 'major version bump', 'release age 2 days', "
            "'new outbound network calls in library code'. "
            "Do NOT use snake_case field names from the input JSON."
        )
    )
    release_age_hours: float | None = None  # passed through for per-repo age gate enforcement
    new_dependency_count: int = 0  # passed through for per-repo max_new_dependencies gate
