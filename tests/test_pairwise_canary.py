"""PAIRWISE CANARY — regression canary for the frozen A3 pairwise verifier (prompt v3).

LIVE-MODEL test: re-verify the 34 frozen dev-slice pairs (22 wrong + 12 correct) in
BATCHES OF 8 per LLM call — the SAME batching the frozen production params and the
original 22/22 + 12/12 dev-slice measurement used. The v3 prompt's
'SAME TASK, DIFFERENT GRANULARITY' clause operates with batch context; individual
calls lose it and the model over-tightens recall (manager-verified: 2 correct pairs
flipped to NO under individual calls).

- Source fixture: tests/pairwise_canary_fixture.json (batch_size=8 metadata).
- Prompt: STRICT_PROMPT_V3 below — frozen verbatim from the A3 run (a3_prompts.py).
- Model guard: the test FAILS if settings.llm_model is not OpenRouter qwen3.6
  (the load_dotenv-is-CWD-relative Ollama-fallback bug guard). Export
  LLM_MODEL=openrouter/qwen/qwen3.6-35b-a3b + LLM_API_KEY from the repo .env
  before running.
- CI safety: PAIRWISE_CANARY_SKIP=1 -> whole module skips (no model calls).
- Missing/unparseable item in a batch -> one individual retry (production
  semantics); still unparseable -> that pair FAILS loudly (never silently passes).
"""
import json
import os
import re
from typing import Any

import pytest
from litellm import completion

from ai_classification.config import settings

FIXTURE = os.path.join(os.path.dirname(__file__), "pairwise_canary_fixture.json")
REQUIRED_MODEL_SUBSTR = ("openrouter", "qwen3.6")
BATCH_SIZE = 8
BATCH_MAX_TOKENS = 4000  # production batch budget; verbose reasons truncate at 800

STRICT_PROMPT_V3 = """You verify whether two incident tickets describe the SAME underlying problem.

A YES requires BOTH conditions:
(1) SAME FAILING ACTION — the exact operation the user tried to perform and it failed (e.g. 'issuing a Rawdah permit', 'submitting an appeal against a violation', 'confirming hotel arrival', 'uploading an evaluation form'). Different actions = NO, even if both are 'technical errors' or 'form submissions'.
(2) SAME SERVICE / SYSTEM SURFACE — the same page, portal, or module where the failure occurs (e.g. 'Rawdah permit portal' vs 'reports/complaints page' vs 'tax data form portal' are different surfaces).

Category-level overlap is INSUFFICIENT. 'Both are technical failures', 'both involve submitting something', 'both show an error message' — NOT enough.

SAME TASK, DIFFERENT GRANULARITY: different steps or variants of the SAME task on the SAME page/entity count as the same failing action — e.g. 'cannot view reports' vs 'cannot reply to a report' vs 'cannot close a report' are all the reports page being broken. Different pages/portals/services = NO.

Positive example (same service, related actions — YES):
A: 'البلاغ رقم 2026-2073410، تم اغلاق البلاغ ... لم يُحدّث في المنصة' (a report was closed but not updated on the platform) | B: 'هناك مشكلة تقنية في صفحة البلاغات ولا نستطيع الرد ولا اقفل اي بلاغ' (technical problem in the reports page, cannot reply or close reports). YES — same service (reports/complaints page), same task family (report lifecycle actions failing on that page).

Negative examples (both tickets are technical errors, but DIFFERENT problems — NO):
1. A: 'عطل في تشغيل نظام CRM' (CRM system down) | B: 'خطأ تقني في إجراء (تأكيد الوصول الفعلي) لفندق متعاقد عليه' (error confirming hotel arrival). Tempting: 'both technical failures'. Correct: NO — failing actions differ (system outage vs arrival confirmation), surfaces differ (CRM vs hotel contract portal).
2. A: 'عدم القدرة على تسجيل نموذج البيانات الضريبي' (cannot register tax data form) | B: 'عملية رفع نموذج التقييم تفشل' (evaluation form upload fails). Tempting: 'both form submission failures'. Correct: NO — different forms, different portals.
3. A: 'ERROR IN ISSUING RAWDAH PERMITS' (Rawdah permit issuance error) | B: 'مشكلة تقنية في صفحة البلاغات' (reports page technical problem). Tempting: 'both technical errors preventing processing'. Correct: NO — Rawdah permit portal vs reports/complaints page.

Answer format — for each pair, ONE object:
{"pair": n, "decision": "YES"|"NO", "reason": "<one short sentence, max 15 words — YES: name the shared failing action and service; NO: name the differing actions/services>"}
Return ONLY a JSON array with ALL pairs answered. Strict binary; when in doubt, NO."""


