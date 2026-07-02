"""Pydantic models for API requests, responses, and the LLM classification output."""

from pydantic import BaseModel, Field, field_validator, model_validator

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


class RerankItem(BaseModel):
    """One LLM-ranked candidate from `llm_rerank_similar`'s raw JSON array output."""

    id: str
    similarity: int
    reasoning: str = ""

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: object) -> object:
        # LLMs sometimes echo the candidate id as a bare JSON number.
        return str(v) if isinstance(v, (int, float)) else v

    @field_validator("similarity")
    @classmethod
    def _clamp_similarity(cls, v: int) -> int:
        # LLMs occasionally return values outside 0-100 — clamp rather than reject
        # so one sloppy field doesn't discard an otherwise-usable match.
        return max(0, min(100, v))


class RelatedIncident(BaseModel):
    id: str
    title: str
    similarity: float = Field(ge=0.0, le=1.0)
    classification: ClassificationResult
    reasoning: str | None = None


class ClassifyResponse(BaseModel):
    incident_title: str
    classification: ClassificationResult
    incident_id: str | None = None
    related_incidents: list[RelatedIncident] = Field(default_factory=list)


class ReportCluster(BaseModel):
    cluster_id: str
    summary: str
    affected_system: str
    affected_service: str
    count: int
    worst_severity: str
    incidents: list[dict]


class ReportResponse(BaseModel):
    period: str
    total_incidents: int
    clusters: list[ReportCluster]
