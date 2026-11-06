"""Tests for the HA Banking config flow, coordinator and sensors."""

from datetime import timedelta
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

DOMAIN = "ha_banking"
API = "https://api.enablebanking.com"
BALANCES_URL = f"{API}/accounts/u1/balances"
BALANCES = {
    "balances": [
        {"balance_type": "XPCD", "balance_amount": {"amount": "99.00", "currency": "EUR"}},
        {
            "balance_type": "CLBD",
            "balance_amount": {"amount": "1234.56", "currency": "EUR"},
            "reference_date": "2026-11-05",
        },
    ]
}
SESSION = {
    "session_id": "s1",
    "access": {"valid_until": "2027-05-01T00:00:00+00:00"},
    "accounts": [
        {
            "uid": "u1",
            "identification_hash": "h1",
            "name": "Compte courant",
            "account_id": {"iban": "FR7612345678901234"},
            "currency": "EUR",
        }
    ],
}
BALANCE_ENTITY = "sensor.compte_courant_balance"
EXPIRY_ENTITY = "sensor.compte_courant_access_expires"


def make_pem() -> str:
    """Generate a throwaway RSA private key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


PEM = make_pem()


def entry_data(valid_until: str = "2027-05-01T00:00:00+00:00") -> dict[str, Any]:
    """Config entry data as created by the config flow."""
    return {
        "application_id": "app",
        "private_key": PEM,
        "redirect_url": "https://enablebanking.com/",
        "country": "FR",
        "aspsp": "Boursorama",
        "session_id": "s1",
        "accounts": [
            {
                "uid": "u1",
                "id": "h1",
                "name": "Compte courant",
                "iban": "FR7612345678901234",
                "currency": "EUR",
            }
        ],
        "valid_until": valid_until,
    }


def add_entry(hass: HomeAssistant, **kwargs: Any) -> MockConfigEntry:
    """Add a mock config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Boursorama", unique_id="h1", data=entry_data(**kwargs)
    )
    entry.add_to_hass(hass)
    return entry


def cache(entry: MockConfigEntry, age: timedelta, amount: str = "500.00") -> dict:
    """Storage content for cached balances fetched `age` ago."""
    return {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {
            "fetched_at": (dt_util.utcnow() - age).isoformat(),
            "accounts": {
                "h1": [
                    {
                        "balance_type": "CLBD",
                        "balance_amount": {"amount": amount, "currency": "EUR"},
                        "reference_date": "2026-11-01",
                    },
                    {
                        "balance_type": "XPCD",
                        "balance_amount": {"amount": "42.00", "currency": "EUR"},
                    },
                ]
            },
        },
    }


def balance_calls(aioclient_mock) -> int:
    """Number of balance requests sent to the bank."""
    return sum(1 for call in aioclient_mock.mock_calls if str(call[1]) == BALANCES_URL)


def mock_reauth_api(aioclient_mock, session: dict[str, Any]) -> None:
    """Mock the endpoints used by the re-authentication flow."""
    aioclient_mock.get(
        f"{API}/aspsps",
        json={"aspsps": [{"name": "Boursorama", "country": "FR", "maximum_consent_validity": 15552000}]},
    )
    aioclient_mock.post(f"{API}/auth", json={"url": "https://bank.example/auth"})
    aioclient_mock.post(f"{API}/sessions", json=session)


async def finish_reauth(hass: HomeAssistant, flow_id: str) -> dict[str, Any]:
    """Go through reauth_confirm and authorize with a valid callback URL."""
    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["step_id"] == "authorize", result
    state = hass.config_entries.flow._progress[flow_id]._state
    return await hass.config_entries.flow.async_configure(
        flow_id, {"callback_url": f"https://enablebanking.com/?state={state}&code=c"}
    )


