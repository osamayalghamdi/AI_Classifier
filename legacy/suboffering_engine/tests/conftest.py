"""Pytest bootstrap for the quarantined sub-offering engine tests.

The legacy package lives at repo root (legacy/), so running pytest from the
repo root must see ai_classification/ too. Inserting the repo root into
sys.path makes both `ai_classification.*` and `legacy.*` importable
regardless of the invocation directory.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
