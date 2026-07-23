"""HTTP API surface.

Phase 1 exposes only the minimum service boundary needed to prove the slice: a
campaign-creation route and a staged-import route, both gated by the
``csv_import`` feature switch (default off).
"""

from __future__ import annotations
