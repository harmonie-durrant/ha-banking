"""Data update coordinator for HA Banking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    EnableBankingAuthError,
    EnableBankingClient,
    EnableBankingError,
    EnableBankingRateLimitError,
    EnableBankingSessionError,
)
from .const import (
    BALANCE_TYPE_AUTO,
    BALANCE_TYPE_PRIORITY,
    CACHE_MARGIN,
    CONF_ACCOUNTS,
    CONF_APPLICATION_ID,
    CONF_BALANCE_TYPE,
    CONF_PRIVATE_KEY,
    CONF_UPDATE_INTERVAL,
    CONF_VALID_UNTIL,
    DEFAULT_UPDATE_HOURS,
    DOMAIN,
    REAUTH_BEFORE_EXPIRY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

type HaBankingConfigEntry = ConfigEntry[HaBankingCoordinator]
type RawBalances = dict[str, list[dict[str, Any]]]
type BalanceData = dict[str, AccountBalance | None]


@dataclass
class AccountBalance:
    """Main balance of an account."""

    amount: Decimal
    currency: str
    balance_type: str | None
    reference_date: str | None


def pick_main_balance(
    balances: list[dict[str, Any]], preferred: str | None = None
) -> AccountBalance | None:
    """Select the preferred balance type, else the first by BALANCE_TYPE_PRIORITY."""
    if not balances:
        return None

    def rank(bal: dict[str, Any]) -> int:
        btype = bal.get("balance_type")
        if preferred and btype == preferred:
            return -1
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
    )


def access_valid_until(entry: ConfigEntry) -> datetime | None:
    """Return when the bank access of this entry expires."""
    if raw := entry.data.get(CONF_VALID_UNTIL):
        return dt_util.parse_datetime(raw)
    return None


class HaBankingCoordinator(DataUpdateCoordinator[BalanceData]):
    """Fetch balances for every account of a bank session.

    Banks only allow a few unattended requests per day, so the raw balances are
    persisted and reused while younger than the update interval (restarts,
    reloads, option changes), and kept on screen when the bank rate-limits us.
    """

    config_entry: HaBankingConfigEntry

    def __init__(self, hass: HomeAssistant, entry: HaBankingConfigEntry) -> None:
        """Initialize the coordinator."""
        update_interval = timedelta(
            hours=entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_HOURS)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.client = EnableBankingClient(
            async_get_clientsession(hass),
            entry.data[CONF_APPLICATION_ID],
            entry.data[CONF_PRIVATE_KEY],
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._cache_max_age = update_interval - CACHE_MARGIN
        preferred = entry.options.get(CONF_BALANCE_TYPE, BALANCE_TYPE_AUTO)
        self._preferred = None if preferred == BALANCE_TYPE_AUTO else preferred
        self.last_fetch: datetime | None = None
        self._raw: RawBalances = {}
        self._force_next = False

    async def _async_setup(self) -> None:
        """Load the last fetched balances from disk."""
        stored = await self._store.async_load()
        if not stored:
            return
        try:
            raw = stored["accounts"]
            if not isinstance(raw, dict):
                raise TypeError
            self.last_fetch = dt_util.parse_datetime(stored["fetched_at"])
            self._raw = raw
        except (KeyError, TypeError, ValueError):
            # Unknown/older cache format: fetch from the bank once.
            _LOGGER.debug("Ignoring cached balances in an unknown format")
            self.last_fetch = None
            self._raw = {}

    async def async_force_refresh(self) -> None:
        """Fetch from the bank now, ignoring the cache."""
        self._force_next = True
        await self.async_refresh()

    def _build(self, raw: RawBalances) -> BalanceData:
        return {
            account_id: pick_main_balance(balances, self._preferred)
            for account_id, balances in raw.items()
        }

    async def _async_update_data(self) -> BalanceData:
        now = dt_util.utcnow()
        accounts = self.config_entry.data[CONF_ACCOUNTS]
        force, self._force_next = self._force_next, False

        valid_until = access_valid_until(self.config_entry)
        if valid_until and now >= valid_until:
            raise ConfigEntryAuthFailed("Bank access has expired")
        if valid_until and valid_until - now < REAUTH_BEFORE_EXPIRY:
            # Data keeps flowing; this only prompts the user to renew in time.
            self.config_entry.async_start_reauth(self.hass)

        if (
            not force
            and self.last_fetch is not None
            and now - self.last_fetch < self._cache_max_age
            and {acc["id"] for acc in accounts} <= self._raw.keys()
        ):
            _LOGGER.debug("Using balances fetched at %s", self.last_fetch)
            return self._build(self._raw)

        raw: RawBalances = {}
        for account in accounts:
            try:
                balances = await self.client.get_balances(account["uid"])
            except EnableBankingSessionError as err:
                raise ConfigEntryAuthFailed(
                    f"Bank access is no longer valid: {err}"
                ) from err
            except EnableBankingRateLimitError as err:
                if not self._raw:
                    raise UpdateFailed(f"Bank rate limit reached: {err}") from err
                _LOGGER.warning(
                    "Bank rate limit reached, keeping balances from %s until next refresh",
                    self.last_fetch,
                )
                # Count this as a fetch so we don't retry before the next interval.
                self.last_fetch = now
                return self._build(self._raw)
            except EnableBankingAuthError as err:
                raise UpdateFailed(
                    f"Enable Banking rejected the application credentials: {err}"
                ) from err
            except EnableBankingError as err:
                raise UpdateFailed(str(err)) from err
            _LOGGER.debug(
                "Balances for %s: %s",
                account.get("name"),
                [(b.get("balance_type"), b.get("balance_amount")) for b in balances],
            )
            raw[account["id"]] = balances

        self.last_fetch = now
        self._raw = raw
        await self._store.async_save({"fetched_at": now.isoformat(), "accounts": raw})

        data = self._build(raw)
        for account in accounts:
            if data.get(account["id"]) is None:
                _LOGGER.warning(
                    "%s returned no usable balance for account %s",
                    self.config_entry.title,
                    account.get("name") or account["id"],
                )
        return data

    @property
    def raw_balances(self) -> RawBalances:
        """All balances last returned by the bank, per account."""
        return self._raw
