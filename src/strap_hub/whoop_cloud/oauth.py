"""WHOOP authorization and single-writer storage for rotating credentials."""

from __future__ import annotations

import fcntl
import json
import math
import os
import secrets
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hmac import compare_digest
from pathlib import Path
from urllib.parse import SplitResult, parse_qs, urlsplit

import httpx
from oauthlib.oauth2 import WebApplicationClient

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
DEFAULT_REDIRECT_URI = "http://localhost:8771/whoop/callback"
SCOPES = ("read:recovery", "read:cycles", "read:workout", "read:sleep", "read:profile",
          "read:body_measurement", "offline")


class WhoopError(ValueError):
    """A WHOOP failure safe to print without provider response details."""


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str = DEFAULT_REDIRECT_URI

    @classmethod
    def from_env(cls) -> OAuthConfig:
        client_id = os.environ.get("WHOOP_CLIENT_ID")
        client_secret = os.environ.get("WHOOP_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise WhoopError("Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET")
        redirect_uri = os.environ.get("WHOOP_REDIRECT_URI") or DEFAULT_REDIRECT_URI
        parse_redirect_uri(redirect_uri)
        return cls(client_id, client_secret, redirect_uri)


def parse_redirect_uri(uri: str) -> SplitResult:
    try:
        redirect = urlsplit(uri)
        port = redirect.port
        if port == 0:
            raise ValueError
        if (
            redirect.scheme in {"http", "https", "whoop"} and redirect.hostname
            and not (redirect.username or redirect.password or redirect.query or redirect.fragment)
            and (redirect.scheme != "http" or redirect.hostname in {"localhost", "127.0.0.1"})
        ):
            return redirect
    except ValueError:
        pass
    raise WhoopError("WHOOP_REDIRECT_URI must be a registered HTTPS, whoop://, or HTTP loopback URL")


def token_path(db_path: str | Path | None) -> Path:
    custom = os.environ.get("HUB_WHOOP_TOKEN_PATH")
    db = Path(db_path).expanduser().resolve() if db_path else None
    if custom:
        path = Path(custom).expanduser()
        path = path.parent.resolve() / path.name
    elif db is not None:
        path = db.with_name(db.name + ".whoop-token.json")
    else:
        raise WhoopError("Set HUB_DB_PATH or HUB_WHOOP_TOKEN_PATH")

    if db is not None:
        store_files = {db.with_name(db.name + suffix) for suffix in ("", "-wal", "-shm", "-journal")}
        if path in store_files or path.with_name(path.name + ".lock") in store_files:
            raise WhoopError("WHOOP credentials must use a separate file from the hub store")
    return path


@dataclass(frozen=True, repr=False)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float

    def __post_init__(self) -> None:
        if (
            not all(isinstance(value, str) and value and value.isascii()
                    and value.isprintable() and not any(char.isspace() for char in value)
                    for value in (self.access_token, self.refresh_token))
            or type(self.expires_at) not in (float, int)
            or not 0 < self.expires_at < 253402300799
        ):
            raise WhoopError("WHOOP token data is invalid")


def _open_private(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        os.close(fd)
        raise WhoopError("WHOOP credential files must be owner-only regular files with mode 600")
    return fd


@dataclass(frozen=True)
class TokenStore:
    path: Path

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with os.fdopen(_open_private(lock_path, os.O_RDWR | os.O_CREAT), "rb+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def load(self) -> Tokens | None:
        try:
            with os.fdopen(_open_private(self.path, os.O_RDONLY), encoding="utf-8") as stream:
                values = json.load(stream)
            return Tokens(values["access_token"], values["refresh_token"], values["expires_at"])
        except FileNotFoundError:
            return None
        except (ValueError, TypeError, KeyError):
            raise WhoopError("WHOOP token file is invalid or has unsafe permissions") from None

    def save(self, tokens: Tokens) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, prefix=".whoop-", delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            try:
                json.dump({
                    "access_token": tokens.access_token,
                    "refresh_token": tokens.refresh_token,
                    "expires_at": tokens.expires_at,
                }, stream)
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
                temp_path.replace(self.path)
            finally:
                with suppress(FileNotFoundError):
                    temp_path.unlink()

    def status(self) -> dict[str, str | None]:
        try:
            tokens = self.load()
        except (OSError, WhoopError):
            return {"token_state": "unreadable", "token_expires_at": None}
        if tokens is None:
            return {"token_state": "missing", "token_expires_at": None}
        return {
            "token_state": "expired" if tokens.expires_at <= time.time() else "ready",
            "token_expires_at": datetime.fromtimestamp(tokens.expires_at, UTC).isoformat(),
        }


def authorization_url(config: OAuthConfig, state: str) -> str:
    return WebApplicationClient(config.client_id).prepare_request_uri(
        AUTH_URL, redirect_uri=config.redirect_uri, scope=SCOPES, state=state,
    )


def new_state() -> str:
    return secrets.token_urlsafe(6)


def callback_code(url: str, redirect_uri: str, state: str) -> str:
    try:
        callback = urlsplit(url)
    except ValueError:
        raise WhoopError("Invalid WHOOP callback URL") from None
    if callback._replace(query="").geturl() != urlsplit(redirect_uri).geturl():
        raise WhoopError("WHOOP callback URL does not match the registered redirect URL")
    params = parse_qs(callback.query, keep_blank_values=True)
    states = params.get("state", [])
    if len(states) != 1 or not states[0].isascii() or not compare_digest(states[0], state):
        raise WhoopError("WHOOP callback state did not match")
    if "error" in params:
        raise WhoopError("WHOOP authorization was declined or rejected")
    codes = params.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise WhoopError("WHOOP callback did not contain one authorization code")
    return codes[0]


@dataclass(frozen=True)
class OAuth:
    config: OAuthConfig
    store: TokenStore
    http: httpx.Client

    def _token_request(self, body: str) -> Tokens:
        started_at = time.time()
        try:
            response = self.http.post(
                TOKEN_URL, content=body, headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.RequestError:
            raise WhoopError("WHOOP token request failed in transit") from None
        if not response.is_success:
            raise WhoopError(f"WHOOP token request returned HTTP {response.status_code}. Run whoop-auth if access was lost.")
        try:
            values = response.json()
            if isinstance(values["expires_in"], bool):
                raise ValueError
            seconds = float(values["expires_in"])
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError
            tokens = Tokens(values["access_token"], values["refresh_token"], started_at + seconds)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise WhoopError("WHOOP returned invalid token data. Authorize with the offline scope.") from None

        # Refresh requests are never retried. Persist both rotated values before returning.
        self.store.save(tokens)
        return tokens

    def exchange(self, code: str) -> None:
        client = WebApplicationClient(self.config.client_id)
        body = client.prepare_request_body(
            code=code, redirect_uri=self.config.redirect_uri, client_secret=self.config.client_secret,
        )
        with self.store.locked():
            self.store.load()  # Verify the destination before consuming a one-use code.
            self._token_request(body)

    @contextmanager
    def session(self) -> Iterator[TokenSession]:
        # Hold the same lock until the caller finishes using the grant. A second
        # process reads the latest token pair after the first process releases it.
        with self.store.locked():
            tokens = self.store.load()
            if tokens is None:
                raise WhoopError("Run whoop-auth before using WHOOP")
            yield TokenSession(self, tokens)


@dataclass(repr=False)
class TokenSession:
    oauth: OAuth
    tokens: Tokens

    def access_token(self, *, refresh: bool = False) -> str:
        if refresh or self.tokens.expires_at <= time.time() + 30:
            config = self.oauth.config
            client = WebApplicationClient(config.client_id)
            body = client.prepare_refresh_body(
                refresh_token=self.tokens.refresh_token, scope="offline",
                client_id=config.client_id, client_secret=config.client_secret,
            )
            self.tokens = self.oauth._token_request(body)
        return self.tokens.access_token
