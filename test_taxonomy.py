"""Test all test_incidents.json against the taxonomy.
Uses keyword matching to validate taxonomy coverage without LLM calls.
Also offers optional LLM-based classification for a subset."""

import json
import re
import sys
from collections import Counter

sys.path.insert(0, ".")

from ai_classification.domain.taxonomy import (
    AffectedSystem, SERVICES_BY_SYSTEM, flatten_services,
)


# ── Keyword maps ─────────────────────────────────────────────────────

# Map common keywords to (system, service, offering)
KEYWORD_MAP = {
    # Rawdah permits
    "rawdah": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Issue Permits"),
    "rawda": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Issue Permits"),
    "rawdha": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Issue Permits"),
    "rowdah": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Issue Permits"),
    "permit": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Issue Permits"),
    "تصريح": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Issue Permits"),
    "روضة": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Issue Permits"),
    "الروضة": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Issue Permits"),
    "زيارة الروضة": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Issue Permits"),
    "حجز الروضة": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Issue Permits"),
    
    # Company evaluation
    "تقييم الشركات": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    "تقييم": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    "دورات تقييم": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    "تقيم": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    "تقييم أداء": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    "اعتراض": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    "evaluation": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    
    # Reports / Incident tracking
    "بلاغ": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    "البلاغ": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    "البلاغات": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    "صفحة البلاغات": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    "إغلاق بلاغ": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    "حالة البلاغ": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    
    # Transport / Movement
    "طلب تنقل": ("Nusuk Masar Haj", "Between cities - Nusuk Masar Haj", "Create between cities  Request"),
    "اعتماد": ("Nusuk Masar Haj", "Between cities - Nusuk Masar Haj", "Create between cities  Request"),
    "اعتماد طلبات": ("Nusuk Masar Haj", "Between cities - Nusuk Masar Haj", "Create between cities  Request"),
    "حافل": ("Nusuk Masar Haj", "Between cities - Nusuk Masar Haj", "Manage Buses"),
    "مغادرة": ("Nusuk Masar Haj", "Between cities - Nusuk Masar Haj", "Create Final departure  Request"),
    "final departure": ("Nusuk Masar Haj", "Between cities - Nusuk Masar Haj", "Create Final departure  Request"),
    "كشف إركاب": ("Nusuk Masar Haj", "Istiqbal - Nusuk Masar Haj", "Create Passenger Manifest"),
    "passenger manifest": ("Nusuk Masar Haj", "Istiqbal - Nusuk Masar Haj", "Create Passenger Manifest"),
    
    # Accommodation / Housing
    "سكن": ("Nusuk Masar Haj", "contracts - Nusuk Masar Haj", "Contract with Accommodation Service Providers"),
    "تسكين": ("Nusuk Masar Haj", "contracts - Nusuk Masar Haj", "Contract with Accommodation Service Providers"),
    "عقد سكن": ("Nusuk Masar Haj", "contracts - Nusuk Masar Haj", "Contract with Accommodation Service Providers"),
    "فندق": ("Nusuk Masar Haj", "contracts - Nusuk Masar Haj", "Contract with Accommodation Service Providers"),
    "الوصول الفعلي": ("Nusuk Masar Haj", "Pre Arrival - Nusuk Masar Haj", "Confirm Pre Arrival Data"),
    "check-in": ("Nusuk Masar Haj", "Pre Arrival - Nusuk Masar Haj", "Confirm Pre Arrival Data"),
    "وصول الفعلي": ("Nusuk Masar Haj", "Pre Arrival - Nusuk Masar Haj", "Confirm Pre Arrival Data"),
    
    # Tax / Invoicing
    "ضريبي": ("Nusuk Masar Haj", "7.1 Invoicing and Billing - Nusuk Masar Haj", "Bill Generation"),
    "الضريبي": ("Nusuk Masar Haj", "7.1 Invoicing and Billing - Nusuk Masar Haj", "Bill Generation"),
    "فواتير": ("Nusuk Masar Haj", "7.1 Invoicing and Billing - Nusuk Masar Haj", "Bill Generation"),
    "فوترة": ("Nusuk Masar Haj", "7.1 Invoicing and Billing - Nusuk Masar Haj", "Bill Generation"),
    "فاتورة": ("Nusuk Masar Haj", "7.1 Invoicing and Billing - Nusuk Masar Haj", "Bill Generation"),
    "ضريبة": ("Nusuk Masar Haj", "7.1 Invoicing and Billing - Nusuk Masar Haj", "Bill Generation"),
    "VAT": ("Nusuk Masar Haj", "7.1 Invoicing and Billing - Nusuk Masar Haj", "Bill Generation"),
    
    # Financial
    "تحويل": ("Nusuk Masar Haj", "Financial Transactions - Nusuk Masar Haj", "view tansactions list"),
    "تحويلات": ("Nusuk Masar Haj", "Financial Transactions - Nusuk Masar Haj", "view tansactions list"),
    "سداد": ("Nusuk Masar Haj", "Funds & Refund Management - Nusuk Masar Haj", "Create Refund Money Request"),
    "مبلغ": ("Nusuk Masar Haj", "Funds & Refund Management - Nusuk Masar Haj", "Create Refund Money Request"),
    "محفظ": ("Nusuk Masar Haj", "7.2 Bank Account Management - Nusuk Masar Haj", "Load the electronic wallet via bank transfer"),
    "مبالغ": ("Nusuk Masar Haj", "Funds & Refund Management - Nusuk Masar Haj", "Create Refund Money Request"),
    "الدفع": ("Nusuk Masar Haj", "7.1 Invoicing and Billing - Nusuk Masar Haj", "Bill Payment"),
    
    # Violations / Penalties
    "مخالفات": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    "مخالفة": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    "تظلم": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    "الاعتراض": ("Nusuk Masar Haj", "inquiry - Nusuk Masar Haj", "inquiry"),
    
    # Visa / Passport
    "تأشيرة": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Issue Visas"),
    "تاشير": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Issue Visas"),
    "جواز": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Select Pilgrims"),
    "جوازات": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Select Pilgrims"),
    "معالجة": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Create Group"),
    "حالة جاري المعالجة": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Create Group"),
    
    # License / Commercial Registration
    "سجل تجاري": ("Nusuk Masar Haj", "licensing - Nusuk Masar Haj", ""),
    "license": ("Nusuk Masar Haj", "licensing - Nusuk Masar Haj", ""),
    "تصريح": ("Nusuk Masar Haj", "pilgrim groups and issue permit - Nusuk Masar Haj", "Issue Permits"),
    "licensing": ("Nusuk Masar Haj", "licensing - Nusuk Masar Haj", ""),
    
    # CRM
    "crm": ("Nusuk Masar Haj", "System/Application - Nusuk Masar Haj", "Service Unavailability"),
    "CRM": ("Nusuk Masar Haj", "System/Application - Nusuk Masar Haj", "Service Unavailability"),
    
    # DB / System
    "db cpu": ("Nusuk Masar Haj", "System/Application - Nusuk Masar Haj", "Backend Latency"),
    "high cpu": ("Nusuk Masar Haj", "System/Application - Nusuk Masar Haj", "Backend Latency"),
    "database": ("Nusuk Masar Haj", "System/Application - Nusuk Masar Haj", "Backend Latency"),
    "server": ("Nusuk Masar Haj", "System/Application - Nusuk Masar Haj", "Service Unavailability"),
    
    # Suggestion
    "مقترح": ("Nusuk Masar Haj", "suggestion - Nusuk Masar Haj", "suggestion"),
    "suggestion": ("Nusuk Masar Haj", "suggestion - Nusuk Masar Haj", "suggestion"),
    
    # Registration
    "تسجيل": ("Nusuk Masar Haj", "Registration - Nusuk Masar Haj", "Register a new user on the platform"),
    "nafath": ("Nusuk Masar Haj", "Registration - Nusuk Masar Haj", "register with nafath"),
    "نفاذ": ("Nusuk Masar Haj", "Registration - Nusuk Masar Haj", "register with nafath"),
}


