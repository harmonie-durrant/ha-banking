"""Tests for the balance history import.

The recorder is mocked (lookup of existing statistics and the import call):
the recorder test fixtures of pytest-homeassistant-custom-component don't work
on Python 3.14 yet. Everything up to what is handed to the recorder is real.
"""

from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.ha_banking.history import daily_balances

from .test_flow import API, BALANCE_ENTITY, BALANCES_URL, entry_data

TX_URL = f"{API}/accounts/u1/transactions"
HISTORY = "custom_components.ha_banking.history"


def tx(day: date, amount: str, indicator: str, status: str = "BOOK") -> dict[str, Any]:
    """Build a transaction."""
    return {
        "transaction_amount": {"amount": amount, "currency": "EUR"},
        "credit_debit_indicator": indicator,
        "status": status,
        "booking_date": day.isoformat(),
    }


def tx_params(today: date, days: int, **extra: str) -> dict[str, str]:
    """Query parameters of a transactions request."""
    return {
        "date_from": (today - timedelta(days=days)).isoformat(),
        "transaction_status": "BOOK",
        **extra,
    }


def test_daily_balances() -> None:
    """Balances are rebuilt backwards from the anchor, oldest first."""
    today = date(2026, 11, 6)
    txs = [
        tx(today - timedelta(days=1), "30.00", "DBIT"),
        tx(today - timedelta(days=3), "100.00", "CRDT"),
        tx(today - timedelta(days=3), "5.00", "DBIT", status="PDNG"),  # ignored
        tx(today - timedelta(days=4), "-20.00", "DBIT"),  # signed amount
    ]
    result = daily_balances(Decimal("1000"), today, txs, today - timedelta(days=180))

    # End-of-day balance of D = end of D+1 minus transactions booked on D+1.
    assert result == [
        (today - timedelta(days=4), Decimal("930")),
        (today - timedelta(days=3), Decimal("1030")),
        (today - timedelta(days=2), Decimal("1030")),
        (today - timedelta(days=1), Decimal("1000")),
        (today, Decimal("1000")),
    ]


def test_daily_balances_no_transactions() -> None:
    """Without transactions nothing is invented."""
    assert daily_balances(Decimal("1"), date(2026, 11, 6), [], date(2026, 5, 1)) == []


@pytest.fixture
def recorder() -> Generator[MagicMock]:
    """Mock the recorder: no existing statistics, capture imports."""
    mock = MagicMock()
    mock.first = AsyncMock(return_value=None)
    with (
        patch(f"{HISTORY}.async_first_statistic", mock.first),
        patch(f"{HISTORY}.async_import_statistics", mock.import_statistics),
    ):
        yield mock


@pytest.fixture
def today(hass: HomeAssistant, aioclient_mock) -> date:
    """Mock a booked balance anchored today, with the recorder loaded."""
    hass.config.components.add("recorder")
    day = dt_util.now().date()
    aioclient_mock.get(
        BALANCES_URL,
        json={
            "balances": [
                {
                    "balance_type": "CLBD",
                    "balance_amount": {"amount": "1234.56", "currency": "EUR"},
                    "reference_date": day.isoformat(),
                }
            ]
        },
    )
    return day