async def test_full_flow(hass: HomeAssistant, aioclient_mock, hass_storage) -> None:
    """Set up from scratch and get a balance sensor."""
    aioclient_mock.get(
        f"{API}/aspsps",
        json={"aspsps": [{"name": "Boursorama", "country": "FR", "maximum_consent_validity": 15552000}]},
    )
    aioclient_mock.post(f"{API}/auth", json={"url": "https://bank.example/auth", "authorization_id": "a"})
    aioclient_mock.post(f"{API}/sessions", json=SESSION)
    aioclient_mock.get(BALANCES_URL, json=BALANCES)

    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "user"
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"],
        {"application_id": "app", "private_key": PEM, "redirect_url": "https://enablebanking.com/", "country": "fr"},
    )
    assert r["step_id"] == "bank", r
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"aspsp": "Boursorama"})
    assert r["step_id"] == "authorize", r
    flow = hass.config_entries.flow._progress[r["flow_id"]]
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {"callback_url": "https://enablebanking.com/?state=bad&code=c"}
    )
    assert r["errors"] == {"base": "state_mismatch"}
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {"callback_url": f"https://enablebanking.com/?state={flow._state}&code=c"}
    )
    assert r["step_id"] == "accounts", r
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"selected_accounts": ["h1"]})
    assert r["type"] is FlowResultType.CREATE_ENTRY, r
    assert r["title"] == "Boursorama"
    assert r["options"] == {"selected_accounts": ["h1"]}
    await hass.async_block_till_done()
    # Balances fetched for the account list are reused: one bank call in total.
    assert balance_calls(aioclient_mock) == 1

    _, _, _, headers = aioclient_mock.mock_calls[0]
    tok = headers["Authorization"].split()[1]
    assert jwt.get_unverified_header(tok)["kid"] == "app"
    assert jwt.decode(tok, options={"verify_signature": False})["aud"] == "api.enablebanking.com"

    state = hass.states.get(BALANCE_ENTITY)
    assert state.state == "1234.56"
    assert state.attributes["unit_of_measurement"] == "EUR"
    assert state.attributes["balance_type"] == "CLBD"
    assert hass.states.get(EXPIRY_ENTITY).state == "2027-05-01T00:00:00+00:00"

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert stored["accounts"]["h1"] == BALANCES["balances"]


async def test_bad_key(hass: HomeAssistant) -> None:
    """An invalid private key is reported as invalid_auth."""
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {"application_id": "app", "private_key": "nope", "redirect_url": "https://x/", "country": "FR"}
    )
    assert r["errors"] == {"base": "invalid_auth"}


async def test_restart_uses_fresh_cache(hass: HomeAssistant, aioclient_mock, hass_storage) -> None:
    """A restart within the refresh interval does not call the bank."""
    entry = add_entry(hass)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = cache(entry, timedelta(hours=1))
    aioclient_mock.get(BALANCES_URL, json=BALANCES)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert balance_calls(aioclient_mock) == 0
    assert hass.states.get(BALANCE_ENTITY).state == "500.00"


async def test_restart_with_stale_cache_fetches(hass: HomeAssistant, aioclient_mock, hass_storage) -> None:
    """An old cache is refreshed from the bank."""
    entry = add_entry(hass)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = cache(entry, timedelta(hours=7))
    aioclient_mock.get(BALANCES_URL, json=BALANCES)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert balance_calls(aioclient_mock) == 1
    assert hass.states.get(BALANCE_ENTITY).state == "1234.56"


