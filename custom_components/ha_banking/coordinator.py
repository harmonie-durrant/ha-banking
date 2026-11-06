"""Data update coordinator for HA Banking."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EnableBankingAuthError, EnableBankingClient, EnableBankingError
from .const import (
    BALANCE_TYPE_PRIORITY,
    CONF_ACCOUNTS,
    CONF_APPLICATION_ID,
    CONF_PRIVATE_KEY,
    DOMAIN,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

type HaBankingConfigEntry = ConfigEntry[HaBankingCoordinator]


@dataclass
class AccountBalance:
    """Main balance of an account."""

    amount: Decimal
    currency: str
    balance_type: str | None
    reference_date: str | None
    all_balances: list[dict[str, Any]]


def pick_main_balance(balances: list[dict[str, Any]]) -> AccountBalance | None:
    """Select the most relevant balance according to BALANCE_TYPE_PRIORITY."""
    if not balances:
        return None

    def rank(bal: dict[str, Any]) -> int:
        btype = bal.get("balance_type")
        return (
            BALANCE_TYPE_PRIORITY.index(btype)
            if btype in BALANCE_TYPE_PRIORITY
            else len(BALANCE_TYPE_PRIORITY)
        )

    best = min(balances, key=rank)
    amount = best.get("balance_amount") or {}
    try:
        value = Decimal(str(amount["amount"]))
    except (KeyError, InvalidOperation):
        return None
    return AccountBalance(
        amount=value,
        currency=amount.get("currency", ""),
        balance_type=best.get("balance_type"),
        reference_date=best.get("reference_date"),
        all_balances=balances,
    )


class HaBankingCoordinator(DataUpdateCoordinator[dict[str, AccountBalance | None]]):
    """Fetch balances for every account of a bank session."""

    config_entry: HaBankingConfigEntry

    def __init__(self, hass: HomeAssistant, entry: HaBankingConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = EnableBankingClient(
            async_get_clientsession(hass),
            entry.data[CONF_APPLICATION_ID],
            entry.data[CONF_PRIVATE_KEY],
        )

    async def _async_update_data(self) -> dict[str, AccountBalance | None]:
        result: dict[str, AccountBalance | None] = {}
        for account in self.config_entry.data[CONF_ACCOUNTS]:
            try:
                balances = await self.client.get_balances(account["uid"])
            except EnableBankingAuthError as err:
                # TODO: raise ConfigEntryAuthFailed once a reauth flow exists.
                raise UpdateFailed(
                    f"Bank access rejected, the session may have expired: {err}"
                ) from err
            except EnableBankingError as err:
                raise UpdateFailed(str(err)) from err
            result[account["id"]] = pick_main_balance(balances)
        return result
