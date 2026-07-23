"""Feature-switch tests (FND-007)."""

from __future__ import annotations

import pytest
from app.core.features import FeatureFlags
from pydantic import ValidationError


def test_all_flags_default_off() -> None:
    flags = FeatureFlags()
    dumped = flags.model_dump()
    assert dumped, "expected at least one feature flag defined"
    assert all(value is False for value in dumped.values())
    assert flags.enabled() == []


def test_enabled_reports_only_true_flags() -> None:
    flags = FeatureFlags(csv_import=True, scoring=True)
    assert set(flags.enabled()) == {"csv_import", "scoring"}


def test_flags_are_immutable() -> None:
    flags = FeatureFlags()
    with pytest.raises(ValidationError):
        flags.csv_import = True  # type: ignore[misc]
