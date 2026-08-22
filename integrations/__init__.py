"""Integrations — standalone connectors that talk to the classifier ONLY
through its public HTTP API.

Each sub-package (e.g. `smax`) is a self-contained process: it must be
runnable on a machine that has only network access to the classifier API,
so nothing under integrations/ may import from the classifier app's
internals (enforced by a containment grep).
"""
