"""Config flow for the HA Banking integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any
from urllib.parse import parse_qs, urlparse
import uuid

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .api import EnableBankingAuthError, EnableBankingClient, EnableBankingError
from .const import (
    BALANCE_TYPE_AUTO,
    BALANCE_TYPE_PRIORITY,
    CONF_ACCOUNTS,
    CONF_APPLICATION_ID,
    CONF_ASPSP,
    CONF_BALANCE_TYPE,
    CONF_COUNTRY,
    CONF_PRIVATE_KEY,
    CONF_REDIRECT_URL,
    CONF_SESSION_ID,
    CONF_UPDATE_INTERVAL,
    CONF_VALID_UNTIL,
    DEFAULT_COUNTRY,
    DEFAULT_REDIRECT_URL,
    DEFAULT_UPDATE_HOURS,
    DOMAIN,
    MAX_UPDATE_HOURS,
    MIN_UPDATE_HOURS,
)

_LOGGER = logging.getLogger(__name__)

CONF_CALLBACK_URL = "callback_url"
CONF_REAUTHORIZE = "reauthorize"
CONTROL_PANEL_URL = "https://enablebanking.com/cp/applications"


class HaBankingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HA Banking."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HaBankingOptionsFlow:
        """Return the options flow."""
        return HaBankingOptionsFlow()

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
            if await self._async_start_bank_auth(aspsp, errors):
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

    async def _async_start_bank_auth(
        self, aspsp: dict[str, Any], errors: dict[str, str]
    ) -> bool:
        """Start authorization at the bank; fill errors and return False on failure."""
        assert self._client is not None
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
            return False
        self._data[CONF_ASPSP] = aspsp["name"]
        self._auth_url = auth["url"]
        return True

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Bank access expired or is about to: authorize again."""
        self._data = dict(entry_data)
        self._client = EnableBankingClient(
            async_get_clientsession(self.hass),
            entry_data[CONF_APPLICATION_ID],
            entry_data[CONF_PRIVATE_KEY],
        )
        return await self.async_step_reauth_confirm()

    async def _async_reauthorize_same_bank(
        self, errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        """Start a new authorization with the entry's bank.

        Returns the next step, an abort, or None with errors filled in.
        """
        assert self._client is not None
        try:
            aspsps = await self._client.get_aspsps(self._data[CONF_COUNTRY])
        except EnableBankingAuthError:
            errors["base"] = "invalid_auth"
            return None
        except EnableBankingError:
            _LOGGER.exception("Error listing banks")
            errors["base"] = "cannot_connect"
            return None
        aspsp = next((a for a in aspsps if a["name"] == self._data[CONF_ASPSP]), None)
        if aspsp is None:
            return self.async_abort(reason="bank_not_found")
        if await self._async_start_bank_auth(aspsp, errors):
            return await self.async_step_authorize()
        return None

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm, then send the user to their bank again."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if result := await self._async_reauthorize_same_bank(errors):
                return result

        valid_until = dt_util.parse_datetime(self._data.get(CONF_VALID_UNTIL) or "")
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={
                "bank": self._data[CONF_ASPSP],
                "valid_until": (
                    dt_util.as_local(valid_until).strftime("%Y-%m-%d")
                    if valid_until
                    else "?"
                ),
            },
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change application credentials, optionally re-linking the bank."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            updates = {
                CONF_APPLICATION_ID: user_input[CONF_APPLICATION_ID].strip(),
                # Left empty: keep the current key (it is never shown back).
                CONF_PRIVATE_KEY: (user_input.get(CONF_PRIVATE_KEY) or "").strip()
                or entry.data[CONF_PRIVATE_KEY],
                CONF_REDIRECT_URL: user_input[CONF_REDIRECT_URL].strip(),
            }
            self._data = {**entry.data, **updates}
            self._client = EnableBankingClient(
                async_get_clientsession(self.hass),
                updates[CONF_APPLICATION_ID],
                updates[CONF_PRIVATE_KEY],
            )
            if user_input.get(CONF_REAUTHORIZE):
                if result := await self._async_reauthorize_same_bank(errors):
                    return result
            else:
                try:
                    await self._client.get_aspsps(self._data[CONF_COUNTRY])
                except EnableBankingAuthError:
                    errors["base"] = "invalid_auth"
                except EnableBankingError:
                    _LOGGER.exception("Error listing banks")
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates=updates
                    )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_APPLICATION_ID, default=entry.data[CONF_APPLICATION_ID]
                ): str,
                vol.Optional(CONF_PRIVATE_KEY): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
                vol.Required(
                    CONF_REDIRECT_URL, default=entry.data[CONF_REDIRECT_URL]
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                vol.Required(CONF_REAUTHORIZE, default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            description_placeholders={"bank": entry.data[CONF_ASPSP]},
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
        bank = self._data[CONF_ASPSP]
        raw_accounts: list[dict[str, Any]] = session.get("accounts") or []
        _LOGGER.info("%s returned %d account(s)", bank, len(raw_accounts))
        accounts: list[dict[str, Any]] = []
        skipped: list[str] = []
        for acc in raw_accounts:
            description = describe_account(acc)
            if not acc.get("uid"):
                # Enable Banking omits the uid when the bank doesn't allow
                # querying balances/transactions of that account.
                _LOGGER.warning(
                    "Skipping account that %s does not allow querying: %s",
                    bank,
                    description,
                )
                skipped.append(description)
                continue
            _LOGGER.info("Adding account: %s", description)
            accounts.append(
                {
                    "uid": acc["uid"],
                    "id": acc.get("identification_hash") or acc["uid"],
                    "name": acc.get("name") or acc.get("product"),
                    "iban": (acc.get("account_id") or {}).get("iban"),
                    "currency": acc.get("currency"),
                }
            )
        if not accounts:
            return self.async_abort(reason="no_accounts")

        session_data = {
            CONF_SESSION_ID: session["session_id"],
            CONF_ACCOUNTS: accounts,
            CONF_VALID_UNTIL: (session.get("access") or {}).get("valid_until"),
        }

        if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            entry = (
                self._get_reauth_entry()
                if self.source == SOURCE_REAUTH
                else self._get_reconfigure_entry()
            )
            old_ids = {acc["id"] for acc in entry.data[CONF_ACCOUNTS]}
            if not old_ids & {acc["id"] for acc in accounts}:
                return self.async_abort(reason="wrong_accounts")
            updates = {
                key: self._data[key]
                for key in (CONF_APPLICATION_ID, CONF_PRIVATE_KEY, CONF_REDIRECT_URL)
            }
            return self.async_update_reload_and_abort(
                entry, data_updates={**updates, **session_data}
            )

        await self.async_set_unique_id(
            "|".join(sorted(acc["id"] for acc in accounts))
        )
        self._abort_if_unique_id_configured()

        self._data.update(session_data)
        if skipped:
            return self.async_create_entry(
                title=bank,
                data=self._data,
                description="skipped_accounts",
                description_placeholders={
                    "accounts": "\n".join(f"- {s}" for s in skipped)
                },
            )
        return self.async_create_entry(title=bank, data=self._data)


class HaBankingOptionsFlow(OptionsFlowWithReload):
    """Refresh interval and main balance type."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            user_input[CONF_UPDATE_INTERVAL] = int(user_input[CONF_UPDATE_INTERVAL])
            return self.async_create_entry(data=user_input)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_HOURS
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_UPDATE_HOURS,
                        max=MAX_UPDATE_HOURS,
                        step=1,
                        unit_of_measurement="h",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_BALANCE_TYPE, default=BALANCE_TYPE_AUTO): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            BALANCE_TYPE_AUTO,
                            *(t.lower() for t in BALANCE_TYPE_PRIORITY),
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_BALANCE_TYPE,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, self.config_entry.options
            ),
        )


def describe_account(acc: dict[str, Any]) -> str:
    """Human-readable account summary for logs, with a masked identifier."""
    account_id = acc.get("account_id") or {}
    ident = account_id.get("iban") or (account_id.get("other") or {}).get(
        "identification"
    )
    masked = f"…{ident[-4:]}" if ident else "no identifier"
    name = acc.get("name") or acc.get("product") or "unnamed"
    return (
        f"{name} ({masked}, type={acc.get('cash_account_type')}, "
        f"product={acc.get('product')}, usage={acc.get('usage')}, "
        f"currency={acc.get('currency')})"
    )
