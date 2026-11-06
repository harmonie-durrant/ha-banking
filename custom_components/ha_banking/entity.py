"""Base entity for HA Banking."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ASPSP, DOMAIN
from .coordinator import HaBankingCoordinator


class HaBankingEntity(CoordinatorEntity[HaBankingCoordinator]):
    """Entity attached to a bank account device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: HaBankingCoordinator, account: dict[str, Any], key: str
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._account_id: str = account["id"]
        bank = coordinator.config_entry.data[CONF_ASPSP]
        iban: str | None = account.get("iban")
        mask = account.get("mask") or (iban[-4:] if iban else None)
        name = account.get("name") or (f"{bank} …{mask}" if mask else bank)
        self._attr_translation_key = key
        self._attr_unique_id = f"{self._account_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._account_id)},
            name=name,
            manufacturer=bank,
            model=iban,
            entry_type=DeviceEntryType.SERVICE,
        )
