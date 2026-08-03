"""Internal domain models — the LLM classification contract and derived types.
Pipeline position: 15_models — domain types (ClassificationResult, …)."""

from pydantic import BaseModel, Field, model_validator

from .taxonomy import AffectedSystem, IncidentType, Severity, Urgency, Category, SERVICES_BY_SYSTEM, flatten_services

# Flat service list derived from the hierarchy (built once at import time)
_FLAT_SERVICES = flatten_services()


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
    signature: str = Field(
        description="Short problem signature for matching: max 12 words, "
        "format: [Who] can't do [what] in [which step] because of [what]. "
        "No company names, no ticket IDs, no dates, no numbers, no counts. "
        "English only. This is used for embedding/grouping, not display."
    )
    failure_mode: str = Field(
        default="FM-000",
        description="Failure mode code from the taxonomy. Pick the best match from FAILURE_MODES. If none matches, use FM-000 (unclassified / new)."
    )

    @model_validator(mode="after")
    def _check_service_in_system(self) -> "ClassificationResult":
        """Auto-correct affected_system if the service belongs to a different one.

        Bare-service values keep the legacy flat-list behavior byte-identical.
        Dot-path values ("Service.Offering", cascade mode) validate only the
        SERVICE part — the longest prefix that is a key of SERVICES_BY_SYSTEM.
        Service names may themselves contain dots (e.g.
        "7.1 Invoicing and Billing - Nusuk Masar Haj") while offerings never
        do, so the value is never split on the first dot; the offering part is
        not validated.
        """
        allowed = _FLAT_SERVICES.get(self.affected_system, [])
        if self.service in allowed:
            return self
        for system, services in _FLAT_SERVICES.items():
            if self.service in services:
                self.affected_system = system
                return self
        # Dot-path form (cascade): "Service.Offering"
        if "." in self.service:
            own = SERVICES_BY_SYSTEM.get(self.affected_system, {})
            for key in own:
                if self.service == key or self.service.startswith(key + "."):
                    return self
            for system, services in SERVICES_BY_SYSTEM.items():
                if system == self.affected_system:
                    continue
                for key in services:
                    if self.service == key or self.service.startswith(key + "."):
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
