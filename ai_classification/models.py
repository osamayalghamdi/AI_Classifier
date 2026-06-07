"""Pydantic models for the API — request, response, and the
structured classification result the LLM produces.
"""

from pydantic import BaseModel, Field, model_validator

from .schemas import (
    AffectedSystem,
    IncidentType,
    Severity,
    Urgency,
    Category,
    SERVICES_BY_SYSTEM,
)


# ── Request ──


class ClassifyRequest(BaseModel):
    title: str = Field(description="Short incident title (subject line)")
    description: str = Field(description="Full incident description or body")
    extracted_text: str = Field(
        default="",
        description="OCR-extracted text from screenshot/PDF (optional, improves similarity)",
    )


# ── Response ──


class ClassificationResult(BaseModel):
    """Structured output the LLM must return, field by field."""

    affected_system: AffectedSystem
    service: str = Field(description="Service affected within the system")
    incident_type: IncidentType
    severity: Severity
    urgency: Urgency
    category: Category
    confidence: str = Field(
        pattern=r"^(low|medium|high)$",
        description="How confident the LLM is in its classification",
    )
    reasoning: str | None = Field(
        default=None,
        description="Brief explanation of the classification choices",
    )

    @model_validator(mode="after")
    def _check_service_in_system(self) -> "ClassificationResult":
        """Validate that the service matches the chosen affected_system.

        If the service is found under a *different* system, auto-correct
        ``affected_system`` to the right one — the service is the more
        precise signal.  If the service isn't in *any* system's list,
        raise as usual.
        """
        allowed = SERVICES_BY_SYSTEM.get(self.affected_system, [])

        # Fast path — already correct.
        if self.service in allowed:
            return self

        # Slow path — find the system that actually owns this service.
        for system, services in SERVICES_BY_SYSTEM.items():
            if self.service in services:
                self.affected_system = system
                return self

        raise ValueError(
            f"service '{self.service}' is not valid for "
            f"affected_system '{self.affected_system}'. "
            f"Allowed: {allowed}"
        )


class RelatedIncident(BaseModel):
    """A past incident that is semantically similar to the current one."""

    id: str = Field(description="Unique incident identifier")
    title: str = Field(description="Title of the past incident")
    similarity: float = Field(
        ge=0.0, le=1.0,
        description="Cosine similarity score (0=unrelated, 1=identical meaning)",
    )
    classification: ClassificationResult = Field(
        description="The classification that was applied to the past incident"
    )


class ClassifyResponse(BaseModel):
    incident_title: str
    classification: ClassificationResult
    incident_id: str | None = Field(
        default=None,
        description="Unique ID for this incident (set when storage is enabled)",
    )
    related_incidents: list[RelatedIncident] = Field(
        default_factory=list,
        description="Past incidents with similar meaning, sorted by similarity",
    )


# ── Report models ──


class ReportCluster(BaseModel):
    """One group of semantically similar incidents."""

    summary: str = Field(description="2–3 sentence summary of the cluster")
    affected_system: str
    affected_service: str
    count: int
    worst_severity: str
    incidents: list[dict] = Field(
        description="List of {id, title, severity, created_at} dicts"
    )


class ReportResponse(BaseModel):
    period: str = Field(description="Label for the report period, e.g. 'Today' or 'This week'")
    total_incidents: int
    clusters: list[ReportCluster]
