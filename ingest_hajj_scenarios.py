"""Continue feeding the remaining Hajj/Umrah incidents, one by one, patiently."""
import json
import time
import urllib.request

API_BASE = "http://api:8000"

remaining = [
    # ── INSURANCE ──
    {
        "title": "Insurance covers wrong region for Umrah traveler",
        "description": "Health insurance policy for Moroccan mutamir covers only 'Non-Hajj' period. Umrah within Ramadan requires different coverage tier. Error: COVERAGE_REGION_MISMATCH. System did not validate coverage scope at purchase."
    },
    {
        "title": "Insurance provider Tawuniya not recognized by verification system",
        "description": "Mutamir purchased mandatory Umrah insurance from Tawuniya but the verification gateway returns error INSURANCE_PROVIDER_NOT_FOUND. All other providers (Bupa, Medgulf, AlRajhi Takaful) verify fine. Integration broken for Tawuniya only."
    },
    {
        "title": "Insurance claim denied for emergency hospitalization",
        "description": "Mutamir hospitalized in Makkah with acute chest pain after Tawaf. Insurance company denies claim citing 'pre-existing cardiac condition' — reference CLAIM-2026-38472. Mutamir had no prior cardiac diagnosis. No appeal mechanism in system."
    },
    {
        "title": "Insurance renewal payment rejected by bank",
        "description": "Failed to renew mandatory Umrah health insurance for 5 mutamir in one group. Credit card payment declined by Al Rajhi Bank. Error: PAYMENT_DECLINED_CARD_ISSUER. System retry limit exhausted after 3 attempts."
    },
    {
        "title": "Insurance certificate not issued after payment confirmation",
        "description": "Mutamir paid 850 SAR for mandatory Umrah health insurance via Mada card. Payment confirmed (transaction REF: MADA-8847291) but insurance certificate PDF was never generated. Visa application rejected for missing insurance document."
    },
    # ── PAYMENT / VOUCHER ──
    {
        "title": "Voucher payment failed after OTP verification with bank deduction",
        "description": "Mutamir selected 5-star Umrah package (14,500 SAR). Payment gateway returns TRANSACTION_FAILED after successful OTP verification. Amount 14,500 SAR deducted from Alinma Bank account but not credited to service provider. Transaction REF: ALINMA-662-38492."
    },
    {
        "title": "Umrah service voucher not generated after successful payment",
        "description": "Full payment of 9,200 SAR confirmed via STC Pay (REF: STC-772194). Hotel + transport service voucher was never generated. Mutamir has receipt but no QR code to present at Makkah hotel check-in. Support ticket ESC-44928 created."
    },
    {
        "title": "Ramadan promotional discount voucher rejected as expired",
        "description": "Promotional discount code 'UMRAH_RAMADAN_25' rejected with error VOUCHER_EXPIRED despite valid until 2026-07-15. Multiple mutamir report same issue — batch RAM-2026-003 affected. Marketing team unaware of expiry flag change."
    },
    {
        "title": "Group booking only accepts full payment — no partial deposit",
        "description": "Group of 15 mutamir (12 adults + 3 children) wants to pay 50% deposit for Umrah package. System requires FULL payment upfront. Error: GROUP_DEPOSIT_NOT_SUPPORTED. Previous year allowed partial deposit. Change made without notice."
    },
    {
        "title": "Payment gateway only accepts Saudi-issued cards",
        "description": "Malaysian mutamir cannot complete payment for Umrah package. Payment gateway rejects international VISA card with error: CARD_REGION_NOT_SUPPORTED. Only Mada and Saudi-issued cards accepted. No alternative like Paypal or bank transfer."
    },
    {
        "title": "Invoice shows incorrect pricing — Gold package instead of Basic",
        "description": "Invoice INV-UMRAH-77201 generated for 8,000 SAR instead of agreed 6,500 SAR for Basic Umrah package. System incorrectly applied Gold package pricing tier. Cannot regenerate corrected invoice from portal."
    },
    {
        "title": "Payment refund stuck for cancelled Umrah booking",
        "description": "Mutamir cancelled Umrah package within 24 hours (eligible for full refund per policy). REFUND-2026-55128 created 7 days ago. Status still 'PENDING_APPROVAL'. No agent action taken. Customer support says 'escalated'."
    },
    # ── TRAVEL / LOCATION ──
    {
        "title": "Mutamir inside Saudi Arabia cannot complete Umrah registration",
        "description": "Egyptian mutamir already in Makkah (arrived on tourist visa, now wants Umrah) but registration system blocks with error: APPLICANT_MUST_BE_OUTSIDE_KSA. Geographic check based on IP/cell tower. No manual override or exception process."
    },
    {
        "title": "Outside KSA flag blocks new visa link to existing Umrah record",
        "description": "Pakistani mutamir returned home after first Umrah to renew passport. System shows flag OUTSIDE_KSA and refuses to link new visa application to existing Umrah profile. Must register from scratch with new account. Profile ID: MTR-88321."
    },
    {
        "title": "No flight availability from Jakarta to Jeddah for 10 days",
        "description": "All flights from Jakarta to Jeddah fully booked until next week. Peak Umrah season. System returns error: NO_FLIGHT_AVAILABILITY. Mutamir has valid visa (VISA-UM-6629) and insurance but cannot secure transport. No waitlist feature exists."
    },
    {
        "title": "Detailed travel itinerary not generated after package confirmation",
        "description": "Umrah package CONFIRMED (REF: PKG-8842) and paid but detailed itinerary (flight times, hotel in Makkah/Madinah, bus schedule) was never generated. Mutamir arrives at King Abdulaziz Airport with no transport or hotel details."
    },
    {
        "title": "Return flight auto-rebooked 3 days later without notification",
        "description": "System auto-changed mutamir's return flight from Jeddah to Casablanca from July 10 to July 13. No SMS or email notification sent. Mutamir discovers at check-in counter. Original booking REF: SV-8821-UMRAH."
    },
    # ── ACCOUNT / SYSTEM ──
    {
        "title": "Account locked after 3 failed password attempts — wrong keyboard layout",
        "description": "Mutamir cannot log in. Account locked after 3 attempts. Password actually correct but was typed with Arabic keyboard layout active instead of English. Error: ACCOUNT_LOCKED_24H. No unlock mechanism for 24 hours."
    },
    {
        "title": "Passport update not reflected — visa application rejected due to data mismatch",
        "description": "Mutamir updated passport number from A123456 to B789012 and expiry date. System shows old passport data in visa application. Rejected with VISA_DATA_MISMATCH. Update timestamp in profile shows correct but query returns cached stale data."
    },
    {
        "title": "International OTP SMS not delivered to Moroccan number",
        "description": "OTP for mobile verification not arriving for Moroccan mutamir (+212 6XX XXX XXX). Tried 7 times over 2 hours. International SMS routing issue with the provider. Error: OTP_SEND_FAILED. Cannot proceed without verified number."
    },
    {
        "title": "Cannot link dependents to family Umrah account",
        "description": "Father (MTR-99201) trying to add wife and 3 children to his Umrah account. System returns FAMILY_LINK_ERROR with no details. All 4 dependents have valid passports and individual visas approved. Profile linking broken."
    },
    {
        "title": "System shows 'SERVICE_UNAVAILABLE' during peak registration hours",
        "description": "Every evening between 8 PM and 11 PM KSA, the Umrah registration portal returns HTTP 503 SERVICE_UNAVAILABLE. Peak usage hours during Ramadan. Mutamir cannot complete registration during available free time after work."
    },
    {
        "title": "Email notification for visa approval not sent — mutamir missed deadline",
        "description": "Visa approved (VISA-UM-7742) but email notification to mutamir@example.com was never delivered. Mutamir missed 48-hour payment confirmation window. Visa auto-cancelled. Error log shows EMAIL_DELIVERY_FAILED — SMTP timeout."
    },
]

def classify(title, description):
    url = f"{API_BASE}/classify"
    data = json.dumps({"title": title, "description": description}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

for i, inc in enumerate(remaining):
    result = classify(inc["title"], inc["description"])
    status = "OK" if "incident_id" in result else "FAIL"
    cls = result.get("classification") or {}
    system = cls.get("affected_system", "?")
    sev = cls.get("severity", "?")
    print(f"[{i+1:02d}/{len(remaining)}] {status} | {system:20s} | {sev:10s} | {inc['title'][:55]}")
    if "error" in result:
        print(f"  ERROR: {result['error']}")
    time.sleep(0.5)

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
