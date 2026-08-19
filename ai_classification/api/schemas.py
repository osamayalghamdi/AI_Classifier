"""Pydantic request/response models for the HTTP API.
Pipeline position: 50_api — request/response schemas."""

from pydantic import BaseModel, Field

from ..domain.models import ClassificationResult, SimilarOpenIncident


class ClassifyRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=8000)
    extracted_text: str = Field(default="", max_length=20000)
    documents: list[str] = Field(default_factory=list)
    assign_group: str = Field(default="")
    assignee: str = Field(default="")
    priority: str = Field(default="medium")
    notes: str | None = None
    discussion_history: list[dict] = Field(default_factory=list)
    escalation_info: str | None = None
    completion_code: str | None = None
    source_ticket_id: str = Field(default="", description="Originating ticket ID. Used for exact-match deduplication.")
    affected_system: str = Field(default="", description="Affected system supplied by the ticketing system. The classifier validates and pins it (LLM system resolution only when empty).")


class ClassifyResponse(BaseModel):
    incident_title: str
    classification: ClassificationResult
    incident_id: str | None = None
    similar_open_incidents: list[SimilarOpenIncident] = Field(default_factory=list)


class ResolveResponse(BaseModel):
    incident_id: str
    status: str


class ClassifyBatchRequest(BaseModel):
    incidents: list[ClassifyRequest] = Field(min_length=1, max_length=50)


class ClassifyBatchResponse(BaseModel):
    results: list[ClassifyResponse]
    total: int
    failed: int


class BulkImportItem(BaseModel):
    DisplayLabel: str = Field("", description="Incident title. Maps to 'title' internally.")
    Description: str = Field("", description="Incident description. Maps to 'description' internally.")


class BulkImportRequest(BaseModel):
    incidents: list[BulkImportItem] = Field(min_length=1, description="Array of incidents with DisplayLabel and Description fields")
