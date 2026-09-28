# Cluster review 2026-09-23 — why the clusters looked wrong, and the v5 fixes

Review of the live `/api/reports/clusters` output (91 tickets, 15 active
clusters) plus the `clusters` / `cluster_members` / `assignment_log` tables and
the API logs. Four defects, all fixed in code (PROMPT_VERSION
`clustering-v4-2026-09` → `clustering-v5-2026-09`).

## D1 — the nightly audit was a no-op (audit/assign thrash) — FIXED

Symptom: clusters the audit had declared impure were back on the dashboard
within 15 minutes, every night.

Evidence (`assignment_log`, incident `3d8cc3aaceb1`, cluster
`cl_1181e7794142`):

| time | action | result |
|---|---|---|
| 09-19 03:46 | audit_remove | back in the cluster at 04:02 |
| 09-20 03:59 | audit_remove | back at 04:14 |
| 09-22 19:16 | audit_remove | back at 19:32 |
| 09-23 19:18 | audit_remove | back at 19:33 |

15 of the 74 active members (20%) were sitting in a cluster that had already
evicted them. Cause: Flow C (audit, nightly, sees ALL members) and Flow A
(sweep, every 15 min, sees a card and a per-ticket verdict) are two LLMs with
no memory of each other's verdict, and nothing recorded the disagreement.

Fix: audit verdicts are BINDING. `audit_cluster` now logs `audit_keep` for
kept members as well, `store.audit_vetoes()` reads the NEWEST verdict per
(incident, cluster), and Flow A drops vetoed candidates BEFORE the LLM call.
Flow B refuses to re-mint a group under the name of a cluster that evicted one
of its members (retired clusters included). Vetoes expire after
`AUDIT_VETO_TTL_DAYS = 30` and are cleared by a later `audit_keep`, so a wrong
verdict cannot lock a ticket out forever — it costs one retry per month
instead of one every 15 minutes.

Proof on the live system after the fix: `audit_remove` at 23:14:45 →
Flow A at 23:14:48 with candidates
`["cl_ccc6c59a602b", "cl_1aa3a09e0107", "cl_6d15c381dc4d", "cl_ba6e87b7b504"]`
— the vetoing cluster is gone from the candidate list and the ticket stays in
the pool.

## D2 — cross-system members survived the v4 invariant — FIXED

Five active members belonged to a different system than their cluster (2 Umrah
tickets in a Hajj Rawdah cluster, 1 Umrah in a Hajj evaluation cluster, 1 Hajj
in a CRM cluster, 1 `Other` in a Hajj cluster) — a violation of the v4 rule
"Hajj/Umrah never mix".

Cause: `add_cluster_member` only refuses NEW cross-system inserts. Four rows
predate that invariant (assigned 2026-08-24, before the v4 commit on 09-01)
and one was RE-CLASSIFIED into `Other` after joining. Nothing reconciled
existing rows.

Fix: `_evict_cross_system_members()` runs at the top of every sweep and evicts
members whose CURRENT system differs from the cluster's (logged as
`system_evict`, deliberately NOT an audit veto). Live result: all 5 evicted at
23:13, `mismatched = 0` for every active cluster since; the two Umrah Rawdah
tickets re-formed as their own Umrah cluster (`cl_245feb0e4bda`) through the
normal sweep — different team, different cluster, as designed.

## D3 — naming empty clusters — FIXED

60 retired rows, 34 of them named "لا توجد تذاكر في العنقود" ("there are no
tickets in the cluster"). Cause: the audit-demote path removed the last
members and then called Flow D, which asked the LLM to title an EMPTY ticket
list; the model answered with a sentence about the emptiness.

Fix: `regenerate_name` is a no-op below `_MIN_CLUSTER_MEMBERS`, the audit never
renames a demoted cluster, and `_arabic_cluster_name([])` returns "مجموعة"
without an LLM call. The 60 empty retired rows were deleted (all had zero
members; nothing references them — `cluster_proposals` is a separate table).

## D4 — the sweep could not enforce "same service" — FIXED

Clusters like "فشل في عمليات رفع أو تصدير البيانات" (an `or` in the name) merged
tickets from unrelated services: `_sweep_batch` sent the LLM only id/title/
description, while `SWEEP_PROMPT` demanded "the SAME failing action on the SAME
service surface" — the service label the classifier had already produced was
never in the payload, so the LLM could only match symptom words.

Fix: each sweep ticket now carries `service` + `offering`, and the prompt
spells out that different `service` values are different problems (no "X or Y"
group titles). Existing clusters are re-judged by the nightly audit.

## Audit prompt precision (false positives)

`74af9be0eda3` and `bdfbe03ae8c1` ("عطل في تشغيل CRM" / description "عطل في
تشغيل النظام") had been removed from the CRM-outage cluster for saying "the
system" instead of "CRM" — near-identical tickets (97.9% / 97.6% similarity).
`AUDIT_PROMPT` now instructs the audit to judge the ticket as a WHOLE
(title + description + service) and not to remove a ticket merely because its
description is generic. Verified live: the same cluster now audits to
`removed: []`.

## Verification

* `pytest tests` — 267 passed, 6 xfailed, 1 xpassed; 11 new regression tests in
  `tests/services/cluster/test_persistent_clustering.py`
  (`TestAuditVerdictsAreBinding`, `TestSweepDoesNotReMintRejectedClusters`,
  `TestSystemReconciliation`, `TestNoNamingOfEmptyClusters`).
* Live after the fix: 18 active clusters, **0 cross-system members**, no
  empty-cluster names, no empty rows (75 rows → 18, all active).
* Live audit pass (the system's own nightly check, run manually once): 6
  members removed with reasons; the 23:28 sweep did NOT put them back — each
  one now carries `none_fit` instead of the old `assign`. Before the fix the
  same members were re-assigned 15-16 minutes after every eviction.

### Steady state worth knowing

* The 19-member Rawdah cluster became 15 members after the audit split off two
  "no slots available" tickets; the sweep then grouped those two into their own
  cluster (`عدم ظهور أو حجز تصاريح الروضة`). That is the intended behaviour —
  the audit separates two problems, the sweep clusters the separated ones — not
  the old thrash (which re-added them to the SAME cluster).
* The veto is per (incident, cluster), so an evicted ticket may still join a
  DIFFERENT cluster: `b844c129d2a5` (evicted from the contracts/arrival cluster)
  was assigned by the next sweep to the transport-approval cluster instead.
  A global per-ticket block was rejected as too blunt — a misfiled ticket
  usually does belong somewhere else.
* Still imperfect (LLM judgement, not a code defect): the audit kept
  `cl_5f62c942c8c6` ("فشل في عمليات رفع أو تصدير البيانات", an "or" title
  spanning reports-export and evaluation-upload). The sweep prompt now forbids
  such titles for NEW mints; an existing one needs an audit that splits it.
