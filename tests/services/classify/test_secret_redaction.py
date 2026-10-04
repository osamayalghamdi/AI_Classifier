"""Credentials must never reach the log table, the job row or the API.

Provider SDKs embed the request headers in their exception text — litellm's
HTTP error message contains `'Authorization': 'Bearer sk-or-v1-…'`, i.e. the
LIVE API key. Those strings were written verbatim into
`classification_log.raw_verdict` ("<stage call failed: …>") and into
`ingestion_jobs.error_message`, both of which are stored in the database and
served back by the API.
"""

from ai_classification.services.classify.llm import redact_secrets

LIVE_KEY = "sk-or-v1-" + "0" * 64   # shape-only: never a real key


class TestRedactSecrets:
    def test_provider_error_with_authorization_header(self):
        msg = f"litellm.APIConnectionError: {{'Authorization': 'Bearer {LIVE_KEY}', 'Content-Type': 'application/json'}}"
        out = redact_secrets(msg)
        assert LIVE_KEY not in out
        assert "sk-or-v1" not in out
        assert "<redacted>" in out
        # the useful part of the error survives
        assert "APIConnectionError" in out

    def test_key_value_forms(self):
        for raw in (f"LLM_API_KEY={LIVE_KEY}",
                    f"api_key: {LIVE_KEY}",
                    f"token={LIVE_KEY}",
                    f'password="{LIVE_KEY}"'):
            assert LIVE_KEY not in redact_secrets(raw), raw

    def test_plain_errors_are_untouched(self):
        for raw in ("HTTP 429 rate limit exceeded",
                    "connection refused to postgres:5432",
                    "json.JSONDecodeError: Expecting value: line 1 column 1"):
            assert redact_secrets(raw) == raw

    def test_handles_none_and_objects(self):
        assert redact_secrets(None) == ""
        assert "boom" in redact_secrets(ValueError("boom"))

    def test_stage_failure_text_written_to_the_log_is_redacted(self):
        """The exact string the cascade logs into classification_log."""
        from ai_classification.services.classify.cascade import _log_classification  # noqa: F401
        text = f"<stage call failed: Client error {{'Authorization': 'Bearer {LIVE_KEY}'}}>"
        assert LIVE_KEY not in redact_secrets(text)
