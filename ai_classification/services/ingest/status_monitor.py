"""Service status monitor — periodic per-service health probes with LOUD
logging on state changes.

Why: on a fresh deployment the LLM endpoint (e.g. the company ELM gateway)
may be unreachable. The readiness endpoint reports it, but only when
called. This monitor runs in the background, probes each service every
N seconds, and logs a CLEAR warning at startup + on every state CHANGE
(up→down / down→up), so a dead LLM is visible in the logs without digging
through cascade warnings.

State is cached and exposed via GET /status (no auth — it sits next to
/health and /ready).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from ai_classification.shared.config import settings

_log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 60.0


@dataclass
class ServiceStatus:
    name: str
    status: str = "unknown"          # ok | unreachable | error
    detail: str = ""
    last_checked: str = ""
    resolved: dict = field(default_factory=dict)   # model/base the probe used


class _Monitor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._services: dict[str, ServiceStatus] = {}
        self._interval = DEFAULT_INTERVAL_S
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── probe implementations ─────────────────────────────────────────
    def _probe_db(self) -> ServiceStatus:
        st = ServiceStatus(name="db")
        try:
            from ai_classification.services.jobs.integration import _connect
            with _connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            st.status, st.detail = "ok", ""
        except Exception as exc:  # noqa: BLE001
            st.status, st.detail = "error", f"{type(exc).__name__}: {exc}"
        return st

    def _probe_embedding(self) -> ServiceStatus:
        st = ServiceStatus(name="embedding")
        st.resolved = {"model": settings.embedding_model_name}
        try:
            from ai_classification.shared.store import store
            if store._model is None:
                st.status, st.detail = "error", "model not loaded"
            else:
                st.status, st.detail = "ok", ""
        except Exception as exc:  # noqa: BLE001
            st.status, st.detail = "error", f"{type(exc).__name__}: {exc}"
        return st

    def _probe_llm(self) -> ServiceStatus:
        st = ServiceStatus(name="llm")
        st.resolved = {
            "model": settings.llm_model,
            "api_base": settings.llm_api_base or "(provider default)",
        }
        try:
            from litellm import completion
            kwargs: dict = dict(
                model=settings.llm_model,
                max_tokens=1,
                temperature=0.0,
                timeout=5.0,
                messages=[{"role": "user", "content": "ping"}],
            )
            if settings.llm_api_base:
                kwargs["api_base"] = settings.llm_api_base
            if settings.llm_api_key:
                kwargs["api_key"] = settings.llm_api_key
            completion(**kwargs)
            st.status, st.detail = "ok", ""
        except Exception as exc:  # noqa: BLE001
            st.status = "unreachable"
            st.detail = f"{type(exc).__name__}: {str(exc)[:200]}"
        return st

    # ── orchestration ─────────────────────────────────────────────────
    def _probe_all(self, announce_change: bool) -> None:
        probes = {
            "db": self._probe_db,
            "embedding": self._probe_embedding,
            "llm": self._probe_llm,
        }
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for name, probe in probes.items():
            fresh = probe()
            fresh.last_checked = now
            with self._lock:
                prev = self._services.get(name)
                self._services[name] = fresh
            if announce_change and prev is not None and prev.status != fresh.status:
                if fresh.status == "ok":
                    _log.warning("SERVICE %s: RECOVERED — %s", name.upper(), fresh.resolved)
                else:
                    _log.warning(
                        "SERVICE %s: DOWN — %s [%s] %s",
                        name.upper(), fresh.resolved, fresh.status, fresh.detail,
                    )

    def snapshot(self) -> dict[str, ServiceStatus]:
        with self._lock:
            return dict(self._services)

    def _run(self) -> None:
        # First pass: announce initial state LOUDLY so a dead LLM is
        # visible from the very first log line.
        self._probe_all(announce_change=False)
        with self._lock:
            for st in self._services.values():
                if st.status == "ok":
                    _log.info("SERVICE %s: ok (%s)", st.name.upper(), st.resolved)
                else:
                    _log.warning(
                        "SERVICE %s: %s (%s) — %s",
                        st.name.upper(), st.status.upper(), st.resolved, st.detail,
                    )
        while not self._stop.wait(self._interval):
            try:
                self._probe_all(announce_change=True)
            except Exception:  # noqa: BLE001 — never kill the loop
                _log.exception("status monitor probe cycle failed")

    def start(self, interval_s: float = DEFAULT_INTERVAL_S) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._interval = interval_s
        self._thread = threading.Thread(target=self._run, name="status-monitor", daemon=True)
        self._thread.start()
        _log.info("Status monitor started — probing db/embedding/llm every %ss", interval_s)


monitor = _Monitor()
