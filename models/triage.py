from pydantic import BaseModel

from models.verdict import Verdict
from models.package import PackageChecks


class TriageResult(BaseModel):
    """Return value of PackageTriageWorkflow — verdict plus the raw check signals."""

    verdict: Verdict
    signals: PackageChecks
