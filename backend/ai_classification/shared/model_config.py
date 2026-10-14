"""Model-configuration report — what is missing, what to set, what still works.

A model that is not configured is a deployment FACT, not a reason to die. The
app is hosted without its models on purpose: the dashboard, the ingest API, the
review surfaces and the E1-E9 integration all work, and only classification /
OCR / similarity degrade. Refusing to start turns "set one environment
variable" into a restart loop whose single line scrolls away with the pod, and
"why is nothing classified?" becomes a hunt instead of a sentence.

So startup never dies on a missing model. Instead this module produces ONE
report — every missing capability, the environment variables that fix it, and
what degrades while it is unset — which the lifespan logs at startup and
`/ready` (next to `/status`) serves as JSON, so the answer is in the log and in
one HTTP call.

Three readers must be kept in step, and two are easy to forget:

    /ready, /status, /test/llm   probe  LLM_MODEL / LLM_API_BASE
    the classifier cascade       calls  the ACTIVE registry entry
                                        (MODEL_<NAME>_ID / _API_BASE / _API_KEY)

Configure one and not the other and the app lies in a way that looks like a
real failure: a run that classifies normally still reports `llm: unreachable`,
or a green probe sits next to a cascade that never reaches the model.
`classifier_issues()` reports that split explicitly instead of leaving it to be
rediscovered.

Nothing here does I/O and nothing here raises — it is pure config inspection, so
a lifespan, a route handler or a test can call it freely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelIssue:
    """One unconfigured capability.

    `blocking` separates "this capability cannot work at all" (advisory=False)
    from "this works, but the reader will mislead you" (advisory) — a probe
    pointing at a different endpoint than the cascade is the second kind: it
    must be visible without making the host look broken.
    """

    capability: str          # classifier | classifier-probe | embedding | ocr | attachment-reader
    summary: str             # one line: what is wrong
    env: tuple[str, ...]     # the variables that fix it
    fix: str                 # one-line example / instruction
    effect: str              # what still works while it is unset
    blocking: bool = True    # False = advisory (nothing is unusable, the reading is just misleading)

    def as_dict(self) -> dict:
        return {
            "capability": self.capability,
            "summary": self.summary,
            "env": list(self.env),
            "fix": self.fix,
            "effect": self.effect,
            "blocking": self.blocking,
        }


def _get(settings, name: str, default: Any = "") -> Any:
    """Read a setting defensively: tests stub `settings` with a SimpleNamespace."""
    return getattr(settings, name, default)


def _is_loopback(url: str) -> bool:
    """True for a base URL that only reaches the machine you are already on."""
    host = (urlsplit(url).hostname or "") if url else ""
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def _cluster_only_host(url: str) -> str | None:
    """The host of a URL that can only resolve inside the cluster (a bare name).

    `http://ocr:8001` is right in k8s and unresolvable anywhere else, which is
    the shape of a deployment mistake rather than a transient failure — worth
    naming up front instead of letting every attachment log a connection error.
    """
    host = (urlsplit(url).hostname or "") if url else ""
    if not host or host == "localhost" or "." in host:
        return None
    return host


# ── Classifier (the one capability that must fail the job loudly) ──────

def classifier_issues(settings) -> list[ModelIssue]:
    import os

    entry = _get(settings, "active_classifier_model", None)
    # The ENV value, not settings.llm_model: that setting falls back to a
    # default id in config.py, and reporting that default as "LLM_MODEL=…" made
    # "you never set it" look like "you set it wrong" — the opposite fix.
    llm_model_env = (os.environ.get("LLM_MODEL") or "").strip()
    llm_model = (_get(settings, "llm_model", "") or "").strip()
    llm_base = (_get(settings, "llm_api_base", "") or "").strip()

    if entry is None:
        issues = [ModelIssue(
            capability="classifier",
            summary=("no classifier model is ENABLED ("
                     + (f"LLM_MODEL={llm_model_env!r} selects none" if llm_model_env
                        else "LLM_MODEL is unset") + ")"),
            env=("LLM_MODEL", "MODEL_<NAME>_ENABLED"),
            fix="LLM_MODEL=ollama/classifier-qwen with LLM_API_BASE=http://<host>:11434 (local Ollama, "
                "the default), or LLM_MODEL=openai/qwen3.6 with BASE_URL + UNIVERSAL_API_KEY for the "
                "company endpoint — or enable a catalog entry directly: MODEL_<NAME>_ENABLED=1",
            effect="every classification job ends LLM_UNAVAILABLE with this message recorded on the "
                   "job (no service / system / severity). Ingest, the dashboard, review and the "
                   "E1-E9 API keep working.",
        )]
        if llm_model_env.startswith("ollama/"):
            issues.append(ModelIssue(
                capability="classifier",
                summary="LLM_MODEL names a local Ollama model but the local entry is switched OFF",
                env=("MODEL_LOCAL_LLM_ENABLED", "LLM_MODEL"),
                fix="set MODEL_LOCAL_LLM_ENABLED=1 (an `ollama/...` LLM_MODEL enables the LOCAL_LLM "
                    "entry on its own — only an explicit MODEL_LOCAL_LLM_ENABLED=0 disables it)",
                effect="jobs fail fast with LLM_UNAVAILABLE while the Ollama server may be perfectly "
                       "healthy — check /ready rather than the Ollama logs",
            ))
        return issues

    provider = (entry.provider or "").strip().lower()
    issues: list[ModelIssue] = []

    # A local Ollama needs no key; every remote provider does.
    if provider != "ollama" and not (entry.api_key or "").strip():
        key_vars = ("LLM_API_KEY", f"MODEL_{entry.name}_API_KEY")
        if provider == "elm":
            key_vars = ("UNIVERSAL_API_KEY", "LLM_API_KEY", f"MODEL_{entry.name}_API_KEY")
        issues.append(ModelIssue(
            capability="classifier",
            summary=f"classifier '{entry.name}' is enabled with no API key",
            env=key_vars,
            fix=f"UNIVERSAL_API_KEY=… (company endpoint) or LLM_API_KEY=…, or per-model "
                f"MODEL_{entry.name}_API_KEY=…",
            effect="every call is rejected 401 → jobs end LLM_UNAVAILABLE",
        ))

    if provider == "ollama" and _is_loopback(entry.api_base):
        issues.append(ModelIssue(
            capability="classifier",
            summary=f"classifier '{entry.name}' points at {entry.api_base} — inside a pod or "
                    "container that is the container itself, not this machine",
            env=("LLM_API_BASE", f"MODEL_{entry.name}_API_BASE"),
            fix="LLM_API_BASE=http://192.168.1.50:11434 (the host running Ollama; use the host's "
                "LAN address, not localhost, for anything deployed)",
            effect="the call is refused (connection refused) and jobs end LLM_UNAVAILABLE, while the "
                   "same model answers fine from a shell on the host",
            blocking=False,
        ))

    if not (entry.api_base or "").strip():
        issues.append(ModelIssue(
            capability="classifier",
            summary=f"classifier '{entry.name}' is enabled with no API base URL",
            env=("BASE_URL", "LLM_API_BASE", f"MODEL_{entry.name}_API_BASE"),
            fix="BASE_URL=https://llms.elm.sa/v1 (company endpoint) or LLM_API_BASE=<host>/v1",
            effect="the call falls back to the provider default and fails → jobs end LLM_UNAVAILABLE",
        ))

    # The two-readers trap: the probe and the cascade must name the same model.
    entry_model = (entry.model_id or "").strip()
    if llm_model and entry_model != llm_model:
        issues.append(ModelIssue(
            capability="classifier-probe",
            summary=f"probe and cascade disagree on the model: LLM_MODEL={llm_model!r} but the "
                    f"active entry {entry.name} calls {entry_model!r}",
            env=("LLM_MODEL", f"MODEL_{entry.name}_ID"),
            fix="point both at the same model (set LLM_MODEL to the entry's id, or the entry's "
                f"MODEL_{entry.name}_ID to {llm_model})",
            effect="/ready, /status and /test/llm report on one model while jobs run on another — a "
                   "green probe next to failing jobs, or vice versa",
            blocking=False,
        ))
    entry_base = (entry.api_base or "").strip()
    if entry_base != llm_base and (entry_base or llm_base):
        issues.append(ModelIssue(
            capability="classifier-probe",
            summary=f"probe and cascade disagree on the endpoint: "
                    f"{llm_base or '(provider default)'} vs {entry_base or '(provider default)'}",
            env=("LLM_API_BASE", "BASE_URL", f"MODEL_{entry.name}_API_BASE"),
            fix="set LLM_API_BASE to the endpoint the registry uses (or the registry override to "
                "LLM_API_BASE) so the probe tests the same host the cascade calls",
            effect="the probe can pass while jobs fail, or fail while jobs pass",
            blocking=False,
        ))
    return issues


def classifier_blocking(settings) -> ModelIssue | None:
    """The first reason classification cannot work at all, or None."""
    return next((i for i in classifier_issues(settings) if i.blocking), None)


# ── Embeddings (similarity + clustering) ──────────────────────────────

def embedding_issues(settings) -> list[ModelIssue]:
    provider = (_get(settings, "embedding_provider", "") or "").strip().lower()
    url = (_get(settings, "embedding_api_url", "") or "").strip()

    if provider in ("local", "sentence-transformers", "st"):
        import importlib.util
        if importlib.util.find_spec("sentence_transformers") is None:
            return [ModelIssue(
                capability="embedding",
                summary="EMBEDDING_PROVIDER=local but the local embedding stack is not installed",
                env=("EMBEDDING_PROVIDER", "EMBEDDING_API_URL", "EMBEDDING_MODEL_ID"),
                fix="EMBEDDING_PROVIDER=external with EMBEDDING_API_URL=https://<host>/v1 "
                    "(+ EMBEDDING_MODEL_ID=bge-m3, EMBEDDING_DIM=1024), or install the "
                    "`local-models` extra on a dev box",
                effect="no vectors are produced: similar-incident search returns nothing and cluster "
                       "grouping has nothing to group on. Incidents still store and classify.",
            )]
        return []

    if provider in ("", "external"):
        if not url:
            return [ModelIssue(
                capability="embedding",
                summary="embeddings are external but EMBEDDING_API_URL is unset",
                env=("EMBEDDING_PROVIDER", "EMBEDDING_API_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL_ID"),
                fix="EMBEDDING_API_URL=https://<host>/v1 with EMBEDDING_API_KEY=… and "
                    "EMBEDDING_MODEL_ID=bge-m3 (EMBEDDING_DIM must match the endpoint's output)",
                effect="no vectors are produced: similar-incident search returns nothing and cluster "
                       "grouping has nothing to group on. Incidents still store and classify.",
            )]
        return []

    return [ModelIssue(
        capability="embedding",
        summary=f"unknown EMBEDDING_PROVIDER={provider!r}",
        env=("EMBEDDING_PROVIDER",),
        fix="EMBEDDING_PROVIDER=external (+ EMBEDDING_API_URL) for a deployed host, or local on a dev box",
        effect="embeddings are unavailable → no similarity, no clustering",
    )]


# ── OCR service + attachment vision reader (both external, both optional) ──

def ocr_issues(settings) -> list[ModelIssue]:
    if not _get(settings, "ocr_enabled", False):
        return []
    url = (_get(settings, "ocr_service_url", "") or "").strip()
    if not url:
        return [ModelIssue(
            capability="ocr",
            summary="OCR_ENABLED=1 but OCR_SERVICE_URL is unset",
            env=("OCR_SERVICE_URL", "OCR_ENABLED"),
            fix="OCR_SERVICE_URL=http://<ocr-host>:8001 (the OCR service is its own deployment), or "
                "OCR_ENABLED=0 to record attachments by name only",
            effect="attachment text is never transcribed; attachments are still recorded on the incident",
        )]
    host = _cluster_only_host(url)
    if host:
        return [ModelIssue(
            capability="ocr",
            summary=f"OCR_SERVICE_URL points at '{host}', a cluster-internal name",
            env=("OCR_SERVICE_URL", "OCR_ENABLED"),
            fix="point OCR_SERVICE_URL at the OCR service reachable from where this process runs, or "
                "set OCR_ENABLED=0",
            effect="each attachment's OCR call fails (logged, best-effort) — tickets still ingest, "
                   "just without extracted text",
            blocking=False,
        )]
    return []


def attachment_reader_issues(settings) -> list[ModelIssue]:
    if not _get(settings, "attachments_enabled", False):
        return []
    configured = getattr(settings, "attachment_reader_configured", None)
    if callable(configured) and configured():
        return []
    return [ModelIssue(
        capability="attachment-reader",
        summary="ATTACHMENTS_ENABLED=1 but the vision reader is not configured",
        env=("ATTACHMENT_VISION_API_URL", "ATTACHMENT_VISION_MODEL", "ATTACHMENT_VISION_API_KEY"),
        fix="set all three against an OpenAI-compatible vision endpoint with "
            "ATTACHMENT_VISION_MODEL in provider/model form, or ATTACHMENTS_ENABLED=0",
        effect="attachments are link-only and classification is text-only",
        blocking=False,
    )]


# ── The whole report ──────────────────────────────────────────────────

def inspect_model_config(settings=None) -> list[ModelIssue]:
    """Every unconfigured model/endpoint, in a stable order. Never raises."""
    if settings is None:
        from ai_classification.shared.config import settings as current
        settings = current
    issues: list[ModelIssue] = []
    for inspect in (classifier_issues, embedding_issues, ocr_issues, attachment_reader_issues):
        try:
            issues.extend(inspect(settings))
        except Exception:  # noqa: BLE001 — a report must never be the thing that crashes
            continue
    return issues


def report_lines(issues: list[ModelIssue]) -> list[str]:
    """The log block: one paragraph per gap — what, what to set, what degrades."""
    if not issues:
        return []
    lines = [f"{len(issues)} item(s) need attention — hosting anyway (see /ready):"]
    for n, issue in enumerate(issues, 1):
        tag = "" if issue.blocking else " (advisory)"
        lines.append(f"{n}. [{issue.capability}{tag}] {issue.summary}")
        lines.append(f"   set:    {' / '.join(issue.env)} — {issue.fix}")
        lines.append(f"   meanwhile: {issue.effect}")
    return lines


def as_dicts(issues: list[ModelIssue]) -> list[dict]:
    return [issue.as_dict() for issue in issues]


def summary(settings=None) -> dict:
    """The report as the routes serve it: configured? + the gaps, in full."""
    issues = inspect_model_config(settings)
    return {
        "configured": not issues,
        "blocking": [i.as_dict() for i in issues if i.blocking],
        "issues": as_dicts(issues),
    }
