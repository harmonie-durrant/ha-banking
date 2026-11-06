"""The HA Banking integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from .const import CONF_HISTORY_PENDING, CONF_IMPORT_HISTORY, DOMAIN, STORAGE_VERSION
from .coordinator import HaBankingConfigEntry, HaBankingCoordinator, selected_accounts
from .history import async_import_history

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: HaBankingConfigEntry) -> bool:
    """Set up HA Banking from a config entry."""
    _remove_untracked_devices(hass, entry)
    coordinator = HaBankingCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if entry.data.get(CONF_HISTORY_PENDING):
        if entry.options.get(CONF_IMPORT_HISTORY, True):
            entry.async_create_background_task(
                hass, async_import_history(hass, entry), f"{DOMAIN} history import"
            )
        else:
            hass.config_entries.async_update_entry(
                entry,
                data={k: v for k, v in entry.data.items() if k != CONF_HISTORY_PENDING},
            )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HaBankingConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: HaBankingConfigEntry) -> None:
    """Delete the persisted balances when the entry is removed."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()


def _remove_untracked_devices(hass: HomeAssistant, entry: HaBankingConfigEntry) -> None:
    """Remove devices (and their entities) of accounts no longer tracked."""
    tracked = {acc["id"] for acc in selected_accounts(entry)}
    dev_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        if not any(
            domain == DOMAIN and ident in tracked for domain, ident in device.identifiers
        ):
            dev_reg.async_remove_device(device.id)
