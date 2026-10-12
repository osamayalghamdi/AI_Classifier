"""Canonical-statement post-processing — content must survive.

Regression guard for the 2026-09 fix: `_normalize_canonical` used to cut the
statement at the first colon, delete every 6+ digit run and every date-like
token, and delete words like "number". Prod canonicals were left unreadable
("Inter-city transport requests (IDs , ) are stuck…", "Recurring technical
error (Error ) occurs…", "00 with only two hours remaining.") — and those
strings are shown to operators and fed to the clustering prompts.
"""

from ai_classification.services.classify.parsing import _normalize_canonical


class TestCanonicalPreservesContent:
    def test_error_codes_and_ids_survive(self):
        cs = ("Recurring technical error (Error number 10036015) occurs when "
              "submitting a Rawdah permit.")
        assert _normalize_canonical(cs) == cs

    def test_invoice_ids_survive(self):
        cs = "Internal transfer of invoices failing for invoice numbers 800904037, 800912873."
        assert _normalize_canonical(cs) == cs

    def test_request_ids_survive(self):
        cs = "Inter-city transport requests 135217 and 135218 are stuck in a pending approval state."
        assert _normalize_canonical(cs) == cs

    def test_dates_survive(self):
        assert _normalize_canonical("Bus departs at 14:00 on 4/6/2026.") == "Bus departs at 14:00 on 4/6/2026."
        assert _normalize_canonical("Permit issue on 30-May-2026.") == "Permit issue on 30-May-2026."

    def test_counts_survive(self):
        cs = "600 pilgrims affected vs 13 pilgrims affected."
        assert _normalize_canonical(cs) == cs

    def test_colon_inside_prose_is_not_a_label_split(self):
        """'…at 14:00 with only two hours remaining.' must not become '00 …'."""
        assert _normalize_canonical("Bus departs at 14:00 with only two hours remaining.") \
            == "Bus departs at 14:00 with only two hours remaining."

    def test_words_like_number_are_kept(self):
        cs = "The system rejects the contract number entered by the provider."
        assert _normalize_canonical(cs) == cs


class TestCanonicalStripsOnlyRealLabels:
    def test_system_and_service_prefix_removed(self):
        assert _normalize_canonical("Nusuk Masar Haj/contracts: Portal fails to load.") \
            == "Portal fails to load."
        assert _normalize_canonical("Nusuk Masar Haj.Issue Permits: Rawdah permit fails.") \
            == "Rawdah permit fails."
        assert _normalize_canonical("CRM - Service Unavailability: Reports page broken.") \
            == "Reports page broken."

    def test_ordinary_prose_prefix_is_kept(self):
        cs = "Rejected: the commercial registration number must start with 4."
        assert _normalize_canonical(cs) == cs

    def test_whitespace_is_normalized(self):
        assert _normalize_canonical("  Two   spaces \n and a newline ") == "Two spaces and a newline"

    def test_empty_input(self):
        assert _normalize_canonical("") == ""
