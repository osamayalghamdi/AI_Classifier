#!/usr/bin/env python3
"""
Presentation demo — AI Incident Classifier E2E walkthrough.

Runs against a running instance of the API (default http://localhost:8082).
Usage:
    python demo_presentation.py
    python demo_presentation.py --url http://192.168.1.50:8082
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8082/api"
PASS = 0
FAIL = 0


def api(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode()}


def heading(text: str):
    print()
    print("=" * 62)
    print(f"  {text}")
    print("=" * 62)


def step(label: str, detail: str = ""):
    print(f"\n  ► {label}")
    if detail:
        for line in detail.split("\n"):
            print(f"    {line}")


def ok(text: str):
    global PASS
    PASS += 1
    print(f"    ✅  {text}")


def fail(text: str):
    global FAIL
    FAIL += 1
    print(f"    ❌  {text}")


# ── Scenario incidents ────────────────────────────────────────────

INCIDENTS = [
    # Payment gateway outage chain
    {
        "label": "1-payment-outage",
        "title": "Payment gateway down — all transactions failing",
        "desc": "All payment transactions are returning 503 errors since 14:32 UTC. Payment provider status page shows a confirmed outage.",
    },
    {
        "label": "2-payment-partial",
        "title": "Checkout timeouts for EU users",
        "desc": "Users in Europe see intermittent 504 errors at checkout. Some transactions go through after retrying 3-4 times.",
    },
    {
        "label": "3-payment-refunds",
        "title": "Refund processing queue stuck",
        "desc": "The refund batch job has been stuck for 2 hours. About 150 refund requests are queued and not processing.",
    },
    # CRM / portal incidents
    {
        "label": "4-crm-slow",
        "title": "CRM portal slow to load",
        "desc": "Customer portal taking 15+ seconds to load pages. Sales team of 200 users affected since 10 AM.",
    },
    {
        "label": "5-crm-ssl",
        "title": "SSL certificate expired on customer portal",
        "desc": "Users see a security warning when visiting the portal. Certificate expired 2 days ago.",
    },
    # Network incidents
    {
        "label": "6-vpn-flapping",
        "title": "VPN tunnel to Dubai office keeps disconnecting",
        "desc": "Site-to-site VPN between HQ and Dubai office drops and reconnects every 5 minutes. 50 users affected.",
    },
    {
        "label": "7-dns-issue",
        "title": "DNS resolution failing for internal services",
        "desc": "Multiple internal services unreachable via hostname. Direct IP access works. DNS server responding with NXDOMAIN for internal zones.",
    },
    # Security incident
    {
        "label": "8-brute-force",
        "title": "Brute-force attack on admin panel",
        "desc": "5000+ failed login attempts from 12 different IPs targeting the admin interface. Possible coordinated attack.",
    },
    # Infrastructure
    {
        "label": "9-disk-full",
        "title": "Production database disk at 98%",
        "desc": "PostgreSQL /data partition at 98% capacity on primary server. Auto-vacuum may fail. Need immediate storage review.",
    },
    # Standalone odd-one-out
    {
        "label": "10-email-spam",
        "title": "Legitimate emails marked as spam",
        "desc": "Multiple users report internal company emails going to spam folders. SMTP relay shows no errors on our end.",
    },
]


def run():
    heading("AI Incident Classifier — Live Demo")
    step("Base URL", BASE_URL)
    step("Model", api("GET", "/health").get("model", "unknown"))

    # ── 1. Health check ──────────────────────────────────────────
    heading("1. Health check")
    health = api("GET", "/health")
    print(f"    Status:      {health.get('status')}")
    print(f"    Model:       {health.get('model')}")
    print(f"    Store ready: {health.get('store_ready')}")
    if health.get("status") == "ok":
        ok("API is live")
    else:
        fail("API not healthy — aborting")
        return

    # ── 2. Classify incidents ────────────────────────────────────
    heading("2. Classify all incidents")
    ids = {}
    for inc in INCIDENTS:
        step(inc["label"], f'Title: {inc["title"]}')
        resp = api("POST", "/classify", {
            "title": inc["title"],
            "description": inc["desc"],
        })
        if "incident_id" in resp:
            ids[inc["label"]] = resp["incident_id"]
            cls = resp["classification"]
            print(f"      ID:     {resp['incident_id']}")
            print(f"      System: {cls['affected_system']}")
            print(f"      Type:   {cls['incident_type']}")
            print(f"      Severity: {cls['severity']}  |  Urgency: {cls['urgency']}")
            print(f"      Category: {cls['category']}  |  Confidence: {cls['confidence']}")
            if cls['reasoning']:
                print(f"      Why:    {cls['reasoning']}")
            related = resp.get("related_incidents", [])
            if related:
                print(f"      Related: {len(related)} similar incident(s)")
                for r in related[:2]:
                    print(f"        · {r['title']}  (sim={r['similarity']})")
            ok("Classified")
        else:
            print(f"      ERROR: {resp}")
            fail("Classification failed")

    # ── 3. Show daily report ─────────────────────────────────────
    heading("3. Daily report (clusters formed)")
    report = api("GET", "/reports/daily")
    print(f"    Period:          {report.get('period')}")
    print(f"    Total incidents: {report.get('total_incidents')}")
    print(f"    Clusters:        {len(report.get('clusters', []))}")
    for c in report.get("clusters", []):
        print(f"\n    ── Cluster: {c['cluster_id'][:8]}…")
        print(f"       Summary: {c['summary']}")
        print(f"       {c['affected_system']} / {c['affected_service']}")
        print(f"       {c['count']} incidents  |  Worst severity: {c['worst_severity']}")
    if report.get("clusters"):
        ok(f"{len(report['clusters'])} clusters formed automatically")
    else:
        fail("No clusters formed")

    # ── 4. Summary ───────────────────────────────────────────────
    heading("Summary")
    print(f"""
    Classified:  {len(ids)} incidents
    Clusters:    {len(report.get('clusters', []))} groups formed automatically
    Pipeline:
      1. LLM classifies (system, type, severity, urgency, category)
      2. Embedding cosine similarity finds related incidents
      3. Incremental clustering groups related incidents
      4. LLM summarizes each cluster (2-3 sentences)
      5. Reports available via /reports/daily and /reports/weekly

    Key architecture:
      • LLM used only for classification + cluster summarization
      • Everything else: embedding cosine similarity + SQL
      • Reports return in <10ms with zero LLM calls
    """)

    print(f"\n  {'─' * 58}")
    print(f"  Results: {PASS} passed, {FAIL} failed")
    print(f"  {'─' * 58}")

    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Presentation demo for AI Incident Classifier")
    parser.add_argument("--url", default=BASE_URL, help="API base URL (e.g. http://192.168.1.50:8082/api)")
    args = parser.parse_args()
    BASE_URL = args.url
    run()
