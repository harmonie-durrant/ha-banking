"""Sensors for HA Banking."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ACCOUNTS, CONF_ASPSP, DOMAIN
from .coordinator import (
    AccountBalance,
    HaBankingConfigEntry,
    HaBankingCoordinator,
    access_valid_until,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaBankingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors, one device per account."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for account in entry.data[CONF_ACCOUNTS]:
        entities.append(BalanceSensor(coordinator, account))
        entities.append(AccessExpirySensor(coordinator, account))
    async_add_entities(entities)


class HaBankingEntity(CoordinatorEntity[HaBankingCoordinator]):
    """Base entity attached to a bank account device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: HaBankingCoordinator, account: dict[str, Any], key: str
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._account_id: str = account["id"]
        bank = coordinator.config_entry.data[CONF_ASPSP]
        iban: str | None = account.get("iban")
        name = account.get("name") or (f"{bank} …{iban[-4:]}" if iban else bank)
        self._attr_translation_key = key
        self._attr_unique_id = f"{self._account_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._account_id)},
            name=name,
            manufacturer=bank,
            model=iban,
            entry_type=DeviceEntryType.SERVICE,
        )


class BalanceSensor(HaBankingEntity, SensorEntity):
    """Main balance of a bank account."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: HaBankingCoordinator, account: dict[str, Any]) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, account, "balance")
        self._attr_native_unit_of_measurement = account.get("currency")

    @property
    def _balance(self) -> AccountBalance | None:
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
        """Expose which balance type was used and when it was fetched."""
        if not self._balance:
            return None
        last_fetch = self.coordinator.last_fetch
        return {
            "balance_type": self._balance.balance_type,
            "reference_date": self._balance.reference_date,
            "fetched_at": last_fetch.isoformat() if last_fetch else None,
        }


class AccessExpirySensor(HaBankingEntity, SensorEntity):
    """When the bank access (consent) expires and must be renewed."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HaBankingCoordinator, account: dict[str, Any]) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, account, "access_expires")

    @property
    def available(self) -> bool:
        """Stay available even when the bank cannot be reached."""
        return True

    @property
    def native_value(self) -> datetime | None:
        """Return the access expiry date."""
        return access_valid_until(self.coordinator.config_entry)
