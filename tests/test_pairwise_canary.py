"""PAIRWISE CANARY — regression canary for the frozen A3 pairwise verifier (prompt v3).

LIVE-MODEL test: for each of the 34 frozen dev-slice pairs (22 wrong + 12 correct),
re-verify on the live LLM and assert the expected verdict (wrong -> NO, correct -> YES).

- Source fixture: tests/pairwise_canary_fixture.json (built from
  /tmp/pairwise_experiment/cache/dev_qwen_rev3.json + the A3 audit; 34/34 agreement).
- Prompt: STRICT_PROMPT_V3 below — frozen verbatim from the A3 run (a3_prompts.py).
- Model guard: the test FAILS if settings.llm_model is not OpenRouter qwen3.6
  (the load_dotenv-is-CWD-relative Ollama-fallback bug guard). Export
  LLM_MODEL=openrouter/qwen/qwen3.6-35b-a3b + LLM_API_KEY from the repo .env
  before running.
- CI safety: PAIRWISE_CANARY_SKIP=1 -> whole module skips (no model calls).
"""
import json
import os

import pytest
from litellm import completion

from ai_classification.config import settings

FIXTURE = os.path.join(os.path.dirname(__file__), "pairwise_canary_fixture.json")
REQUIRED_MODEL_SUBSTR = ("openrouter", "qwen3.6")

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
Return ONLY a JSON array. Strict binary; when in doubt, NO."""


def _load_fixture():
    with open(FIXTURE) as fh:
        return json.load(fh)


def _verify_pair(pair, max_tokens=300):
    """One strict-prompt verdict for a pair. Returns decision string or None."""
    from typing import Any

    user = (
        "Verify this ONE pair (FULL ticket text):\n\n"
        f"Pair 1:\n[A] {pair['title_a']}\n    {pair['description_a']}\n"
        f"[B] {pair['title_b']}\n    {pair['description_b']}"
    )
    kwargs: dict[str, Any] = dict(
        model=settings.llm_model,
        temperature=0.0,
        seed=42,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": STRICT_PROMPT_V3},
            {"role": "user", "content": user},
        ],
    )
    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base
    if settings.llm_api_key:
        kwargs["api_key"] = settings.llm_api_key
    if "qwen3" in settings.llm_model.lower():
        kwargs["extra_body"] = {"reasoning": {"enabled": False}}
    resp = completion(**kwargs)
    content = resp.choices[0].message.content.strip()
    text = content.strip("`").strip()
    if text.startswith("json"):
        text = text[4:].strip()
    import re
    i, j = text.find("["), text.rfind("]")
    if i != -1 and j > i:
        text = text[i:j + 1]
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    for item in data:
        if item.get("decision") in ("YES", "NO"):
            return item["decision"]
    return None


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
    assert fx["n_pairs"] == 34
    assert fx["n_wrong"] == 22 and fx["n_correct"] == 12
    assert len(fx["pairs"]) == 34


@pytest.mark.parametrize("idx", range(34))
def test_pair_verdict(idx):
    """Live-model re-verification: wrong pairs must be NO, correct pairs must be YES."""
    fx = _load_fixture()
    pair = fx["pairs"][idx]
    decision = _verify_pair(pair)
    assert decision is not None, f"pair {idx} ({pair['id_a'][:6]}~{pair['id_b'][:6]}): LLM response unparseable"
    assert decision == pair["expected"], (
        f"pair {idx} {pair['id_a'][:6]}~{pair['id_b'][:6]} [{pair['judgment']}]: "
        f"expected {pair['expected']}, got {decision}"
    )
