"""Refresh button for HA Banking."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import HaBankingConfigEntry, HaBankingCoordinator, selected_accounts
from .entity import HaBankingEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaBankingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one refresh button per account."""
    coordinator = entry.runtime_data
    async_add_entities(
        RefreshButton(coordinator, account) for account in selected_accounts(entry)
    )


class RefreshButton(HaBankingEntity, ButtonEntity):
    """Fetch balances from the bank now (all accounts of this bank)."""

    def __init__(self, coordinator: HaBankingCoordinator, account: dict[str, Any]) -> None:
        """Initialize the button."""
        super().__init__(coordinator, account, "refresh")

    @property
    def available(self) -> bool:
        """Allow pressing even when the last refresh failed."""
        return True

    async def async_press(self) -> None:
        """Refresh now. Counts toward the bank's daily request limit."""
        await self.coordinator.async_force_refresh()