async def test_rate_limit_keeps_last_balance(
    hass: HomeAssistant, aioclient_mock, hass_storage, caplog: pytest.LogCaptureFixture
) -> None:
    """When the bank rate-limits us, the last known balance stays available."""
    entry = add_entry(hass)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = cache(entry, timedelta(hours=7))
    aioclient_mock.get(
        BALANCES_URL,
        status=429,
        json={"code": 429, "error": "ASPSP_RATE_LIMIT_EXCEEDED", "message": "ASPSP Rate limit exceeded"},
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(BALANCE_ENTITY).state == "500.00"
    assert "rate limit" in caplog.text


async def test_rate_limit_without_cache_retries(hass: HomeAssistant, aioclient_mock) -> None:
    """Without any cached balance, a rate limit makes setup retry later."""
    entry = add_entry(hass)
    aioclient_mock.get(BALANCES_URL, status=429, text="Too Many Requests")

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_expired_session_starts_reauth(hass: HomeAssistant, aioclient_mock) -> None:
    """An expired session error triggers the re-authentication flow."""
    entry = add_entry(hass)
    aioclient_mock.get(
        BALANCES_URL,
        status=401,
        json={"code": 401, "error": "EXPIRED_SESSION", "message": "Session is expired"},
    )

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [config_entries.SOURCE_REAUTH]


async def test_expiring_soon_prompts_reauth_but_keeps_working(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Within a week of expiry, the user is prompted while data keeps flowing."""
    soon = (dt_util.utcnow() + timedelta(days=3)).isoformat()
    entry = add_entry(hass, valid_until=soon)
    aioclient_mock.get(BALANCES_URL, json=BALANCES)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(BALANCE_ENTITY).state == "1234.56"
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [config_entries.SOURCE_REAUTH]


async def test_reauth_flow(hass: HomeAssistant, aioclient_mock) -> None:
    """Re-authorizing updates the session and keeps the entry."""
    entry = add_entry(hass, valid_until="2020-01-01T00:00:00+00:00")
    new_session = {
        **SESSION,
        "session_id": "s2",
        "access": {"valid_until": "2027-06-01T00:00:00+00:00"},
        "accounts": [{**SESSION["accounts"][0], "uid": "u2"}],
    }
    mock_reauth_api(aioclient_mock, new_session)
    aioclient_mock.get(f"{API}/accounts/u2/balances", json=BALANCES)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await finish_reauth(hass, result["flow_id"])
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["session_id"] == "s2"
    assert entry.data["accounts"][0]["uid"] == "u2"
    assert entry.data["valid_until"] == "2027-06-01T00:00:00+00:00"
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(BALANCE_ENTITY).state == "1234.56"


async def test_reauth_wrong_accounts(hass: HomeAssistant, aioclient_mock) -> None:
    """Authorizing a different customer's accounts is refused."""
    entry = add_entry(hass)
    other = {**SESSION, "accounts": [{**SESSION["accounts"][0], "identification_hash": "other"}]}
    mock_reauth_api(aioclient_mock, other)

    result = await entry.start_reauth_flow(hass)
    result = await finish_reauth(hass, result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_accounts"
    assert entry.data["session_id"] == "s1"


async def test_reconfigure_credentials_only(hass: HomeAssistant, aioclient_mock) -> None:
    """Changing the application ID keeps the key when left empty."""
    entry = add_entry(hass)
    aioclient_mock.get(f"{API}/aspsps", json={"aspsps": [{"name": "Boursorama"}]})

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"application_id": "app2", "redirect_url": "https://example.com/", "reauthorize": False},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["application_id"] == "app2"
    assert entry.data["private_key"] == PEM
    assert entry.data["redirect_url"] == "https://example.com/"
    assert entry.data["session_id"] == "s1"


async def test_reconfigure_invalid_key(hass: HomeAssistant) -> None:
    """A broken new key is rejected and nothing is saved."""
    entry = add_entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"application_id": "app", "private_key": "nope", "redirect_url": "https://x/", "reauthorize": False},
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data["private_key"] == PEM


async def test_reconfigure_reauthorize_adds_account(hass: HomeAssistant, aioclient_mock) -> None:
    """Re-authorizing from reconfigure refreshes the account list."""
    entry = add_entry(hass)
    lep = {
        "uid": "u3",
        "identification_hash": "h3",
        "name": "LEP",
        "account_id": {"iban": "FR7600000000000003"},
        "cash_account_type": "SVGS",
        "currency": "EUR",
    }
    new_session = {**SESSION, "session_id": "s3", "accounts": [*SESSION["accounts"], lep]}
    mock_reauth_api(aioclient_mock, new_session)
    aioclient_mock.get(BALANCES_URL, json=BALANCES)
    aioclient_mock.get(f"{API}/accounts/u3/balances", json=BALANCES)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"application_id": "app", "redirect_url": "https://enablebanking.com/", "reauthorize": True},
    )
    assert result["step_id"] == "authorize", result
    state = hass.config_entries.flow._progress[result["flow_id"]]._state
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": f"https://enablebanking.com/?state={state}&code=c"}
    )
    assert result["step_id"] == "accounts", result
    schema = result["data_schema"].schema
    default = next(k for k in schema if k == "selected_accounts").default()
    assert default == ["h1"]  # current selection, new account unticked
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"selected_accounts": ["h1", "h3"]}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.options["selected_accounts"] == ["h1", "h3"]
    assert entry.data["session_id"] == "s3"
    assert [a["id"] for a in entry.data["accounts"]] == ["h1", "h3"]
    assert hass.states.get("sensor.lep_balance").state == "1234.56"


