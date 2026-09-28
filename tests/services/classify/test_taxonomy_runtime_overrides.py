"""Runtime-override registry atomicity (admin reload vs. request threads).

Regression (taxonomy.set_runtime_overrides): the registry was clear()ed and
refilled IN PLACE while other threads iterate it (prompt building, service
validation), which raised "dictionary changed size during iteration" and
failed the ticket being classified. It is now built fresh and the module
global is rebound in ONE assignment (same as set_active_systems), so a
reader that is mid-iteration keeps the consistent snapshot it started on.
"""

import pytest

import ai_classification.domain.taxonomy as tax
from ai_classification.domain.taxonomy import AffectedSystem


@pytest.fixture(autouse=True)
def _restore_overrides():
    """Never leak overrides into other tests."""
    yield
    tax.set_runtime_overrides({})


def test_reader_mid_iteration_survives_a_reload():
    tax.set_runtime_overrides({"Other": {"Existing": ["o"]}})
    it = iter(tax._runtime_overrides.items())
    first = next(it)                                     # reader is mid-iteration
    tax.set_runtime_overrides({"Other": {"Existing": ["o"]},
                               "CRM": {"CRM svc": ["o"]}})  # reload (was: clear+update)
    assert first == ("Other", {"Existing": ["o"]})
    assert list(it) == []                                # ends cleanly, no RuntimeError


def test_readers_see_the_new_registry_immediately():
    tax.set_runtime_overrides({"Other": {"Existing": ["o"]}})
    assert "Existing" in tax.effective_services_by_system()[AffectedSystem.other]
    assert tax.runtime_overrides() == {"Other": {"Existing": ["o"]}}

    tax.set_runtime_overrides({})
    assert "Existing" not in tax.effective_services_by_system()[AffectedSystem.other]
    assert tax.runtime_overrides() == {}


def test_registry_is_not_aliased_to_the_caller_dict():
    merged = {"Other": {"Existing": ["o"]}}
    tax.set_runtime_overrides(merged)
    merged["CRM"] = {"CRM svc": ["o"]}      # caller keeps building its own dict
    assert tax.runtime_overrides() == {"Other": {"Existing": ["o"]}}
