"""End-to-end test with real Qwen2.5:1.5b via Ollama.

Tests the full classify() pipeline with a real LLM to verify:
  - JSON output is parseable
  - Pydantic validation passes (strict gatekeeper)
  - Retry mechanism kicks in if needed
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai_classification.classifier import classify
from ai_classification.models import ClassificationResult

PASS = 0
FAIL = 0


def check(label: str, title: str, description: str):
    global PASS, FAIL
    print(f"\n  [{label}]")
    print(f"    Title:       {title}")
    print(f"    Description: {description[:80]}...")
    try:
        result = classify(title, description)
        print(f"    ✅  OK  affected_system={result.affected_system}")
        print(f"            service={result.service}")
        print(f"            incident_type={result.incident_type}")
        print(f"            severity={result.severity}")
        print(f"            urgency={result.urgency}")
        print(f"            category={result.category}")
        print(f"            confidence={result.confidence}")
        print(f"            reasoning={result.reasoning}")
        PASS += 1
    except Exception as e:
        print(f"    ❌  FAIL — {e}")
        FAIL += 1


# ── Test cases ────────────────────────────────────────────────────────

print("=" * 60)
print("E2E: Qwen2.5:1.5b — Incident Classification")
print("=" * 60)

check(
    "payment-outage",
    "Payment gateway down",
    "All payment transactions are failing with 503 errors. "
    "Users cannot complete purchases. This has been going on for 30 minutes.",
)

check(
    "crm-slow",
    "CRM portal is slow",
    "Customer portal taking 15+ seconds to load pages. "
    "Approximately 200 users affected in the sales team.",
)

check(
    "vpn-flapping",
    "VPN keeps disconnecting",
    "Remote employees in Dubai office report VPN drops every few minutes. "
    "They can reconnect but it's disrupting work.",
)

check(
    "ssl-expired",
    "SSL certificate expired on customer portal",
    "Users see a security warning when visiting https://portal.example.com. "
    "Certificate expired March 15 2025.",
)

check(
    "disk-full",
    "Production database disk is 98% full",
    "The PostgreSQL primary server /data partition is at 98% capacity. "
    "Auto-vacuum may fail soon. Need to add storage or clean old WAL logs.",
)

check(
    "email-spam",
    "Legitimate emails being marked as spam",
    "Multiple users report that internal company emails are going to "
    "recipients' spam folders. SMTP relay shows no errors.",
)

check(
    "data-pipeline-failure",
    "ETL job failed at 3 AM",
    "The nightly ETL job that syncs CRM data to the data warehouse "
    "failed with a connection timeout. Data is 6 hours stale.",
)

# ── Summary ───────────────────────────────────────────────────────────

print()
print("=" * 60)
print(f"  Results: {PASS} passed, {FAIL} failed")
print("=" * 60)

sys.exit(0 if FAIL == 0 else 1)
