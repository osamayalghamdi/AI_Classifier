"""Internal domain models — the LLM classification contract and derived types."""

from pydantic import BaseModel, Field, model_validator

from .taxonomy import AffectedSystem, IncidentType, Severity, Urgency, Category, SERVICES_BY_SYSTEM


class ClassificationResult(BaseModel):
    """Structured output the LLM must return."""

    affected_system: AffectedSystem
    service: str
    incident_type: IncidentType
    severity: Severity
    urgency: Urgency
    category: Category
    confidence: str = Field(pattern=r"^(low|medium|high)$")
    reasoning: str | None = None
    canonical_statement: str = Field(
        description="One dense sentence in English stating only observable "
        "symptoms and conditions. Include: what happened, when it started "
        "(if stated), scope (which users/environments), and inconsistency "
        "if mentioned. Never guess root cause. Write in English regardless "
        "of ticket language. Optimized for embedding similarity."
    )

    @model_validator(mode="after")
    def _check_service_in_system(self) -> "ClassificationResult":
        """Auto-correct affected_system if the service belongs to a different one."""
        allowed = SERVICES_BY_SYSTEM.get(self.affected_system, [])
        if self.service in allowed:
            return self
        for system, services in SERVICES_BY_SYSTEM.items():
            if self.service in services:
                self.affected_system = system
                return self
        raise ValueError(
            f"service '{self.service}' is not valid for "
            f"affected_system '{self.affected_system}'. Allowed: {allowed}"
        )


class SimilarOpenIncident(BaseModel):
    """An active (unresolved) incident similar enough to be a likely duplicate."""

    id: str
    title: str
    similarity: float = Field(ge=0.0, le=1.0)
    classification: ClassificationResult
    canonical_statement: str = Field(default="")
