"""Config flow for the HA Banking integration."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlparse
import uuid

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import EnableBankingAuthError, EnableBankingClient, EnableBankingError
from .const import (
    CONF_ACCOUNTS,
    CONF_APPLICATION_ID,
    CONF_ASPSP,
    CONF_COUNTRY,
    CONF_PRIVATE_KEY,
    CONF_REDIRECT_URL,
    CONF_SESSION_ID,
    CONF_VALID_UNTIL,
    DEFAULT_COUNTRY,
    DEFAULT_REDIRECT_URL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CONF_CALLBACK_URL = "callback_url"
CONTROL_PANEL_URL = "https://enablebanking.com/cp/applications"


class HaBankingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HA Banking."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._data: dict[str, Any] = {}
        self._client: EnableBankingClient | None = None
        self._aspsps: dict[str, dict[str, Any]] = {}
        self._auth_url: str | None = None
        self._state: str = uuid.uuid4().hex

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for Enable Banking application credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_COUNTRY] = user_input[CONF_COUNTRY].strip().upper()
            client = EnableBankingClient(
                async_get_clientsession(self.hass),
                user_input[CONF_APPLICATION_ID].strip(),
                user_input[CONF_PRIVATE_KEY].strip(),
            )
            try:
                aspsps = await client.get_aspsps(user_input[CONF_COUNTRY])
            except EnableBankingAuthError:
                errors["base"] = "invalid_auth"
            except EnableBankingError:
                _LOGGER.exception("Error listing banks")
                errors["base"] = "cannot_connect"
            else:
                if not aspsps:
                    errors["base"] = "no_banks"
                else:
                    self._client = client
                    self._data = {
                        CONF_APPLICATION_ID: user_input[CONF_APPLICATION_ID].strip(),
                        CONF_PRIVATE_KEY: user_input[CONF_PRIVATE_KEY].strip(),
                        CONF_REDIRECT_URL: user_input[CONF_REDIRECT_URL].strip(),
                        CONF_COUNTRY: user_input[CONF_COUNTRY],
                    }
                    self._aspsps = {a["name"]: a for a in aspsps}
                    return await self.async_step_bank()

        schema = vol.Schema(
            {
                vol.Required(CONF_APPLICATION_ID): str,
                vol.Required(CONF_PRIVATE_KEY): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
                vol.Required(CONF_REDIRECT_URL, default=DEFAULT_REDIRECT_URL): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL)
                ),
                vol.Required(CONF_COUNTRY, default=DEFAULT_COUNTRY): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            description_placeholders={"control_panel_url": CONTROL_PANEL_URL},
            errors=errors,
        )

    async def async_step_bank(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick their bank, then start authorization."""
        assert self._client is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            aspsp = self._aspsps[user_input[CONF_ASPSP]]
            try:
                auth = await self._client.start_auth(
                    aspsp["name"],
                    self._data[CONF_COUNTRY],
                    self._data[CONF_REDIRECT_URL],
                    self._state,
                    aspsp.get("maximum_consent_validity"),
                )
            except EnableBankingError as err:
                _LOGGER.error("Error starting authorization: %s", err)
                errors["base"] = "auth_start_failed"
            else:
                self._data[CONF_ASPSP] = aspsp["name"]
                self._auth_url = auth["url"]
                return await self.async_step_authorize()

        return self.async_show_form(
            step_id="bank",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ASPSP): SelectSelector(
                        SelectSelectorConfig(
                            options=sorted(self._aspsps),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_authorize(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the bank link and accept the URL the user was redirected to."""
        assert self._client is not None and self._auth_url is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            query = parse_qs(urlparse(user_input[CONF_CALLBACK_URL].strip()).query)
            code = query.get("code", [None])[0]
            state = query.get("state", [None])[0]
            if query.get("error"):
                errors["base"] = "bank_denied"
            elif not code:
                errors["base"] = "no_code"
            elif state != self._state:
                errors["base"] = "state_mismatch"
            else:
                try:
                    session = await self._client.create_session(code)
                except EnableBankingError as err:
                    _LOGGER.error("Error creating session: %s", err)
                    errors["base"] = "session_failed"
                else:
                    return await self._async_create_from_session(session)

        return self.async_show_form(
            step_id="authorize",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CALLBACK_URL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.URL)
                    )
                }
            ),
            description_placeholders={
                "auth_url": self._auth_url,
                "bank": self._data[CONF_ASPSP],
            },
            errors=errors,
        )

    async def _async_create_from_session(
        self, session: dict[str, Any]
    ) -> ConfigFlowResult:
        accounts = [
            {
                "uid": acc["uid"],
                "id": acc.get("identification_hash") or acc["uid"],
                "name": acc.get("name") or acc.get("product"),
                "iban": (acc.get("account_id") or {}).get("iban"),
                "currency": acc.get("currency"),
            }
            for acc in session.get("accounts", [])
            if acc.get("uid")
        ]
        if not accounts:
            return self.async_abort(reason="no_accounts")

        await self.async_set_unique_id(
            "|".join(sorted(acc["id"] for acc in accounts))
        )
        self._abort_if_unique_id_configured()

        self._data[CONF_SESSION_ID] = session["session_id"]
        self._data[CONF_ACCOUNTS] = accounts
        self._data[CONF_VALID_UNTIL] = (session.get("access") or {}).get(
            "valid_until"
        )
        return self.async_create_entry(title=self._data[CONF_ASPSP], data=self._data)