def _load_fixture():
    with open(FIXTURE) as fh:
        return json.load(fh)


def _batch_prompt(pairs):
    lines = []
    for n, p in enumerate(pairs, 1):
        lines.append(
            f"Pair {n}:\n[A] {p['title_a']}\n    {p['description_a']}\n"
            f"[B] {p['title_b']}\n    {p['description_b']}"
        )
    return f"Verify these {len(pairs)} pairs (FULL ticket text):\n\n" + "\n\n".join(lines)


def _call_batch(pairs):
    """One batched LLM call. Returns {1-indexed-position: decision}."""
    kwargs: dict[str, Any] = dict(
        model=settings.llm_model,
        temperature=0.0,
        seed=42,
        max_tokens=BATCH_MAX_TOKENS,
        messages=[
            {"role": "system", "content": STRICT_PROMPT_V3},
            {"role": "user", "content": _batch_prompt(pairs)},
        ],
    )
    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base
    if settings.llm_api_key:
        kwargs["api_key"] = settings.llm_api_key
    if "qwen3" in settings.llm_model.lower():
        kwargs["extra_body"] = {"reasoning": {"enabled": False}}
    resp = completion(**kwargs)
    return _parse_batch(resp.choices[0].message.content)


def _parse_batch(content):
    """Robust parse: fences, preamble, truncation recovery, single-object wrap,
    and concatenated objects (`{...}{...}`)."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    i = text.find("[")
    if i == -1:
        i = text.find("{")
    if i > 0:
        text = text[i:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        i, j = text.find("["), text.rfind("]")
        if i == -1 or j <= i:
            i, j = text.find("{"), text.rfind("}")
        if i == -1 or j <= i:
            raise
        candidate = text[i:j + 1]
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            # concatenated objects, newline-separated: {...}\n{...} -> [..., ...]
            data = json.loads("[" + re.sub(r"\}\s*\{", "},{", candidate) + "]")
    if isinstance(data, dict) and "pairs" in data:
        data = data["pairs"]
    if isinstance(data, dict):
        data = [data]
    out = {}
    for item in data:
        if isinstance(item, dict) and item.get("decision") in ("YES", "NO"):
            out[int(item["pair"])] = (item["decision"], str(item.get("reason", ""))[:120])
    return out


def _verify_one(pair, max_tokens=300):
    """Individual retry (production fallback for a missing batch item)."""
    kwargs: dict[str, Any] = dict(
        model=settings.llm_model,
        temperature=0.0,
        seed=42,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": STRICT_PROMPT_V3},
            {"role": "user", "content":
                f"Verify this ONE pair (FULL ticket text):\n\nPair 1:\n"
                f"[A] {pair['title_a']}\n    {pair['description_a']}\n"
                f"[B] {pair['title_b']}\n    {pair['description_b']}"},
        ],
    )
    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base
    if settings.llm_api_key:
        kwargs["api_key"] = settings.llm_api_key
    if "qwen3" in settings.llm_model.lower():
        kwargs["extra_body"] = {"reasoning": {"enabled": False}}
    resp = completion(**kwargs)
    parsed = _parse_batch(resp.choices[0].message.content)
    return parsed.get(1)  # single-pair response -> position 1


# ── Known-flaky pairs (documented, NOT skipped — assertions stay strict) ──
# All five are the SAME-SERVICE/DIFFERENT-ACTION granularity class: frozen dev-slice
# (prompt v3, batched) said YES; repeated live runs (batched, same params) say NO.
# The model's action-granularity judgment on these is unstable across runs.
# FLAGGED for manager decision — do NOT loosen the assertions silently.
#   pair[1]  13c6f0~fda002  transport approvals      (approval delay vs issuance failure)
#   pair[3]  1096a3~fa70c2  reports page             (status update vs view)
#   pair[9]  1096a3~f70184  reports page             (status sync vs missing reply field)
#   pair[19] 0433ea~33c04b  reports page             (page broken vs close specific report)
#   pair[24] 13c6f0~cad886  transport approvals      (approval vs permit issuance)
KNOWN_FLAKY_IDX = {1, 3, 9, 19, 24}


# ── module-level skip gate ────────────────────────────────────────────
pytestmark = []
if os.environ.get("PAIRWISE_CANARY_SKIP") == "1":
    pytestmark = [pytest.mark.skip(reason="PAIRWISE_CANARY_SKIP=1 — canary disabled for CI")]


def test_model_guard():
    """Fail if the model is not OpenRouter qwen3.6 (Ollama-fallback guard)."""
    model = settings.llm_model
    assert all(s in model.lower() for s in REQUIRED_MODEL_SUBSTR), (
        f"Ollama-fallback guard: settings.llm_model={model!r} — export "
        f"LLM_MODEL=openrouter/qwen/qwen3.6-35b-a3b + LLM_API_KEY from the repo .env "
        f"(load_dotenv is CWD-relative; silent ollama fallback is a known bug)"
    )


def test_fixture_shape():
    fx = _load_fixture()
    assert fx["prompt_version"] == "v3"
    assert fx["model"] == "openrouter/qwen/qwen3.6-35b-a3b"
    assert fx["batch_size"] == BATCH_SIZE
    assert fx["n_pairs"] == 34
    assert fx["n_wrong"] == 22 and fx["n_correct"] == 12
    assert len(fx["pairs"]) == 34


def test_pair_verdicts_batched():
    """Live-model re-verification in batches of 8 (production batching).

    wrong pairs must be NO, correct pairs must be YES. All failures reported in
    one assertion message (idx, verdict, reason)."""
    fx = _load_fixture()
    pairs = fx["pairs"]
    decisions: dict[int, str] = {}
    reasons: dict[int, str] = {}

    for start in range(0, len(pairs), BATCH_SIZE):
        chunk = pairs[start:start + BATCH_SIZE]
        by_pos = _call_batch(chunk)
        for pos, p in enumerate(chunk, 1):
            idx = start + pos - 1
            if pos in by_pos:
                decisions[idx], reasons[idx] = by_pos[pos]
            else:
                # production fallback: one individual retry; then fail loudly
                retry = _verify_one(p)
                if retry is None:
                    decisions[idx], reasons[idx] = "UNPARSEABLE", "individual retry unparseable"
                else:
                    decisions[idx], reasons[idx] = retry

    mismatches = []
    for idx in range(len(pairs)):
        p = pairs[idx]
        got = decisions.get(idx)
        if got != p["expected"]:
            mismatches.append(
                f"pair[{idx}] {p['id_a'][:6]}~{p['id_b'][:6]} [{p['judgment']}]: "
                f"expected {p['expected']}, got {got} — LLM: {reasons.get(idx, '')}"
            )

    passed = len(pairs) - len(mismatches)
    print(f"\nCANARY: {passed}/{len(pairs)} pass (batch_size={BATCH_SIZE}, "
          f"model={settings.llm_model})")
    for m in mismatches:
        print(f"  FAIL {m}")
    assert not mismatches, f"{len(mismatches)}/{len(pairs)} pairs failed:\n" + "\n".join(mismatches)
