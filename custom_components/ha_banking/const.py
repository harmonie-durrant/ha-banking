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

# Options
CONF_UPDATE_INTERVAL: Final = "update_interval"  # hours
CONF_BALANCE_TYPE: Final = "balance_type"
BALANCE_TYPE_AUTO: Final = "auto"

# Banks commonly allow ~4 unattended (background) requests per day under PSD2.
DEFAULT_UPDATE_HOURS: Final = 6
MIN_UPDATE_HOURS: Final = 1
MAX_UPDATE_HOURS: Final = 24

# Order used to pick the "main" balance automatically (or as fallback when the
# chosen type is not returned by the bank).
BALANCE_TYPE_PRIORITY: Final = ("CLBD", "ITBD", "XPCD", "ITAV", "CLAV", "OPBD", "OPAV")

# A cached result is reused (restarts, reloads) while younger than the update
# interval minus this margin, so scheduler jitter never skips a planned refresh.
CACHE_MARGIN: Final = timedelta(minutes=10)

# Ask the user to re-authorize this long before bank access expires.
REAUTH_BEFORE_EXPIRY: Final = timedelta(days=7)

STORAGE_VERSION: Final = 1
