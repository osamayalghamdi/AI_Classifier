"""Audit every stored service value against the taxonomy — find ALL violations.

Usage: python scripts/audit_offerings.py
"""
import json
import sys

sys.path.insert(0, ".")

from ai_classification.domain.taxonomy import SERVICES_BY_SYSTEM

# Load distinct service values from the DB via psql (no app DB client needed)
import subprocess

out = subprocess.run(
    ["docker", "exec", "ai_classifier-postgres-1", "psql", "-U", "aiuser", "-d", "ai_incidents",
     "-t", "-A", "-c",
     "SELECT DISTINCT classification_json::jsonb->>'service' FROM incidents "
     "WHERE classification_json::jsonb->>'service' IS NOT NULL AND classification_json::jsonb->>'service' != ''"],
    capture_output=True, text=True, check=True,
)
values = [v for v in out.stdout.splitlines() if v.strip()]

# Build valid sets
valid_bare = set()
valid_offerings = {}  # system -> service_key -> set(offerings)
for system, services in SERVICES_BY_SYSTEM.items():
    for svc_key, offerings in services.items():
        valid_bare.add(svc_key)
        valid_offerings[(system, svc_key)] = set(offerings)

print(f"{'SERVICE VALUE':<80} {'VERDICT':<12} DETAIL")
print("-" * 140)
violations = []
for svc in sorted(values):
    if svc in valid_bare:
        print(f"{svc:<80} {'OK-bare':<12}")
        continue
    if "." not in svc:
        violations.append((svc, "not in taxonomy (no dot, not a service)"))
        print(f"{svc:<80} {'INVALID':<12} not a taxonomy service")
        continue
    # dot-path: find the longest service key that is a prefix
    matched = None
    for system, services in SERVICES_BY_SYSTEM.items():
        for svc_key in services:
            if svc == svc_key or svc.startswith(svc_key + "."):
                if matched is None or len(svc_key) > len(matched[1]):
                    matched = (system, svc_key)
    if matched is None:
        violations.append((svc, "no service prefix matches"))
        print(f"{svc:<80} {'INVALID':<12} no service prefix in taxonomy")
        continue
    system, svc_key = matched
    offering = svc[len(svc_key) + 1:]
    if offering == "OFFERING-GAP":
        # Literal v3 sentinel: "Key.OFFERING-GAP" means the classifier
        # abstained (no listed offering fit; gap recorded in taxonomy_gaps).
        # A valid service-key prefix + the literal sentinel is legitimate.
        print(f"{svc:<80} {'OK-gap':<12} literal OFFERING-GAP sentinel (abstention)")
        continue
    valid = valid_offerings[(system, svc_key)]
    if offering in valid:
        print(f"{svc:<80} {'OK-offering':<12}")
    else:
        violations.append((svc, f"offering '{offering}' NOT in [{', '.join(sorted(valid))}]"))
        print(f"{svc:<80} {'INVALID':<12} offering '{offering}' not in taxonomy for {svc_key}")

print()
print(f"TOTAL distinct service values: {len(values)}")
print(f"VIOLATIONS: {len(violations)}")
for svc, detail in violations:
    print(f"  - {svc}\n      {detail}")