async def test_skipped_accounts_are_reported(
    hass: HomeAssistant, aioclient_mock, caplog: pytest.LogCaptureFixture
) -> None:
    """Accounts without uid are logged and listed on the success screen."""
    lep = {
        "identification_hash": "h3",
        "name": "LEP",
        "account_id": {"iban": "FR7600000000000003"},
        "cash_account_type": "SVGS",
        "currency": "EUR",
    }
    aioclient_mock.get(f"{API}/aspsps", json={"aspsps": [{"name": "Boursorama", "country": "FR"}]})
    aioclient_mock.post(f"{API}/auth", json={"url": "https://bank.example/auth"})
    aioclient_mock.post(f"{API}/sessions", json={**SESSION, "accounts": [*SESSION["accounts"], lep]})
    aioclient_mock.get(BALANCES_URL, json=BALANCES)

    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"],
        {"application_id": "app", "private_key": PEM, "redirect_url": "https://enablebanking.com/", "country": "FR"},
    )
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"aspsp": "Boursorama"})
    state = hass.config_entries.flow._progress[r["flow_id"]]._state
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {"callback_url": f"https://enablebanking.com/?state={state}&code=c"}
    )
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"selected_accounts": ["h1"]})
    await hass.async_block_till_done()

    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["description"] == "skipped_accounts"
    assert "LEP (…0003, type=SVGS" in r["description_placeholders"]["accounts"]
    assert "does not allow querying: LEP" in caplog.text
    assert "FR7600000000000003" not in caplog.text
    assert [a["id"] for a in r["data"]["accounts"]] == ["h1"]


async def test_old_cache_format_is_ignored(hass: HomeAssistant, aioclient_mock, hass_storage) -> None:
    """A cache from v0.2 (picked balances only) triggers one bank fetch."""
    entry = add_entry(hass)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {
            "fetched_at": dt_util.utcnow().isoformat(),
            "balances": {"h1": {"amount": "1.00", "currency": "EUR", "balance_type": "CLBD", "reference_date": None}},
        },
    }
    aioclient_mock.get(BALANCES_URL, json=BALANCES)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert balance_calls(aioclient_mock) == 1
    assert hass.states.get(BALANCE_ENTITY).state == "1234.56"


async def test_options_change_balance_type_without_bank_call(
    hass: HomeAssistant, aioclient_mock, hass_storage
) -> None:
    """Choosing another balance type applies from cache, without calling the bank."""
    entry = add_entry(hass)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = cache(entry, timedelta(hours=1))
    aioclient_mock.get(BALANCES_URL, json=BALANCES)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(BALANCE_ENTITY)
    assert state.state == "500.00"
    assert state.attributes["available_balance_types"] == ["CLBD", "XPCD"]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"selected_accounts": ["h1"], "update_interval": 12, "balance_type": "xpcd", "import_history": True},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "selected_accounts": ["h1"],
        "update_interval": 12,
        "balance_type": "xpcd",
        "import_history": True,
    }
    assert entry.runtime_data.update_interval == timedelta(hours=12)
    assert hass.states.get(BALANCE_ENTITY).state == "42.00"
    assert balance_calls(aioclient_mock) == 0


async def test_preferred_type_missing_falls_back(hass: HomeAssistant, aioclient_mock) -> None:
    """If the bank doesn't return the chosen type, the automatic choice is used."""
    entry = add_entry(hass)
    hass.config_entries.async_update_entry(entry, options={"update_interval": 6, "balance_type": "itav"})
    aioclient_mock.get(BALANCES_URL, json=BALANCES)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(BALANCE_ENTITY).state == "1234.56"


async def test_refresh_button_bypasses_cache(hass: HomeAssistant, aioclient_mock, hass_storage) -> None:
    """Pressing Refresh calls the bank even with a fresh cache."""
    entry = add_entry(hass)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = cache(entry, timedelta(minutes=5))
    aioclient_mock.get(BALANCES_URL, json=BALANCES)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert balance_calls(aioclient_mock) == 0

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.compte_courant_refresh"}, blocking=True
    )
    await hass.async_block_till_done()

    assert balance_calls(aioclient_mock) == 1
    assert hass.states.get(BALANCE_ENTITY).state == "1234.56"

    # A normal refresh right after still uses the new cache.
    await entry.runtime_data.async_refresh()
    assert balance_calls(aioclient_mock) == 1


CARD = {
    "uid": "u2",
    "identification_hash": "h2",
    "name": "Carte Visa",
    "account_id": {"other": {"identification": "XXXX6634"}},
    "cash_account_type": "CARD",
    "currency": "XXX",
}
CARD_URL = f"{API}/accounts/u2/balances"


def option_labels(result: dict[str, Any]) -> dict[str, str]:
    """value -> label of the account multi-select."""
    selector = result["data_schema"].schema["selected_accounts"]
    return {o["value"]: o["label"] for o in selector.config["options"]}


