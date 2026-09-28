# Full result review 2026-09-24 — every ticket, every cluster

Second pass of the cluster review (see `cluster-v5-audit-thrash.md` for the
first). Method: the whole live corpus (91 tickets, 18 clusters, the unassigned
pool) was exported and reviewed by independent agents — one reviewer per
ticket chunk, one per cluster, plus whole-set consistency passes — and every
finding was verified against the ticket text before anything was changed.
Four review rounds: 91 classifications → cluster purity → whole-set
duplicates → re-verification of the repaired state.

## What the review found and what was fixed

### 1. Canonical statements were being corrupted (data loss)
`services/classify/parsing._normalize_canonical` cut the statement at the
first colon and deleted every 6+ digit run, every date-like token and the
words "number"/"entry"/"percent":

| stored (before) | should be |
|---|---|
| `Inter-city transport requests (IDs , ) are stuck…` | `…requests 135217 and 135218 are stuck…` |
| `Recurring technical error (Error ) occurs…` | `…(Error number 10036015) occurs…` |
| `00 with only two hours remaining.` | `Bus departs at 14:00 with only two hours remaining.` |
| `…on /2026` | `…on 4/6/2026` |

18 of 91 statements were affected. The canonical is shown to operators AND fed
to the clustering prompts, and it is not embedded anywhere (the embedding is
title+description), so the "embedding stability" rationale was stale. Now only
a leading `System[/service]:` label is stripped and whitespace is normalized;
ids, error codes, dates and counts survive.

### 2. A near-miss service string discarded the whole classification
The model writes the system suffix of a service from memory — `licensing -
Nusuk Hajj`, `contracts - Nusuk Hajj Services`, even the concatenation
`licensing - Nusuk Hajj services - Nusuk Masar Haj` where the taxonomy has
`licensing - Nusuk Masar Haj`. The strict validator rejected the string, the
stage retried, and on a second miss the ENTIRE classification was dropped: the
ticket landed on `Other / General / Unspecified` with
`classification_status="failed"`. 41 of 1505 logged stage-2/3 responses (2.7%)
were such near-misses — each one a lost ticket.

Fixes: `snap_service()` maps an off-taxonomy string onto the real option by
comparing the service CORE (system-name variants removed, punctuation/case
normalized; ambiguous cores are skipped), `_parse_stage_service()` keeps the
already-resolved SYSTEM and stores the ticket on that system's generic service
when nothing matches, and `_cascade_fallback()` no longer replaces a known
system with "Other".

### 3. Haj / Umrah misroutes (two teams, one dashboard)
Four tickets were classified as `Nusuk Masar Umrah` on no textual evidence
(company-evaluation tickets named "منصة مسار", Rawdah permits for الحجاج, a
Hajj contract). The LLM stage-1 guess flip-flopped between the two systems on
tickets that are word-for-word the same.

Fixes: an evidence rule in the system prompt (Umrah only with an Umrah marker;
unmarked Nusuk tickets default to Haj), a few-shot example for group Rawdah
permits, and a deterministic resolver rule — Rawdah + (permit|appointment|
group|حجاج) without an Umrah marker resolves to `Nusuk Masar Haj` before the
LLM is ever asked.

### 4. Wrong `ticket_kind` hid real problems from the dashboard
Only `incident`/`service_request` tickets may join a cluster, so three
mislabelled tickets sat unassigned although their clusters existed:
"the platform never applied the closure I did" (administrative → incident),
"there is no reply field on the report" (administrative → incident), "the
platform rejects a valid commercial-registration number" (inquiry → incident).
Triage gained counter-examples for exactly these shapes; all three are now
incidents and two of them are in their cluster.

### 5. The audit could not see the service (mixed clusters survived)
`audit_cluster` sent the LLM only id/title/description while the acceptance
criterion demands "one problem, one service surface" — so a cluster named
"failure in upload **or** export operations" (an evaluation-form upload + a
report export) survived every nightly audit. The audit payload now carries
each member's service and the prompt states that different services are
different problems.

### 6. …but a generic service label is not evidence (the follow-up)
Adding the service rule made the audit evict five legitimate Rawdah permits
whose service was `Istiqbal - ….OFFERING-GAP` (the classifier ABSTAINED — no
offering matched). Rules now spell out that `System/Application - …`,
`….OFFERING-GAP` and `General / Unspecified` carry no surface information: the
audit must judge those tickets by text alone. The five were reinstated.

### 7. Human decisions now survive the nightly audit
An operator override used to be undone by the next audit run (observed within
hours). Members added with `assigned_by="human"` are PINNED: the audit logs
`audit_remove_overruled` and keeps them, and a demotion never returns a pinned
member to the pool. `add_cluster_member` re-stamps an existing row when the
assignment is human, so "pin this ticket here" is explicit.

### 8. The sweep could still merge two services on a shared symptom word
Prod got a "Between cities · Register Arrival" ticket grouped with a
"Registration · application form" one ("cannot enter pilgrim data"). The mint
path now refuses a group spanning two different taxonomy services
(`_inconsistent_services`) — a deterministic backstop behind the prompt.

## Live repair applied

* 46 tickets re-classified (the flagged ones), reviewed before writing: 7
  system corrections, 6 kind corrections, 17 service refinements, 45 canonical
  fixes; one change (a feature request) deliberately not applied.
* Cluster curation with logged reasons (`human-review` in `assignment_log`):
  2 error-based tickets moved into the Rawdah permit cluster, 3 pool tickets
  attached where the audit had wrongly evicted them, 7 members released from
  mixed clusters (each reasoned), 1 symptom bucket dissolved, 2 clusters
  renamed so two different Rawdah problems read differently.
* 5 leftover empty rows deleted (artifacts of the old empty-cluster naming bug).

## Result

91 tickets → 18 active clusters covering 72 tickets, 19 unassigned, **0
cross-system members**, no duplicate clusters, no empty/junk rows. The 19
unassigned are 11 incidents that genuinely have no partner (verified in the
pool review) plus 8 non-problem tickets (inquiry/administrative/feature_
request/content_thin) that the product keeps out of clusters by design.

## Still imperfect (known, not defects)

* Cluster granularity is an LLM judgement: "no Rawdah slots available" vs
  "Rawdah permit issuance errors" are separated; a stricter reading would
  merge or split a few 2-member clusters differently on any given night. The
  audit's decisions are now logged per member, so any change is traceable.
* Ticket descriptions arrive from SMAX truncated at exactly 200 characters —
  classification (and clustering) only ever sees that much of the ticket.
* Two pool tickets are pair-shaped (the two "close this report" requests, the
  two tax-invoicing notices) but their kinds are administrative/inquiry, so the
  product rule keeps them out of clusters.

## Tests

`pytest tests` — full suite green. New this pass:
* `tests/services/classify/test_canonical_normalization.py` (11) — ids, error
  codes, dates and colons survive; only real labels are stripped.
* `tests/services/classify/test_service_snap.py` (12) — every near-miss seen
  in prod snaps to the real option, an invented service still fails, and a
  resolved system is never replaced by "Other".
* `tests/services/cluster/test_persistent_clustering.py` — audit-veto binding,
  cross-system reconciliation, no naming of empty clusters, human pins (3) and
  the service-consistency guard (4).
* `tests/services/classify/test_prompt_identity.py` — frozen prompt SHA
  recomputed for classifier v3.5.
