# Labeling checklist — ground-truth eval set (30–50 tickets)

Goal: a hand-labeled sample whose accuracy number a director can't poke holes in.
The 5-row fixture proved the plumbing; this set produces the real number.

## 1. Picking tickets (bias lives here)

- [ ] **Random, not convenient.** Take a numbered list of real tickets and use a
      random draw (or every Nth in arrival order). Do NOT hand-pick "good"
      examples, sort by severity and grab the top, or take the first page.
- [ ] **Stratify the draw.** Aim for rough coverage: both languages (Arabic +
      English — the classifier is bilingual), the systems that actually appear
      (Nusuk Masar Haj will dominate; keep a few Umrah/OldSM/Other if present),
      all severities (Critical/Major/Minor/Cosmetic), and both intake paths
      (live /classify + synced) if both are in production.
- [ ] **Span time.** Draw across ≥2 weeks of the stream, never one incident
      window. 30 tickets from a single outage is 30 copies of one decision.
- [ ] **Don't pre-filter "classifiable" tickets.** The vague ones are exactly
      where the classifier's judgment matters. If a ticket can't be mapped,
      label it honestly (see §3) — don't silently drop it.
- [ ] **Duplicates policy — decide and state it.** Either (a) dedupe to unique
      incidents before drawing (scores unique incidents, no single decision
      overweighted), or (b) keep the stream (includes dedupe/occurrence
      behavior). Pick one; put it in the eval notes. Recommend (a) for the
      accuracy number.

## 2. Labeling protocol (anchoring lives here)

- [ ] **Label blind.** Read the raw ticket text only. Never look at the
      classifier's output for a ticket before labeling it — comparing anchors
      you to its guesses.
- [ ] **Use the taxonomy's exact strings.** Copy service names from
      SERVICES_BY_SYSTEM (e.g. `7.1 Invoicing and Billing - Nusuk Masar Haj`).
      Do NOT paraphrase or re-type from memory — the harness matches strings
      exactly; one typo is a false miss.
- [ ] **Granularity = what the ticket supports.** If the ticket names the
      action ("can't pay the bill") → truth includes the offering:
      `7.1 Invoicing and Billing - Nusuk Masar Haj.Bill Payment`. If it's
      vague ("portal broken") → service level only, no offering. The harness
      excludes offering from the denominator when truth has none — don't
      invent offerings.
- [ ] **`true_*` vs `user_selected_*` are different columns.** `user_selected` =
      a fast best-guess (what an operator would pick); `true_*` = the
      adjudicated, second-pass answer. The head-to-head delta is the point of
      having both.
- [ ] **Second pass on hesitation.** Anything the first pass flagged as
      uncertain gets a second look (or a second person). One labeler's quirks
      become systematic error; adjudicate disagreements.
- [ ] **Randomize labeling order.** Don't label all Critical tickets first —
      decision fatigue and rubric drift should spread evenly.

## 3. Ambiguity policy

- [ ] **"Other" is an honest answer.** If no system is identifiable from the
      text, `true_system=Other` is correct — but flag the row for review.
- [ ] **Missing info ≠ wrong answer.** A ticket with no system hint that the
      classifier maps to Other is *correct* if you'd map it to Other too.
      Disagreeing with the classifier is not the same as the classifier being
      wrong — the truth column is what you'd defend to a colleague.
- [ ] **Keep a comment column** (free text, harness ignores extra columns) for
      "why": `no system mentioned`, `AR terminology`, `ambiguous between X and Y`.

## 4. Reporting (the number)

- [ ] **Report n and the interval.** At n=50, a point estimate has ±~14%
      (95% CI) — say "80% (n=50, ±14%)", never a bare percentage.
- [ ] **Per-system breakdown**, not just overall — the harness already groups
      by true_system.
- [ ] **System / service / offering separately.** Coarse bar ≠ strict bar.
- [ ] **The 5-row fixture number stays in the smoke-test drawer.** It proved
      plumbing; it is not an accuracy claim.
- [ ] **Version the CSV in the repo** (`evaluation/ground_truth.csv`) so any
      rerun is byte-comparable across classifier versions.

## 5. CSV format (exact headers, harness-required)

```
ticket_id,title,description,user_selected_system,user_selected_service,true_system,true_service
```

- Lines starting with `#` are comments (allowed).
- Empty `true_*` rows are skipped with a warning — don't ship them.
- Extra columns (e.g. `notes`) are ignored — safe to add.

## Run

```
PYTHONPATH=. uv run python evaluation/run_eval.py --csv evaluation/ground_truth.csv
```
Run from `feat/cascade-classifier` (the merged, coherent chain).
