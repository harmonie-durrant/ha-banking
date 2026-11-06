from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
import jwt

API = "https://api.enablebanking.com"

def pem():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()

async def test_full_flow(hass, aioclient_mock):
    aioclient_mock.get(f"{API}/aspsps", json={"aspsps": [{"name": "Boursorama", "country": "FR", "maximum_consent_validity": 15552000}]})
    aioclient_mock.post(f"{API}/auth", json={"url": "https://bank.example/auth", "authorization_id": "a"})
    aioclient_mock.post(f"{API}/sessions", json={"session_id": "s1", "access": {"valid_until": "2027-05-01T00:00:00+00:00"},
        "accounts": [{"uid": "u1", "identification_hash": "h1", "name": "Compte courant", "account_id": {"iban": "FR7612345678901234"}, "currency": "EUR"}]})
    aioclient_mock.get(f"{API}/accounts/u1/balances", json={"balances": [
        {"balance_type": "XPCD", "balance_amount": {"amount": "99.00", "currency": "EUR"}},
        {"balance_type": "CLBD", "balance_amount": {"amount": "1234.56", "currency": "EUR"}, "reference_date": "2026-11-05"}]})

    r = await hass.config_entries.flow.async_init("ha_banking", context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "user"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"application_id": "app", "private_key": pem(), "redirect_url": "https://enablebanking.com/", "country": "fr"})
    assert r["step_id"] == "bank", r
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"aspsp": "Boursorama"})
    assert r["step_id"] == "authorize", r
    flow = hass.config_entries.flow._progress[r["flow_id"]]
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"callback_url": "https://enablebanking.com/?state=bad&code=c"})
    assert r["errors"] == {"base": "state_mismatch"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"callback_url": f"https://enablebanking.com/?state={flow._state}&code=c"})
    assert r["type"] is FlowResultType.CREATE_ENTRY, r
    assert r["title"] == "Boursorama"
    await hass.async_block_till_done()

    # JWT header/claims
    _, url, body, headers = aioclient_mock.mock_calls[0]
    tok = headers["Authorization"].split()[1]
    assert jwt.get_unverified_header(tok)["kid"] == "app"
    assert jwt.decode(tok, options={"verify_signature": False})["aud"] == "api.enablebanking.com"

    states = hass.states.async_all("sensor")
    assert len(states) == 1, states
    s = states[0]
    print(s.entity_id, s.state, s.attributes)
    assert s.state == "1234.56"
    assert s.attributes["unit_of_measurement"] == "EUR"
    assert s.attributes["balance_type"] == "CLBD"

async def test_bad_key(hass):
    r = await hass.config_entries.flow.async_init("ha_banking", context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"application_id": "app", "private_key": "nope", "redirect_url": "https://x/", "country": "FR"})
    assert r["errors"] == {"base": "invalid_auth"}
