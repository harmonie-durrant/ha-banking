"""Balance sensors for HA Banking."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ACCOUNTS, CONF_ASPSP, DOMAIN
from .coordinator import HaBankingConfigEntry, HaBankingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaBankingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up balance sensors, one per account."""
    coordinator = entry.runtime_data
    async_add_entities(
        BalanceSensor(coordinator, account) for account in entry.data[CONF_ACCOUNTS]
    )


class BalanceSensor(CoordinatorEntity[HaBankingCoordinator], SensorEntity):
    """Main balance of a bank account."""

    _attr_has_entity_name = True
    _attr_translation_key = "balance"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: HaBankingCoordinator, account: dict[str, Any]) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._account_id: str = account["id"]
        bank = coordinator.config_entry.data[CONF_ASPSP]
        iban: str | None = account.get("iban")
        name = account.get("name") or (f"{bank} …{iban[-4:]}" if iban else bank)
        self._attr_unique_id = f"{self._account_id}_balance"
        self._attr_native_unit_of_measurement = account.get("currency")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._account_id)},
            name=name,
            manufacturer=bank,
            model=iban,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _balance(self):
        return (self.coordinator.data or {}).get(self._account_id)

    @property
    def available(self) -> bool:
        """Return True if a balance is known."""
        return super().available and self._balance is not None

    @property
    def native_value(self) -> Decimal | None:
        """Return the balance amount."""
        return self._balance.amount if self._balance else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the balance currency."""
        if self._balance and self._balance.currency:
            return self._balance.currency
        return self._attr_native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose which balance type was used."""
        if not self._balance:
            return None
        return {
            "balance_type": self._balance.balance_type,
            "reference_date": self._balance.reference_date,
        }
