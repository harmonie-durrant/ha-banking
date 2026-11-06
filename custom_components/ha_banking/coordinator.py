"""Data update coordinator for HA Banking."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
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
    BALANCE_TYPE_PRIORITY,
    CACHE_MAX_AGE,
    CONF_ACCOUNTS,
    CONF_APPLICATION_ID,
    CONF_PRIVATE_KEY,
    CONF_VALID_UNTIL,
    DOMAIN,
    REAUTH_BEFORE_EXPIRY,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

type HaBankingConfigEntry = ConfigEntry[HaBankingCoordinator]
type BalanceData = dict[str, AccountBalance | None]


@dataclass
class AccountBalance:
    """Main balance of an account."""

    amount: Decimal
    currency: str
    balance_type: str | None
    reference_date: str | None

    def as_dict(self) -> dict[str, Any]:
        """Serialize for storage."""
        return {**asdict(self), "amount": str(self.amount)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountBalance:
        """Deserialize from storage."""
        return cls(**{**data, "amount": Decimal(data["amount"])})


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
    )


def access_valid_until(entry: ConfigEntry) -> datetime | None:
    """Return when the bank access of this entry expires."""
    if raw := entry.data.get(CONF_VALID_UNTIL):
        return dt_util.parse_datetime(raw)
    return None


class HaBankingCoordinator(DataUpdateCoordinator[BalanceData]):
    """Fetch balances for every account of a bank session.

    Banks only allow a few unattended requests per day, so the last result is
    persisted and reused while it is younger than CACHE_MAX_AGE (restarts,
    reloads, manual refreshes), and kept on screen when the bank rate-limits us.
    """

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
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self.last_fetch: datetime | None = None
        self._cached: BalanceData = {}

    async def _async_setup(self) -> None:
        """Load the last fetched balances from disk."""
        stored = await self._store.async_load()
        if not stored:
            return
        try:
            self.last_fetch = dt_util.parse_datetime(stored["fetched_at"])
            self._cached = {
                account_id: AccountBalance.from_dict(bal) if bal else None
                for account_id, bal in stored["balances"].items()
            }
        except (KeyError, TypeError, ValueError, InvalidOperation):
            _LOGGER.debug("Ignoring invalid cached balances")
            self.last_fetch = None
            self._cached = {}

    async def _async_update_data(self) -> BalanceData:
        now = dt_util.utcnow()
        account_ids = {acc["id"] for acc in self.config_entry.data[CONF_ACCOUNTS]}

        valid_until = access_valid_until(self.config_entry)
        if valid_until and now >= valid_until:
            raise ConfigEntryAuthFailed("Bank access has expired")
        if valid_until and valid_until - now < REAUTH_BEFORE_EXPIRY:
            # Data keeps flowing; this only prompts the user to renew in time.
            self.config_entry.async_start_reauth(self.hass)

        previous = self.data if self.data is not None else self._cached
        if (
            self.last_fetch is not None
            and now - self.last_fetch < CACHE_MAX_AGE
            and account_ids <= previous.keys()
        ):
            _LOGGER.debug("Using balances fetched at %s", self.last_fetch)
            return previous

        result: BalanceData = {}
        for account in self.config_entry.data[CONF_ACCOUNTS]:
            try:
                balances = await self.client.get_balances(account["uid"])
            except EnableBankingSessionError as err:
                raise ConfigEntryAuthFailed(
                    f"Bank access is no longer valid: {err}"
                ) from err
            except EnableBankingRateLimitError as err:
                if not previous:
                    raise UpdateFailed(f"Bank rate limit reached: {err}") from err
                _LOGGER.warning(
                    "Bank rate limit reached, keeping balances from %s until next refresh",
                    self.last_fetch,
                )
                # Count this as a fetch so we don't retry before the next interval.
                self.last_fetch = now
                return previous
            except EnableBankingAuthError as err:
                raise UpdateFailed(
                    f"Enable Banking rejected the application credentials: {err}"
                ) from err
            except EnableBankingError as err:
                raise UpdateFailed(str(err)) from err
            result[account["id"]] = pick_main_balance(balances)

        self.last_fetch = now
        self._cached = result
        await self._store.async_save(
            {
                "fetched_at": now.isoformat(),
                "balances": {
                    account_id: bal.as_dict() if bal else None
                    for account_id, bal in result.items()
                },
            }
        )
        return result
