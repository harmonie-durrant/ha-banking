"""Import past balances into Home Assistant long-term statistics.

Banks don't provide past balances, so they are rebuilt from booked
transactions: starting from the current booked balance, each day's
transactions are undone to get the previous day's end-of-day balance.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .api import (
    EnableBankingError,
    EnableBankingRateLimitError,
    EnableBankingSessionError,
)
from .const import (
    CONF_HISTORY_PENDING,
    DOMAIN,
    HISTORY_DAYS,
    HISTORY_FALLBACK_DAYS,
    MAX_TRANSACTION_PAGES,
)
from .coordinator import (
    HaBankingConfigEntry,
    HaBankingCoordinator,
    pick_main_balance,
    selected_accounts,
)

_LOGGER = logging.getLogger(__name__)


def _tx_date(tx: dict[str, Any]) -> date | None:
    for key in ("booking_date", "value_date", "transaction_date"):
        if (raw := tx.get(key)) and (parsed := dt_util.parse_date(str(raw)[:10])):
            return parsed
    return None


def _tx_amount(tx: dict[str, Any]) -> Decimal | None:
    try:
        amount = abs(Decimal(str(tx["transaction_amount"]["amount"])))
    except (KeyError, TypeError, InvalidOperation):
        return None
    return -amount if tx.get("credit_debit_indicator") == "DBIT" else amount


def daily_balances(
    anchor_amount: Decimal,
    anchor_date: date,
    transactions: list[dict[str, Any]],
    date_from: date,
) -> list[tuple[date, Decimal]]:
    """Rebuild end-of-day balances, oldest first.

    Only covers days from the oldest returned transaction (not before
    date_from) up to anchor_date, so days the bank didn't report are never
    invented.
    """
    net: dict[date, Decimal] = defaultdict(Decimal)
    for tx in transactions:
        if tx.get("status") not in (None, "BOOK"):
            continue
        day, amount = _tx_date(tx), _tx_amount(tx)
        if day is None or amount is None:
            continue
        net[day] += amount
    if not net:
        return []
    start = max(date_from, min(net))

    result: list[tuple[date, Decimal]] = []
    balance, day = anchor_amount, anchor_date
    while day >= start:
        result.append((day, balance))
        balance -= net[day]
        day -= timedelta(days=1)
    result.reverse()
    return result


async def async_first_statistic(
    hass: HomeAssistant, statistic_id: str, start: datetime
) -> dict[str, Any] | None:
    """Return the oldest recorded statistics row since start, if any."""

    def _query() -> dict[str, Any] | None:
        rows = statistics_during_period(
            hass, start, None, {statistic_id}, "hour", None, {"state", "sum"}
        )
        return rows[statistic_id][0] if rows.get(statistic_id) else None

    return await get_instance(hass).async_add_executor_job(_query)


async def _async_fetch_transactions(
    coordinator: HaBankingCoordinator, account_uid: str, today: date
) -> tuple[list[dict[str, Any]], date]:
    """Ask for HISTORY_DAYS, falling back to what banks always allow."""
    date_from = today - timedelta(days=HISTORY_DAYS)
    try:
        return (
            await coordinator.client.get_transactions(
                account_uid, date_from, MAX_TRANSACTION_PAGES
            ),
            date_from,
        )
    except (EnableBankingSessionError, EnableBankingRateLimitError):
        raise
    except EnableBankingError as err:
        _LOGGER.info(
            "Bank refused %s days of history (%s), retrying with %s days",
            HISTORY_DAYS,
            err,
            HISTORY_FALLBACK_DAYS,
        )
    date_from = today - timedelta(days=HISTORY_FALLBACK_DAYS)
    return (
        await coordinator.client.get_transactions(
            account_uid, date_from, MAX_TRANSACTION_PAGES
        ),
        date_from,
    )


async def async_import_history(hass: HomeAssistant, entry: HaBankingConfigEntry) -> None:
    """Import rebuilt daily balances for every account, then clear the flag."""
    try:
        await _async_import_all(hass, entry)
    finally:
        data = {k: v for k, v in entry.data.items() if k != CONF_HISTORY_PENDING}
        hass.config_entries.async_update_entry(entry, data=data)


async def _async_import_all(hass: HomeAssistant, entry: HaBankingConfigEntry) -> None:
    if "recorder" not in hass.config.components:
        _LOGGER.info("Recorder not loaded, skipping history import")
        return
    coordinator = entry.runtime_data
    ent_reg = er.async_get(hass)
    now = dt_util.now()
    today = now.date()

    for account in selected_accounts(entry):
        name = account.get("name") or account["id"]
        entity_id = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{account['id']}_balance"
        )
        # Booked transactions only, so anchor on the booked balance.
        anchor = pick_main_balance(coordinator.raw_balances.get(account["id"], []))
        if entity_id is None or anchor is None:
            continue

        try:
            transactions, date_from = await _async_fetch_transactions(
                coordinator, account["uid"], today
            )
        except EnableBankingRateLimitError as err:
            _LOGGER.warning("Bank rate limit reached, history not imported: %s", err)
            return
        except EnableBankingError as err:
            _LOGGER.warning("Could not fetch history for %s: %s", name, err)
            continue

        anchor_date = dt_util.parse_date(anchor.reference_date or "") or today
        days = daily_balances(anchor.amount, anchor_date, transactions, date_from)
        _LOGGER.debug(
            "%s: %d transactions, rebuilt %d days", name, len(transactions), len(days)
        )

        # One row per day, for the hour ending at local midnight. Skip hours
        # not finished yet and anything already recorded by Home Assistant.
        rows: list[tuple[datetime, float]] = []
        for day, balance in days:
            start = dt_util.start_of_local_day(day + timedelta(days=1)) - timedelta(
                hours=1
            )
            if start + timedelta(hours=1) <= now:
                rows.append((start, float(balance)))
        if not rows:
            _LOGGER.info("No history to import for %s", name)
            continue

        first = await async_first_statistic(hass, entity_id, rows[0][0])
        if first is not None and first.get("sum") is not None and first.get("state") is not None:
            first_start = dt_util.utc_from_timestamp(first["start"])
            rows = [row for row in rows if row[0] < first_start]
            if not rows:
                _LOGGER.info("History for %s is already recorded", name)
                continue
            # Line sums up with the existing statistics so there's no jump.
            offset = first["sum"] - first["state"]
        else:
            offset = -rows[0][1]

        statistics = [
            StatisticData(start=start, state=state, sum=state + offset)
            for start, state in rows
        ]
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_class=None,
            unit_of_measurement=anchor.currency or account.get("currency"),
        )
        async_import_statistics(hass, metadata, statistics)
        _LOGGER.info(
            "Imported %d days of balance history for %s (from %s)",
            len(statistics),
            name,
            rows[0][0].date(),
        )