def match_keywords(text: str) -> list[tuple[str, str, str, str]]:
    """Return list of (keyword, system, service, offering) matches."""
    text_lower = text.lower()
    matches = []
    for kw, (sys_name, svc, off) in sorted(KEYWORD_MAP.items(), key=lambda x: -len(x[0])):
        if kw.lower() in text_lower:
            matches.append((kw, sys_name, svc, off))
    return matches


def get_best_match(matches: list) -> tuple | None:
    """Pick the best match — prefer longer/more specific keyword."""
    if not matches:
        return None
    # Sort by keyword length descending (most specific first)
    matches.sort(key=lambda m: -len(m[0]))
    return matches[0][1:]  # (system, service, offering)


def validate_service_in_taxonomy(system_name: str, service_name: str) -> bool:
    """Check that service exists under system in the taxonomy."""
    try:
        system = AffectedSystem(system_name)
    except ValueError:
        return False
    services = flatten_services()
    if system not in services:
        return False
    return service_name in services[system]


def main():
    incidents_path = "test_incidents.json"
    with open(incidents_path) as f:
        incidents = json.load(f)
    
    print(f"Loaded {len(incidents)} test incidents\n")
    
    system_counts = Counter()
    service_counts = Counter()
    unmatched = []
    taxonomy_issues = []
    
    for i, inc in enumerate(incidents, 1):
        title = inc.get("DisplayLabel", "")
        desc = inc.get("Description", "")
        combined = f"{title} {desc}"
        
        matches = match_keywords(combined)
        best = get_best_match(matches)
        
        if best:
            system_name, service_name, offering = best
            system_counts[system_name] += 1
            service_counts[service_name] += 1
            
            # Validate existence in taxonomy
            if not validate_service_in_taxonomy(system_name, service_name):
                taxonomy_issues.append({
                    "index": i,
                    "title": title[:50],
                    "system": system_name,
                    "service": service_name,
                })
        else:
            unmatched.append({"index": i, "title": title[:60], "desc": desc[:80]})
    
    # ── Summary ──
    print("=" * 65)
    print("TAXONOMY COVERAGE TEST — KEYWORD MATCHING")
    print("=" * 65)
    
    matched_count = len(incidents) - len(unmatched)
    pct = matched_count / len(incidents) * 100
    print(f"\nMatched:       {matched_count}/{len(incidents)} ({pct:.0f}%)")
    print(f"Unmatched:     {len(unmatched)}")
    print(f"Taxonomy gaps: {len(taxonomy_issues)}")
    print()
    
    print("Systems:")
    for s, c in system_counts.most_common():
        print(f"  {s:<30s} {c}")
    print()
    
    print("Top Services:")
    for s, c in service_counts.most_common(10):
        print(f"  {s:<55s} {c}")
    print()
    
    if unmatched:
        print("UNMATCHED INCIDENTS:")
        print(f"  {'Index':<6} {'Title':<60}")
        print(f"  {'-'*66}")
        for u in unmatched:
            print(f"  [{u['index']:<3}] {u['title'][:58]}")
        print()
        print("  Unmatched incidents suggest taxonomy gaps.")
        print("  These incidents may belong to services/offerings not yet in the taxonomy.")
        print()
    
    if taxonomy_issues:
        print("TAXONOMY VALIDATION ISSUES:")
        for ti in taxonomy_issues:
            print(f"  [{ti['index']}] {ti['title'][:50]}")
            print(f"    → System='{ti['system']}' Service='{ti['service']}' NOT IN TAXONOMY")
        print()
    
    # Print all incidents with their mapped service
    print("\nFULL MATRIX:")
    print(f"  {'#':<4} {'Service':<55} {'Offering':<45} {'Match?'}")
    print(f"  {'-'*128}")
    for i, inc in enumerate(incidents, 1):
        title = inc.get("DisplayLabel", "")[:40]
        matches = match_keywords(f"{inc.get('DisplayLabel','')} {inc.get('Description','')}")
        best = get_best_match(matches)
        if best:
            svc = best[1]
            off = best[2]
            print(f"  [{i:<2}] {svc[:53]:<55} {off[:43]:<45} ✅")
        else:
            print(f"  [{i:<2}] {'—':<55} {'—':<45} ❌")


if __name__ == "__main__":
    main()
