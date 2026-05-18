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
        """Validate that the service matches the chosen affected_system."""
        allowed = SERVICES_BY_SYSTEM.get(self.affected_system, [])
        if self.service not in allowed:
            raise ValueError(
                f"service '{self.service}' is not valid for "
                f"affected_system '{self.affected_system}'. "
                f"Allowed: {allowed}"
            )
        return self


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
