"""Internal domain models — the LLM classification contract and derived types.
Pipeline position: 15_models — domain types (ClassificationResult, …)."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .taxonomy import (
    AffectedSystem,
    IncidentType,
    Severity,
    Urgency,
    Category,
    TicketKind,
    SERVICES_BY_SYSTEM,
    flatten_services,
)

# Flat service list derived from the hierarchy (built once at import time)
_FLAT_SERVICES = flatten_services()

# Abstention sentinel (classifier v3): when the ticket's problem genuinely
# matches no listed offering, stage 3 stores "<Service>.OFFERING-GAP" and
# records a taxonomy_gap row. The validator accepts this LITERAL sentinel
# and nothing else outside the taxonomy.
OFFERING_GAP_SENTINEL = ".OFFERING-GAP"


class ClassificationResult(BaseModel):
    """Structured output the LLM must return."""

    affected_system: AffectedSystem
    service: str
    incident_type: IncidentType | None = None
    severity: Severity | None = None
    urgency: Urgency | None = None
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
    # ── Stage-0 triage (classifier v3) ────────────────────────────────
    ticket_kind: TicketKind = TicketKind.incident
    # ── Persistence status: 'ok' | 'failed' (genuine LLM failure only) ──
    classification_status: Literal["ok", "failed"] = "ok"
    # ── Set by the (OFF-by-default) self-consistency pass ─────────────
    needs_review: bool = False
    # ── Provenance (set by the seams pipeline; empty for direct callers) ──
    model_version: str = Field(default="", description="Model identity that produced this classification.")
    prompt_version: str = Field(default="", description="System-prompt version identity that produced this classification.")

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
        # Abstention sentinel (classifier v3): "<Service>.OFFERING-GAP" is the
        # ONLY literal outside the taxonomy the validator accepts — the prefix
        # must be a real service key. Anything else still raises.
        if self.service.endswith(OFFERING_GAP_SENTINEL):
            key = self.service[: -len(OFFERING_GAP_SENTINEL)]
            for system, services in SERVICES_BY_SYSTEM.items():
                if key in services:
                    self.affected_system = system
                    return self
            raise ValueError(
                f"service '{self.service}' is not valid for "
                f"affected_system '{self.affected_system}': OFFERING-GAP "
                f"prefix '{key}' is not a service of any system"
            )
        # Dot-path form (cascade): "Service.Offering"
        if "." in self.service:
            own = SERVICES_BY_SYSTEM.get(self.affected_system, {})
            for key in own:
                if self.service == key or self.service.startswith(key + "."):
                    offering = self.service[len(key) + 1:] if self.service != key else ""
                    if offering and offering not in own[key]:
                        raise ValueError(
                            f"offering '{offering}' is not valid for service "
                            f"'{key}' of affected_system '{self.affected_system}'. "
                            f"Allowed offerings: {own[key]}"
                        )
                    return self
            for system, services in SERVICES_BY_SYSTEM.items():
                if system == self.affected_system:
                    continue
                for key in services:
                    if self.service == key or self.service.startswith(key + "."):
                        offering = self.service[len(key) + 1:] if self.service != key else ""
                        if offering and offering not in services[key]:
                            raise ValueError(
                                f"offering '{offering}' is not valid for service "
                                f"'{key}' of affected_system '{system.value}'. "
                                f"Allowed offerings: {services[key]}"
                            )
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
