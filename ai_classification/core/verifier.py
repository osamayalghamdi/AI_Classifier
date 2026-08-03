"""Strict pairwise verifier (frozen prompt v3) + versioned verdict cache.

STRICT_PROMPT_V3 is VERBATIM from tests/test_pairwise_canary.py (the frozen,
manager-accepted dev-slice prompt). tests/test_suboffering.py asserts the two
copies stay identical (drift guard).

Frozen params (STATUS.md): batch 8 pairs/call, retry-once-individual then
UNRESOLVED->NO, cache key includes prompt_version, temp 0.0 seed 42,
OpenRouter qwen3.6 with reasoning disabled.
"""
import json
import logging
import os
import re
import time
from typing import Any

from litellm import completion

from ai_classification.config import settings

_log = logging.getLogger(__name__)

PROMPT_VERSION = "v3"
CACHE_PATH = os.environ.get(
    "SUB_OFFERING_VERIFIER_CACHE",
    "/tmp/pairwise_engine_cache/verdicts_v3.json",
)
BATCH_SIZE = 8
BATCH_MAX_TOKENS = 4000

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


def _key(id_a: str, id_b: str) -> str:
    return f"{PROMPT_VERSION}|{'|'.join(sorted([id_a, id_b]))}"


def _parse_batch(content: str) -> dict[int, tuple[str, str]]:
    """Robust parse: fences, preamble, truncation recovery, single-object wrap,
    and newline-separated concatenated objects (`{...}\n{...}`)."""
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
            data = json.loads("[" + re.sub(r"\}\s*\{", "},{", candidate) + "]")
    if isinstance(data, dict) and "pairs" in data:
        data = data["pairs"]
    if isinstance(data, dict):
        data = [data]
    out = {}
    for item in data:
        if isinstance(item, dict) and item.get("decision") in ("YES", "NO"):
            out[int(item["pair"])] = (item["decision"], str(item.get("reason", ""))[:150])
    return out


class Verifier:
    """Batch-8 strict verifier with a versioned on-disk verdict cache."""

    def __init__(self, cache_path: str = CACHE_PATH):
        self.cache_path = cache_path
        self.cache: dict[str, dict] = {}
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        self.unresolved: list[str] = []
        if os.path.exists(cache_path):
            with open(cache_path) as fh:
                self.cache = json.load(fh)

    def _save_cache(self):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.cache, fh, ensure_ascii=False)
        os.replace(tmp, self.cache_path)

    def _call(self, messages, max_tokens=BATCH_MAX_TOKENS) -> str:
        kwargs: dict[str, Any] = dict(
            model=settings.llm_model,
            temperature=0.0,
            seed=42,
            max_tokens=max_tokens,
            messages=messages,
        )
        if settings.llm_api_base:
            kwargs["api_base"] = settings.llm_api_base
        if settings.llm_api_key:
            kwargs["api_key"] = settings.llm_api_key
        if "qwen3" in settings.llm_model.lower():
            kwargs["extra_body"] = {"reasoning": {"enabled": False}}
        t0 = time.perf_counter()
        resp = completion(**kwargs)
        usage = getattr(resp, "usage", None)
        self.usage["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
        self.usage["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
        self.usage["calls"] += 1
        _log.info("verifier call #%d (%.1fs)", self.usage["calls"], time.perf_counter() - t0)
        return resp.choices[0].message.content

    @staticmethod
    def _pair_text(a: dict, b: dict) -> str:
        return (f"[A] {a['title']}\n    {a['description']}\n"
                f"[B] {b['title']}\n    {b['description']}")

    def _ask_batch(self, pairs) -> dict[int, tuple[str, str]]:
        lines = [f"Pair {n}:\n{self._pair_text(a, b)}"
                 for n, (a, b) in enumerate(pairs, 1)]
        user = (f"Verify these {len(pairs)} pairs (FULL ticket text):\n\n"
                + "\n\n".join(lines))
        try:
            return _parse_batch(self._call([
                {"role": "system", "content": STRICT_PROMPT_V3},
                {"role": "user", "content": user},
            ]))
        except Exception as e:
            _log.warning("batch parse failed: %s", e)
            return {}

    def _ask_individual(self, a: dict, b: dict) -> tuple[str, str] | None:
        try:
            parsed = _parse_batch(self._call([
                {"role": "system", "content": STRICT_PROMPT_V3},
                {"role": "user", "content":
                    f"Verify this ONE pair (FULL ticket text):\n\nPair 1:\n{self._pair_text(a, b)}"},
            ], max_tokens=800))
            return parsed.get(1)
        except Exception as e:
            _log.warning("individual parse failed: %s", e)
            return None

    def verify_pairs(self, pairs: list[tuple[dict, dict]]) -> list[dict]:
        """Verify pairs in batch-8 with versioned cache + retry-once-individual.
        Returns verdict dicts in input order: {decision, reason}."""
        keyed = []
        for a, b in pairs:
            k = _key(a["id"], b["id"])
            v = self.cache.get(k)
            keyed.append({"key": k, "a": a, "b": b,
                          "cached": v is not None, "v": v})
        fresh = [k for k in keyed if not k["cached"]]
        for start in range(0, len(fresh), BATCH_SIZE):
            chunk = fresh[start:start + BATCH_SIZE]
            by_pos = self._ask_batch([(k["a"], k["b"]) for k in chunk])
            for pos, k in enumerate(chunk, 1):
                v = by_pos.get(pos)
                if v is None:
                    v = self._ask_individual(k["a"], k["b"])
                    if v is None:
                        self.unresolved.append(k["key"])
                        v = ("NO", "UNRESOLVED (treated as NO)")
                self.cache[k["key"]] = {"decision": v[0], "reason": v[1]}
                k["v"] = self.cache[k["key"]]
        self._save_cache()
        return [{"decision": k["v"]["decision"], "reason": k["v"].get("reason", "")}
                for k in keyed]
