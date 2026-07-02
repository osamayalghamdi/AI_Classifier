"""Pydantic models for API requests, responses, and the LLM classification output."""

from pydantic import BaseModel, Field, model_validator

from .schemas import AffectedSystem, IncidentType, Severity, Urgency, Category, SERVICES_BY_SYSTEM


class ClassifyRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=8000)
    extracted_text: str = Field(default="", max_length=20000)


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


class ClassifyResponse(BaseModel):
    incident_title: str
    classification: ClassificationResult
    incident_id: str | None = None
    similar_open_incidents: list[SimilarOpenIncident] = Field(default_factory=list)


class ResolveResponse(BaseModel):
    incident_id: str
    status: str
