"""Shared fixtures for HA Banking tests."""

import pytest

# Import early so this repo's custom_components package is cached before the
# test harness puts its own testing_config/custom_components on sys.path.
import custom_components.ha_banking  # noqa: F401


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    yield
