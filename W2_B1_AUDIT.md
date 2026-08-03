# W2_B1_AUDIT.md — B1 claim correction (manager review 2026-08-02)

## Claim as committed (e681afb): "zero wrong merges (146 YES edges audited)"
## Corrected claim: 146 YES edges audited → **144 correct attaches, 2 wrong attaches**.
## B1 as measured is NOT "zero wrong merges".

The wrong attaches are both in the pilgrim-groups proposal (`b6189b1544cd`, natural
run — 20 members, mean_sim 0.5794, FM-018×18 + FM-012×1 + FM-009×1). Both are
cross-FM members whose verifier reasons FABRICATE Rawdah content that their ticket
texts do not contain.

---

## Re-audit of the two cross-FM members (edge → reason → ticket texts → judgment)

### 1) cad886ca1505 (FM-012) — WRONG ATTACH (definitive)

| | |
|---|---|
| Edge | `cad886~ecbbb1` (sim 0.4343 — the ONLY edge connecting cad886; max sim to any pool member is 0.55, so no auto-accept) |
| Verifier reason | "Both describe failure to issue Rawdah permits on the Masar/Nusuk platform." |
| cad886 text | TITLE "فضلا اصدار الاعتماد" / DESC "تم دخول وقت صدور الاعتماد فضلا اصداره 135230" — "approval time has arrived, please issue it 135230". An EXPEDITE request. No Rawdah, no failure, no portal named. |
| ecbbb1 text | "Rawdah Permits Error" (FM-018) — the Rawdah content in the reason is ecbbb1's, NOT cad886's. |
| Judgment | **WRONG ATTACH.** Prompt v3 requires SAME FAILING ACTION: "request/expedite an approval" ≠ "booking/issuing Rawdah permit fails with technical error". The reason over-claims. This is the exact instability class the canary pinned: cad886 appears in known-flaky pairs 14 (a6b2df~cad886), 24 (13c6f0~cad886), 32 (cad886~fe732b) — the transport-approval family — and the engine merged it into the Rawdah cluster. |

### 2) 390a79005b17 (FM-009) — WRONG ATTACH (reason fabricated; surface unproven)

| | |
|---|---|
| Edges | `390a79~ecbbb1`, `390a79~1049c8`, `390a79~372f52`, `390a79~78aebd` (all sim 0.55–0.62, all LLM-verified) |
| Verifier reasons | All of the form "Both describe failure to issue Rawdah permits on the same portal." |
| 390a79 text | TITLE "ملاحظة مرفقة لكم" / DESC "الحاقا للشكوى السابقة رقم 11405845 على طلب استخراج تصريح حجاج الخارج . نفيدكم لازالت الملاحظة مستمرة والوقت يداهمنا فلذا نرجوا حل هذه الاشكالية عاجلا . ليتيح لنا استخراج التصريح." — a follow-up on a complaint about extracting an EXTERNAL-PILGRIM permit (تصريح حجاج الخارج). Never mentions Rawdah/الروضة. |
| Judgment | **WRONG ATTACH (strict standard).** Same offering (Issue Permits) and same class (permit issuance blocked), which is why the candidate sims are 0.55–0.62 — but prompt v3 demands the same SERVICE SURFACE, and the text names no portal; the permit type (external-pilgrim entry permit) is plausibly distinct from Rawdah visit permits. The Rawdah-specific reasons are fabricated relative to the text. Human gate should review merge-worthiness (same-offering class) rather than auto-accept. |

### All other 144 edges — re-verified correct
Every remaining edge in every proposal names the same failing action + same
service, and the member texts match (Rawdah↔Rawdah, evaluation-icon↔evaluation-
icon, appeal↔appeal, arrival-confirm↔arrival-confirm, tax-form↔tax-form,
approval-pending↔approval-pending, report-status↔report-status, CRM↔CRM).

---

## Purity floor assessment — did it catch this? NO, and why that is a gap

Proposal flags: `{"mean_sim": 0.5794, "n_fm_codes": 3, "needs_review": False}`.
The floor (mean_sim < 0.45 OR >6 FM codes → NEEDS_REVIEW) is a **cluster-level**
filter designed to catch heterogeneous grab-bags. A single minority-FM member
(1 of 20) in a majority-FM cluster does not move n_fm_codes past 6 — the floor
cannot catch minority-member contamination by construction. This is a **real
floor gap** for the "minority member" failure mode. Candidate fix (design change,
NOT applied here): member-level rule — a member whose FM code is not in the
cluster's top-2 codes → NEEDS_REVIEW (or exclude). Flagged for the manager.

## What the human gate will do with these (rejection procedure)

The proposal queue is human-decided by design (STATUS.md: never auto-approve).
For `b6189b1544cd` (or its shuffle equivalent `3a9874589f87`, which still carries
390a79): the reviewer must NOT approve as-is. Options, in order of preference:
1. **Approve with exclusion** — not yet supported by the API (member-level
   approval is an open item; today it is approve-everything or reject-all).
2. **Reject** → members return to the pool with 24h cooldown (the API's reject
   path). cad886/390a79 re-enter the pool and can re-cluster; the verifier cache
   keeps the same YES, so repeated clustering would re-propose them — the
   cooldown is the short-term breaker; a verifier-cache negative override
   (forced NO for a known-bad pair) is the proper long-term fix (open item).
3. **Merge** into a transport-approval / permit-class sub-offering if one exists.

For 390a79 specifically, the human may reasonably decide same-offering permit
class is acceptable (it IS an Issue Permits ticket) — the point is the decision
is HUMAN, not auto-minted.

## API gate surfacing — CONFIRMED GAP (this is what the gate is for, and it is incomplete)

Pasted GET /proposals response (live DB, proposal `3a9874589f87`):
```json
{
 "id": "3a9874589f87",
 "offering_id": "pilgrim groups and issue permit - Nusuk Masar Haj",
 "member_ids": ["27da49c32b22", "4061fd68da1a", "390a79005b17", ...],
 "mean_sim": 0.6037,
 "verifier_reasons": {"1049c8~372f52": "Both describe errors while booking or issuing Rawdah permits...", ...},
 "purity_flags": {"mean_sim": 0.6037, "n_fm_codes": 3, "needs_review": false},
 "proposed_label": "Rawdah Permit Selection Issue",
 "status": "pending"
}
```
**What a reviewer CAN see:** verifier_reasons (yes — every edge's reason is
surfaced), mean_sim, purity_flags, label, offering.
**What a reviewer CANNOT see:** member TITLES/DESCRIPTIONS. Member `390a79005b17`
is an opaque id; nothing in this response reveals its text is about external-
pilgrim permit extraction. **The gate as-built cannot catch the cad886/390a79
class of wrong attach without a second query (GET /incidents).** Recommended fix
(open item): include `member_titles` (and optionally truncated descriptions) in
the proposal response. This is the single highest-value gate improvement.

## Numbers that stand after correction
- 8 proposals, 146 YES edges, 144 correct + 2 wrong attaches (this proposal).
- cad886: no auto-accept (max sim 0.55) — joined on ONE fabricated-reason edge.
- The same instability class the canary pinned (transport-approval family,
  cad886 in known-flaky 14/24/32) is exactly what leaked into the cluster.
- B3 order-robustness (5/8) is unaffected by this correction.
- B2 coverage (53/92) is unaffected; if cad886+390a79 were excluded, it is 51/92.