def add_entry(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    """Entry right after an authorization (history pending)."""
    entry = MockConfigEntry(
        domain="ha_banking",
        title="Boursorama",
        unique_id="h1",
        data={**entry_data(), "history_pending": True},
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set up the entry and wait for the background import."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def imported(recorder: MagicMock) -> list[tuple[date, float, float]]:
    """(local day, state, sum) of every imported row."""
    assert recorder.import_statistics.call_count == 1
    _, metadata, rows = recorder.import_statistics.call_args.args
    assert metadata["statistic_id"] == BALANCE_ENTITY
    assert metadata["source"] == "recorder"
    assert metadata["has_sum"] is True
    assert metadata["mean_type"] is StatisticMeanType.NONE
    assert metadata["unit_of_measurement"] == "EUR"
    result = []
    for row in rows:
        start: datetime = row["start"]
        local = dt_util.as_local(start)
        # The hour ending at local midnight.
        assert (local.hour, local.minute, local.second) == (23, 0, 0)
        result.append((local.date(), round(row["state"], 2), round(row["sum"], 2)))
    return result


async def test_import_history(
    hass: HomeAssistant, aioclient_mock, recorder: MagicMock, today: date
) -> None:
    """Rebuilt daily balances are imported once, following pages, and the flag cleared."""
    aioclient_mock.get(
        TX_URL,
        params=tx_params(today, 180, continuation_key="page2"),
        json={"transactions": [tx(today - timedelta(days=10), "20.00", "DBIT")]},
    )
    aioclient_mock.get(
        TX_URL,
        params=tx_params(today, 180),
        json={
            "transactions": [
                tx(today - timedelta(days=1), "30.00", "DBIT"),
                tx(today - timedelta(days=3), "100.00", "CRDT"),
            ],
            "continuation_key": "page2",
        },
    )
    entry = add_entry(hass)

    await setup(hass, entry)

    rows = imported(recorder)
    assert [d for d, _, _ in rows] == [today - timedelta(days=n) for n in range(10, 0, -1)]
    assert [s for _, s, _ in rows] == [1164.56] * 7 + [1264.56, 1264.56, 1234.56]
    assert rows[0][2] == 0
    assert rows[-1][2] == 70.0
    assert "history_pending" not in entry.data


async def test_import_history_falls_back_to_89_days(
    hass: HomeAssistant, aioclient_mock, recorder: MagicMock, today: date
) -> None:
    """If the bank refuses 180 days, 89 days are requested instead."""
    aioclient_mock.get(
        TX_URL,
        params=tx_params(today, 180),
        status=400,
        json={"code": 400, "error": "WRONG_TRANSACTIONS_PERIOD", "message": "Too old"},
    )
    aioclient_mock.get(
        TX_URL,
        params=tx_params(today, 89),
        json={
            "transactions": [
                tx(today - timedelta(days=2), "5.00", "CRDT"),
                tx(today - timedelta(days=1), "10.00", "CRDT"),
            ]
        },
    )
    entry = add_entry(hass)

    await setup(hass, entry)

    assert [s for _, s, _ in imported(recorder)] == [1224.56, 1234.56]


async def test_import_history_skips_recorded_hours(
    hass: HomeAssistant, aioclient_mock, recorder: MagicMock, today: date
) -> None:
    """Days already recorded are kept; imported sums line up with them."""
    aioclient_mock.get(
        TX_URL,
        params=tx_params(today, 180),
        json={"transactions": [tx(today - timedelta(days=5), "50.00", "CRDT")]},
    )
    # Home Assistant already recorded the balance since 2 days ago.
    existing = dt_util.start_of_local_day(today - timedelta(days=2))
    recorder.first.return_value = {
        "start": existing.timestamp(),
        "state": 1234.56,
        "sum": 10.0,
    }
    entry = add_entry(hass)

    await setup(hass, entry)

    rows = imported(recorder)
    assert [d for d, _, _ in rows] == [today - timedelta(days=n) for n in (5, 4, 3)]
    assert all(s == 1234.56 and total == 10.0 for _, s, total in rows)


async def test_import_history_disabled(
    hass: HomeAssistant, aioclient_mock, recorder: MagicMock, today: date
) -> None:
    """With the option off, no transactions are fetched and the flag is cleared."""
    entry = add_entry(hass, import_history=False)

    await setup(hass, entry)

    assert not any("transactions" in str(call[1]) for call in aioclient_mock.mock_calls)
    recorder.import_statistics.assert_not_called()
    assert "history_pending" not in entry.data


async def test_no_import_without_authorization(
    hass: HomeAssistant, aioclient_mock, recorder: MagicMock, today: date
) -> None:
    """A normal restart (no new authorization) doesn't fetch history."""
    entry = MockConfigEntry(domain="ha_banking", unique_id="h1", data=entry_data())
    entry.add_to_hass(hass)

    await setup(hass, entry)

    recorder.import_statistics.assert_not_called()
    assert not any("transactions" in str(call[1]) for call in aioclient_mock.mock_calls)


async def test_rate_limited_history_is_not_retried(
    hass: HomeAssistant, aioclient_mock, recorder: MagicMock, today: date
) -> None:
    """A rate limit stops the import; it won't be retried on every restart."""
    aioclient_mock.get(
        TX_URL,
        params=tx_params(today, 180),
        status=429,
        json={"code": 429, "error": "ASPSP_RATE_LIMIT_EXCEEDED", "message": "limit"},
    )
    entry = add_entry(hass)

    await setup(hass, entry)

    recorder.import_statistics.assert_not_called()
    assert "history_pending" not in entry.data
