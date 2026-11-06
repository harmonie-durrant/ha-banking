"""Constants for the HA Banking integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "ha_banking"

API_BASE_URL: Final = "https://api.enablebanking.com"

CONF_APPLICATION_ID: Final = "application_id"
CONF_PRIVATE_KEY: Final = "private_key"
CONF_REDIRECT_URL: Final = "redirect_url"
CONF_COUNTRY: Final = "country"
CONF_ASPSP: Final = "aspsp"
CONF_SESSION_ID: Final = "session_id"
CONF_ACCOUNTS: Final = "accounts"
CONF_VALID_UNTIL: Final = "valid_until"

DEFAULT_COUNTRY: Final = "FR"
DEFAULT_REDIRECT_URL: Final = "https://enablebanking.com/"

# Banks commonly allow ~4 unattended (background) requests per day under PSD2.
UPDATE_INTERVAL: Final = timedelta(hours=6)

# Preferred order when picking the "main" balance of an account.
BALANCE_TYPE_PRIORITY: Final = ("CLBD", "ITBD", "XPCD", "ITAV", "CLAV", "OPBD", "OPAV")
