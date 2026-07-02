"""Seed mock_incidents.json into the running classifier via /classify, then print the reports."""
import json
import time
import urllib.request

API_BASE = "http://localhost:8000"

with open("mock_incidents.json") as f:
    incidents = json.load(f)


def classify(title, description):
    url = f"{API_BASE}/classify"
    data = json.dumps({"title": title, "description": description}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


for i, inc in enumerate(incidents):
    result = classify(inc["title"], inc["description"])
    status = "OK" if "incident_id" in result else "FAIL"
    cls = result.get("classification") or {}
    system = cls.get("affected_system", "?")
    sev = cls.get("severity", "?")
    print(f"[{i+1:02d}/{len(incidents)}] {status} | {system:16s} | {sev:10s} | {inc['title'][:55]}")
    if "error" in result:
        print(f"  ERROR: {result['error']}")
    time.sleep(0.3)

print("\n========== FINAL REPORT ==========")
for report_type in ["daily", "weekly"]:
    url = f"{API_BASE}/reports/{report_type}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        report = json.loads(resp.read())
    print(f"\n=== {report_type.upper()} REPORT ===")
    print(f"Total incidents: {report['total_incidents']}")
    for cluster in report["clusters"]:
        print(f"\n  [{cluster['worst_severity']}] {cluster['affected_system']} / {cluster['affected_service']} — {cluster['count']} incidents")
        print(f"    Summary: {cluster['summary'][:200]}")
        for inc in cluster["incidents"]:
            print(f"    - {inc['title']} ({inc['severity']})")
