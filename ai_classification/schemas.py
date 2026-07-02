"""Predefined classification taxonomies for incident tickets.

Each field is a fixed set of labels the LLM must choose from.
Add or edit these lists as your domain grows — no code changes needed
anywhere else.
"""

from enum import StrEnum


class AffectedSystem(StrEnum):
    crm = "CRM"
    erp = "ERP"
    payment_gateway = "Payment Gateway"
    infrastructure = "Infrastructure"
    network = "Network"
    security = "Security"
    email = "Email"
    data_pipeline = "Data Pipeline"
    other = "Other"


class IncidentType(StrEnum):
    spike = "Spike"
    degradation = "Degradation"
    unavailability = "Unavailability"
    outage = "Outage"


class Severity(StrEnum):
    critical = "Critical"
    major = "Major"
    minor = "Minor"
    cosmetic = "Cosmetic"


# Highest severity = highest rank; use with max(..., key=SEVERITY_RANK.get)
# instead of max() directly — Severity values sort alphabetically wrong
# ("Minor" > "Critical").
SEVERITY_RANK: dict[str, int] = {
    s.value: i for i, s in enumerate(reversed(list(Severity)))
}


class Urgency(StrEnum):
    immediate = "Immediate"
    high = "High"
    medium = "Medium"
    low = "Low"


class Category(StrEnum):
    hardware = "Hardware"
    software = "Software"
    network_issue = "Network Issue"
    security = "Security"
    performance = "Performance"
    configuration = "Configuration"
    human_error = "Human Error"
    external = "External / Third Party"
    other = "Other"


# ── Service depends on the affected system, so we map it separately ──

SERVICES_BY_SYSTEM: dict[AffectedSystem, list[str]] = {
    AffectedSystem.crm: [
        "Sales Module", "Customer Portal", "Lead Management",
        "Reporting", "API Gateway",
    ],
    AffectedSystem.erp: [
        "Inventory", "Procurement", "HR Module",
        "Finance", "Supply Chain",
    ],
    AffectedSystem.payment_gateway: [
        "Checkout", "Refunds", "Fraud Detection",
        "Billing", "Invoice Generation",
    ],
    AffectedSystem.infrastructure: [
        "Compute (EC2 / VMs)", "Storage", "Load Balancer",
        "DNS", "Container Orchestrator",
    ],
    AffectedSystem.network: [
        "VPN", "Firewall", "CDN", "DNS Resolution", "Peering",
    ],
    AffectedSystem.security: [
        "IAM", "SIEM", "DLP", "Endpoint Protection", "Vulnerability Scanner",
    ],
    AffectedSystem.email: [
        "SMTP Relay", "Spam Filter", "Mailbox Store", "Mailing Lists",
    ],
    AffectedSystem.data_pipeline: [
        "ETL Jobs", "Streaming", "Data Warehouse", "Analytics API",
    ],
    AffectedSystem.other: ["General / Unspecified"],
}
