"""Minimal async client for the Enable Banking API."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
import time
from typing import Any

from aiohttp import ClientError, ClientSession
import jwt

from .const import API_BASE_URL

JWT_LIFETIME = 3600


class EnableBankingError(Exception):
    """Generic Enable Banking API error."""


class EnableBankingAuthError(EnableBankingError):
    """Application credentials (application ID / private key) rejected."""


class EnableBankingSessionError(EnableBankingError):
    """Bank session expired, closed or revoked: the user must authorize again."""


class EnableBankingRateLimitError(EnableBankingError):
    """The bank (ASPSP) rate limit was exceeded."""


SESSION_ERROR_CODES = frozenset(
    {
        "CLOSED_SESSION",
        "EXPIRED_SESSION",
        "REVOKED_SESSION",
        "SESSION_DOES_NOT_EXIST",
        "WRONG_SESSION_STATUS",
    }
)


def _error_from_response(status: int, text: str) -> EnableBankingError:
    """Map an HTTP error response to the matching exception."""
    code: str | None = None
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        code = body["error"]
    message = f"{status} {code or ''}: {text}"
    if status == 429 or code == "ASPSP_RATE_LIMIT_EXCEEDED":
        return EnableBankingRateLimitError(message)
    if code in SESSION_ERROR_CODES:
        return EnableBankingSessionError(message)
    if status in (401, 403):
        return EnableBankingAuthError(message)
    return EnableBankingError(message)


class EnableBankingClient:
    """Thin wrapper around the Enable Banking REST API."""

    def __init__(
        self, session: ClientSession, application_id: str, private_key: str
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._application_id = application_id
        self._private_key = private_key
        self._token: str | None = None
        self._token_exp = 0.0

    def _jwt(self) -> str:
        """Return a cached signed JWT, renewing it before expiry."""
        now = time.time()
        if self._token is None or now > self._token_exp - 60:
            iat = int(now)
            try:
                self._token = jwt.encode(
                    {
                        "iss": "enablebanking.com",
                        "aud": "api.enablebanking.com",
                        "iat": iat,
                        "exp": iat + JWT_LIFETIME,
                    },
                    self._private_key,
                    algorithm="RS256",
                    headers={"kid": self._application_id},
                )
            except (ValueError, TypeError, jwt.PyJWTError) as err:
                raise EnableBankingAuthError(f"Invalid private key: {err}") from err
            self._token_exp = iat + JWT_LIFETIME
        return self._token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._jwt()}"}
        try:
            async with self._session.request(
                method,
                f"{API_BASE_URL}{path}",
                headers=headers,
                params=params,
                json=json,
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise _error_from_response(resp.status, text)
                return await resp.json()
        except (ClientError, TimeoutError) as err:
            raise EnableBankingError(f"Connection error: {err}") from err

    async def get_aspsps(self, country: str) -> list[dict[str, Any]]:
        """List banks (ASPSPs) available in a country for personal users."""
        data = await self._request(
            "GET", "/aspsps", params={"country": country, "psu_type": "personal"}
        )
        return data.get("aspsps", [])

    async def start_auth(
        self,
        aspsp_name: str,
        country: str,
        redirect_url: str,
        state: str,
        max_validity: int | None,
    ) -> dict[str, Any]:
        """Start user authorization; returns a dict with the bank `url`."""
        validity = timedelta(days=90)
        if max_validity:
            # Stay slightly under the bank's maximum to avoid rejection.
            validity = min(
                timedelta(seconds=max_validity) - timedelta(hours=1),
                timedelta(days=180),
            )
        valid_until = datetime.now(UTC) + validity
        return await self._request(
            "POST",
            "/auth",
            json={
                "access": {"valid_until": valid_until.isoformat()},
                "aspsp": {"name": aspsp_name, "country": country},
                "state": state,
                "redirect_url": redirect_url,
                "psu_type": "personal",
            },
        )

    async def create_session(self, code: str) -> dict[str, Any]:
        """Exchange an authorization code for a session with accounts."""
        return await self._request("POST", "/sessions", json={"code": code})

    async def get_balances(self, account_uid: str) -> list[dict[str, Any]]:
        """Return the balances of an account."""
        data = await self._request("GET", f"/accounts/{account_uid}/balances")
        return data.get("balances", [])

    async def get_transactions(
        self, account_uid: str, date_from: date, max_pages: int
    ) -> list[dict[str, Any]]:
        """Return booked transactions since date_from, following all pages."""
        transactions: list[dict[str, Any]] = []
        params: dict[str, str] = {
            "date_from": date_from.isoformat(),
            "transaction_status": "BOOK",
        }
        for _ in range(max_pages):
            data = await self._request(
                "GET", f"/accounts/{account_uid}/transactions", params=params
            )
            transactions.extend(data.get("transactions") or [])
            if not (key := data.get("continuation_key")):
                break
            params = {**params, "continuation_key": key}
        return transactions
