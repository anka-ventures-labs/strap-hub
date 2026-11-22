"""WHOOP v2 collections, request retries and grant revocation."""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx

from strap_hub.whoop_cloud.oauth import TokenSession, WhoopError, valid_user_id

API_BASE = "https://api.prod.whoop.com/developer/v2"
PROFILE_ENDPOINT = "/user/profile/basic"
COLLECTIONS = {
    "cloud_cycle": "/cycle",
    "cloud_recovery": "/recovery",
    "cloud_sleep": "/activity/sleep",
    "cloud_workout": "/activity/workout",
}
MAX_RETRIES = 3
MAX_RETRY_WAIT_SECONDS = 60


def http_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0), trust_env=False)


def _retry_delay(response: httpx.Response, retry: int) -> float:
    delay = float(2**retry)
    for header in ("Retry-After", "X-RateLimit-Reset"):
        value = response.headers.get(header)
        if value and value.isascii() and value.isdecimal():
            delay = max(delay, float(value))
    return delay


class WhoopClient:
    def __init__(self, session: TokenSession) -> None:
        self.session = session
        self.http = session.oauth.http
        self._verified_access_token: str | None = None

    def _request(
        self, method: str, endpoint: str, *, access_token: str | None = None, **kwargs,
    ) -> httpx.Response:
        refresh_allowed = access_token is None
        if access_token is None:
            access_token = self.session.access_token()
        refreshed = False
        retries = 0
        while True:
            if (
                endpoint != PROFILE_ENDPOINT and self._verified_access_token is not None
                and self._verified_access_token != access_token
            ):
                # Collection requests can rotate the token partway through a sync.
                self.verify_account(access_token=access_token)
            try:
                response = self.http.request(
                    method, API_BASE + endpoint, headers={"Authorization": f"Bearer {access_token}"}, **kwargs,
                )
            except httpx.RequestError:
                if method != "GET" or retries == MAX_RETRIES:
                    raise WhoopError("WHOOP API request failed in transit") from None
                time.sleep(2**retries)
                retries += 1
                continue
            if response.is_success:
                return response
            if response.status_code == 401 and refresh_allowed and not refreshed:
                access_token = self.session.access_token(refresh=True)
                refreshed = True
                continue
            if (response.status_code == 429 or 500 <= response.status_code < 600) and retries < MAX_RETRIES:
                delay = _retry_delay(response, retries)
                if delay > MAX_RETRY_WAIT_SECONDS:
                    raise WhoopError(f"WHOOP API requested a {delay:g}-second retry delay. Try again after it resets.")
                time.sleep(delay)
                retries += 1
                continue
            raise WhoopError(f"WHOOP API request returned HTTP {response.status_code}")

    def profile_user_id(self, *, access_token: str | None = None) -> int:
        response = self._request("GET", PROFILE_ENDPOINT, access_token=access_token)
        try:
            user_id = response.json()["user_id"]
            if valid_user_id(user_id):
                return user_id
        except (ValueError, TypeError, KeyError):
            pass
        raise WhoopError("WHOOP profile did not contain a valid account identifier")

    def verify_account(self, *, access_token: str | None = None) -> int:
        if self.session.tokens.user_id is None:
            raise WhoopError("Run whoop-auth to bind these credentials to a WHOOP account")
        user_id = self.profile_user_id(access_token=access_token)
        if user_id != self.session.tokens.user_id:
            raise WhoopError("WHOOP profile does not match the authorized account. Run whoop-auth.")
        self._verified_access_token = self.session.tokens.access_token
        return user_id

    def records(self, table: str, start: str, end: str) -> Iterator[dict]:
        params = {"limit": "25", "start": start, "end": end}
        seen: set[str] = set()
        while True:
            response = self._request("GET", COLLECTIONS[table], params=params)
            try:
                page = response.json()
                records = page["records"]
                next_token = page.get("next_token")
                if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
                    raise ValueError
                if next_token is not None and not isinstance(next_token, str):
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise WhoopError(f"WHOOP returned an invalid {table} page") from None
            yield from records
            if not next_token:
                return
            if next_token in seen:
                raise WhoopError(f"WHOOP repeated a {table} page cursor")
            seen.add(next_token)
            params["nextToken"] = next_token

    def revoke(self) -> None:
        self._request("DELETE", "/user/access")
        self.session.credentials.remove()
