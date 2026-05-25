from typing import Literal
from pydantic import BaseModel


class Verdict(BaseModel):
    classification: Literal["green", "yellow", "red"]
    confidence: float
    reasoning: str
    flags: list[str]
    release_age_hours: float | None = None  # passed through for per-repo age gate enforcement
    new_dependency_count: int = 0  # passed through for per-repo max_new_dependencies gate
