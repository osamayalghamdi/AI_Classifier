"""Pydantic models for the API — request, response, and the
structured classification result the LLM produces.
"""

from pydantic import BaseModel, Field

from .schemas import (
    AffectedSystem,
    IncidentType,
    Severity,
    Urgency,
    Category,
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


class ClassifyResponse(BaseModel):
    incident_title: str
    classification: ClassificationResult