async def test_choose_accounts_with_balances(hass: HomeAssistant, aioclient_mock) -> None:
    """Balances are shown; a card at 0 is flagged and unticked by default."""
    aioclient_mock.get(f"{API}/aspsps", json={"aspsps": [{"name": "Boursorama", "country": "FR"}]})
    aioclient_mock.post(f"{API}/auth", json={"url": "https://bank.example/auth"})
    aioclient_mock.post(f"{API}/sessions", json={**SESSION, "accounts": [*SESSION["accounts"], CARD]})
    aioclient_mock.get(BALANCES_URL, json=BALANCES)
    aioclient_mock.get(
        CARD_URL,
        json={"balances": [{"balance_type": "OTHR", "balance_amount": {"amount": "0", "currency": "EUR"}}]},
    )

    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"],
        {"application_id": "app", "private_key": PEM, "redirect_url": "https://enablebanking.com/", "country": "FR"},
    )
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"aspsp": "Boursorama"})
    state = hass.config_entries.flow._progress[r["flow_id"]]._state
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {"callback_url": f"https://enablebanking.com/?state={state}&code=c"}
    )

    assert r["step_id"] == "accounts"
    assert option_labels(r) == {
        "h1": "Compte courant (…1234) · 1234.56 EUR",
        "h2": "Carte Visa (…6634) · 0 EUR ⚠",
    }
    default = next(k for k in r["data_schema"].schema if k == "selected_accounts").default()
    assert default == ["h1"]

    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"selected_accounts": []})
    assert r["errors"] == {"base": "no_accounts_selected"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"selected_accounts": ["h1"]})
    await hass.async_block_till_done()

    assert r["type"] is FlowResultType.CREATE_ENTRY
    card = next(a for a in r["data"]["accounts"] if a["id"] == "h2")
    assert card["currency"] is None  # "XXX" = no currency
    assert card["type"] == "CARD"
    assert hass.states.get(BALANCE_ENTITY) is not None
    assert hass.states.get("sensor.carte_visa_balance") is None
    # The card wasn't selected: only the current account is refreshed later.
    assert sum(1 for c in aioclient_mock.mock_calls if str(c[1]) == CARD_URL) == 1


def add_two_accounts(hass: HomeAssistant, selected: list[str] | None) -> MockConfigEntry:
    """Entry with a current account and a card."""
    data = entry_data()
    data["accounts"].append(
        {"uid": "u2", "id": "h2", "name": "Carte Visa", "iban": None, "mask": "6634", "type": "CARD", "currency": None}
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Boursorama",
        unique_id="h1|h2",
        data=data,
        options={} if selected is None else {"selected_accounts": selected},
    )
    entry.add_to_hass(hass)
    return entry


async def test_untracking_account_removes_its_device(hass: HomeAssistant, aioclient_mock) -> None:
    """Unticking an account in the options removes its device and entities."""
    entry = add_two_accounts(hass, selected=None)  # entry from before 0.4: all tracked
    aioclient_mock.get(BALANCES_URL, json=BALANCES)
    aioclient_mock.get(
        CARD_URL,
        json={"balances": [{"balance_type": "OTHR", "balance_amount": {"amount": "0", "currency": "EUR"}}]},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.carte_visa_balance").state == "0"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert option_labels(result)["h2"] == "Carte Visa (…6634) · 0 EUR ⚠"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"selected_accounts": ["h1"], "update_interval": 6, "balance_type": "auto", "import_history": True},
    )
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    assert dev_reg.async_get_device_by_identifier((DOMAIN, "h2"), entry.entry_id) is None
    assert dev_reg.async_get_device_by_identifier((DOMAIN, "h1"), entry.entry_id) is not None
    assert hass.states.get("sensor.carte_visa_balance") is None
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "h2_balance") is None


async def test_reauth_keeps_account_selection(hass: HomeAssistant, aioclient_mock) -> None:
    """Renewing access doesn't ask again and keeps untracked accounts untracked."""
    entry = add_two_accounts(hass, selected=["h1"])
    mock_reauth_api(
        aioclient_mock, {**SESSION, "session_id": "s9", "accounts": [*SESSION["accounts"], CARD]}
    )
    aioclient_mock.get(BALANCES_URL, json=BALANCES)

    result = await entry.start_reauth_flow(hass)
    result = await finish_reauth(hass, result["flow_id"])
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert entry.options["selected_accounts"] == ["h1"]
    assert entry.data["session_id"] == "s9"
    assert hass.states.get("sensor.carte_visa_balance") is None
