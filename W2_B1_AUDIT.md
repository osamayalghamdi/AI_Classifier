# W2_B1_AUDIT — honest correction of the B1 claim

Date: 2026-08-02 (manager review pass). Author: W2.
Status: AUDIT — the B1 "zero wrong merges" claim in commit `e681afb` was WRONG.
This doc corrects it. No pipeline re-run was used for the finding (ticket texts
were read directly from the DB); the member-level purity rule WAS then verified
by a cache-backed re-run of proposal construction.

## The discrepancy (manager's counterexample — CONFIRMED)

Proposal `b6189b1544cd` (pilgrim groups, 20 members) contained:

| member | FM | ticket text (verbatim) | verifier reason on its edges | judgment |
|---|---|---|---|---|
| cad886ca | FM-012 | "فضلا اصدار الاعتماد / تم دخول وقت صدور الاعتماد فضلا اصداره 135230" — *please issue the approval #135230; the approval time has arrived* | "Both describe failure to issue Rawdah permits on the Masar/Nusuk platform." | **WRONG ATTACH** — transport-approval request; the text says NOTHING about Rawdah (الروضة). The verifier generalized "issue permit/approval" to "Rawdah permits" without the ticket supporting the surface. |
| 390a7900 | FM-009 | "…على طلب استخراج تصريح حجاج الخارج… ليتيح لنا استخراج التصريح" — *regarding the request to extract a permit for external pilgrims (حجاج الخارج)* | "Both describe failing to book/issue Rawdah permits on the same portal." (edges to 1049c8, 372f52, 78aebd, ecbbb1) | **WRONG ATTACH** — external-pilgrim permit extraction; no Rawdah mention. Same family (permit issuance) but the claimed surface ("Rawdah portal") is not supported by the text. |

Edge count: 5 YES edges involving these two members (cad886: 1, 390a79: 4), all
claiming a surface the ticket text does not support.

## Corrected B1 numbers

- 146 YES edges in proposals (natural run); **5 wrong (3.4%)**, 141 defensible.
- **2 wrong attaches across 8 proposals** (25% of proposals carried a wrong member).
- The other 6 proposals audited clean (tax, transport, CRM, evaluation, hotel,
  reports, appeal — all member texts match their edges' claimed surface).
- The suggestion proposal (FM-022×4 + FM-021×1) is DEFENSIBLE (appeal family,
  defended in the A3 audit) — cross-FM but same problem; not a wrong attach.

## Why the gate missed it (purity floor gap)

- Cluster FM mix: FM-018×18, FM-012×1, FM-009×1 → 3 distinct codes < 6 →
  cluster-level floor did NOT trip.
- mean_sim 0.58 > 0.45 → cohesion floor did NOT trip.
- The gap: the floor counts DISTINCT codes, so a 1-of-20 minority member never
  trips it. cad886 was exactly the canary's most-flaky ticket (known-flaky pairs
  14/24/32, all transport-approval family) — the engine merged the instability
  class the canary had pinned.

## Fix (manager decision, implemented)

1. **Member-level purity rule** (suboffering_cluster.py): member FM ∉ cluster
   top-2 FM codes → member `needs_review` flag in `purity_flags.members`.
   Deterministic (count DESC, code ASC); when the 2nd/3rd counts tie, both tied
   codes are minority (conservative — catches the cad886 1-of-20 class).
   Proposal itself excluded only when flagged ≥ 1/3 of members OR the cluster
   floor trips (2/20 < 1/3 → proposal stays, members flagged — per spec).
2. **Member texts in proposal payload** (store.py): list_proposals/get_proposal
   now return `members: [{id, title, description, failure_mode}]` so the reviewer
   sees cad886's real text next to the verifier's Rawdah claim — the gate that
   makes the queue honest.

## Verification (real output)

- Unit tests (21 pass incl. `test_cad886_class_flagged`: FM-018×18 + FM-012 +
  FM-009 → both minorities flagged) and API member-texts test.
- Cache-backed re-run of proposal construction, proposal `39d968d9013b`
  (pilgrim groups, 20 members): `purity_flags.members` shows
  `cad886 [FM-012] needs_review=True` and `390a79 [FM-009] needs_review=True`;
  cluster `needs_review=False` (2/20 < 1/3 — proposal NOT excluded, per spec).
- Full suite: 91 passed + 15 canary-skipped.

## What the human gate does with these

Reviewer sees (proposal payload): member title "فضلا اصدار الاعتماد" + description
"…135230" + FM-012, next to verifier reason "Both describe failure to issue Rawdah
permits…" — the mismatch is immediately visible. Reviewer actions: reject the
proposal (members stay in pool with 24h cooldown), or merge to a transport-related
sub-offering if one exists. The member-level flag now also surfaces in the payload
so the mismatch is highlighted before the reviewer reads the text.

## Residual

- 390a79 escaped the literal "top-2" rule only under the tie-break; the
  conservative tie handling flags it (2nd/3rd counts tie at 1 → both minority).
  Any cluster with a single dominant code + 2 singleton minorities now flags
  BOTH — documented behavior, matches the cad886-class intent.
- Verifier over-generalization on "issue permit/approval" action across different
  surfaces (transport vs Rawdah vs external-pilgrim) remains an open prompt-v3
  limitation — the member-level rule + human gate are the mitigation, not a
  prompt fix (per the canary finding: never patch the prompt to chase recall).
