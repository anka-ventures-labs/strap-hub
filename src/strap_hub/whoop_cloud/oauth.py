"""WHOOP authorization and single-writer storage for rotating credentials."""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import secrets
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
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
TOKEN_SIDECARS = (".lock", ".staged", ".promote")
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
logger = logging.getLogger(__name__)


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
    db = Path(db_path).expanduser().absolute() if db_path else None
    if custom:
        path = Path(custom).expanduser().absolute()
    elif db is not None:
        path = db.with_name(db.name + ".whoop-token.json")
    else:
        raise WhoopError("Set HUB_DB_PATH or HUB_WHOOP_TOKEN_PATH")

    if db is not None:
        store_files = {
            target.with_name(target.name + suffix)
            for target in (Path(os.path.abspath(db)), db.resolve())
            for suffix in ("", "-wal", "-shm", "-journal")
        }
        if store_files.intersection(path.with_name(path.name + suffix) for suffix in ("", *TOKEN_SIDECARS)):
            raise WhoopError("WHOOP credentials must use a separate file from the hub store")
    return path


@dataclass(frozen=True, repr=False)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float
    user_id: int | None = None
    generation: int = 0

    def __post_init__(self) -> None:
        if (
            not all(isinstance(value, str) and value and value.isascii()
                    and value.isprintable() and not any(char.isspace() for char in value)
                    for value in (self.access_token, self.refresh_token))
            or type(self.expires_at) not in (float, int)
            or not 0 < self.expires_at < 253402300799
            or (self.user_id is not None and not valid_user_id(self.user_id))
            or type(self.generation) is not int or self.generation < 0
        ):
            raise WhoopError("WHOOP token data is invalid")


def valid_user_id(value: object) -> bool:
    return type(value) is int and value > 0


def _open_private(directory: int, name: str, flags: int) -> int:
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        os.close(fd)
        raise WhoopError("WHOOP credential files must be owner-only regular files with mode 600")
    return fd


def _read_tokens(directory: int, name: str) -> Tokens | None:
    try:
        with os.fdopen(_open_private(directory, name, os.O_RDONLY), encoding="utf-8") as stream:
            values = json.load(stream)
        return Tokens(
            values["access_token"], values["refresh_token"], values["expires_at"],
            values.get("user_id"), values.get("generation", 0),
        )
    except FileNotFoundError:
        return None
    except (ValueError, TypeError, KeyError):
        raise WhoopError("WHOOP token file is invalid or has unsafe permissions") from None


@dataclass(frozen=True)
class LockedTokens:
    directory: int
    name: str

    @property
    def staging(self) -> str:
        return self.name + ".staged"

    def load(self) -> Tokens | None:
        live = _read_tokens(self.directory, self.name)
        staged = _read_tokens(self.directory, self.staging)
        if staged is None:
            return live
        if live is not None and staged.generation <= live.generation:
            if staged != live:
                logger.warning("Discarding a WHOOP token stage without a newer generation")
            # Make the live rename durable before removing its recovery link.
            os.fsync(self.directory)
            self._clear_stage()
            return live
        # The previous writer may have failed before syncing the staged file.
        with os.fdopen(_open_private(self.directory, self.staging, os.O_RDONLY), "rb") as stream:
            os.fsync(stream.fileno())
        self._promote()
        return staged

    def _clear_stage(self) -> None:
        with suppress(FileNotFoundError):
            os.unlink(self.name + ".promote", dir_fd=self.directory)
        os.unlink(self.staging, dir_fd=self.directory)
        os.fsync(self.directory)

    def _promote(self) -> None:
        # Keep a link to the staged pair until the replacement is durable.
        os.fsync(self.directory)
        promotion = self.name + ".promote"
        with suppress(FileNotFoundError):
            os.unlink(promotion, dir_fd=self.directory)
        os.link(self.staging, promotion, src_dir_fd=self.directory, dst_dir_fd=self.directory,
                follow_symlinks=False)
        os.replace(promotion, self.name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
        os.fsync(self.directory)
        # Replacing an existing link to the same inode can be a no-op.
        self._clear_stage()

    def save(self, tokens: Tokens) -> Tokens:
        live = self.load()
        tokens = replace(tokens, generation=live.generation + 1 if live else 1)
        fd = _open_private(self.directory, self.staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "expires_at": tokens.expires_at,
                "user_id": tokens.user_id,
                "generation": tokens.generation,
            }, stream)
            stream.flush()
            os.fsync(fd)
        self._promote()
        return tokens

    def remove(self) -> None:
        os.unlink(self.name, dir_fd=self.directory)
        os.fsync(self.directory)


@dataclass(frozen=True)
class TokenStore:
    path: Path

    @contextmanager
    def _directory(self, *, create: bool) -> Iterator[int]:
        path = self.path.absolute()
        if ".." in path.parts or not path.name:
            raise WhoopError("WHOOP credential path must name a file without parent traversals")
        parts = path.parent.parts
        uid = os.getuid()
        directory = os.open(parts[0], DIRECTORY_FLAGS)
        try:
            for part in parts[1:]:
                info = os.fstat(directory)
                # A sticky ancestor such as /tmp protects trusted owners' entries.
                if info.st_uid not in (0, uid) or (
                    info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX
                ):
                    raise WhoopError("WHOOP credential directory has an unsafe parent")
                try:
                    child = os.open(part, DIRECTORY_FLAGS, dir_fd=directory)
                except FileNotFoundError:
                    if not create:
                        raise
                    with suppress(FileExistsError):
                        os.mkdir(part, mode=0o700, dir_fd=directory)
                    child = os.open(part, DIRECTORY_FLAGS, dir_fd=directory)
                os.close(directory)
                directory = child
            info = os.fstat(directory)
            if info.st_uid != uid or info.st_mode & 0o022:
                raise WhoopError("WHOOP credential directory must be owned by the current user and not group/world writable")
            yield directory
        finally:
            os.close(directory)

    @contextmanager
    def locked(self) -> Iterator[LockedTokens]:
        with self._directory(create=True) as directory:
            fd = _open_private(directory, self.path.name + ".lock", os.O_RDWR | os.O_CREAT)
            with os.fdopen(fd, "rb+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield LockedTokens(directory, self.path.name)

    def status(self) -> dict[str, str | None]:
        try:
            with self._directory(create=False) as directory:
                # Status only inspects local files. OAuth promotes staging under the lock.
                tokens = _read_tokens(directory, self.path.name)
                staged = _read_tokens(directory, self.path.name + ".staged")
                if staged is not None and (tokens is None or staged.generation > tokens.generation):
                    tokens = staged
        except FileNotFoundError:
            tokens = None
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

    def _token_request(self, body: str, *, user_id: int | None = None) -> Tokens:
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
            return Tokens(values["access_token"], values["refresh_token"], started_at + seconds, user_id)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise WhoopError("WHOOP returned invalid token data. Authorize with the offline scope.") from None

    @contextmanager
    def exchange(self, code: str) -> Iterator[TokenSession]:
        client = WebApplicationClient(self.config.client_id)
        body = client.prepare_request_body(
            code=code, redirect_uri=self.config.redirect_uri, client_secret=self.config.client_secret,
        )
        with self.store.locked() as credentials:
            credentials.load()  # Verify the destination before consuming a one-use code.
            yield TokenSession(self, credentials, self._token_request(body))

    @contextmanager
    def session(self) -> Iterator[TokenSession]:
        # Hold the same lock until the caller finishes using the grant. A second
        # process reads the latest token pair after the first process releases it.
        with self.store.locked() as credentials:
            tokens = credentials.load()
            if tokens is None:
                raise WhoopError("Run whoop-auth before using WHOOP")
            yield TokenSession(self, credentials, tokens)


@dataclass(repr=False)
class TokenSession:
    oauth: OAuth
    credentials: LockedTokens
    tokens: Tokens

    def access_token(self, *, refresh: bool = False) -> str:
        if refresh or self.tokens.expires_at <= time.time() + 30:
            current = self.credentials.load()
            if current is None:
                raise WhoopError("Run whoop-auth before using WHOOP")
            if current != self.tokens and current.expires_at > time.time() + 30:
                self.tokens = current
                return current.access_token
            config = self.oauth.config
            client = WebApplicationClient(config.client_id)
            body = client.prepare_refresh_body(
                refresh_token=current.refresh_token, scope="offline",
                client_id=config.client_id, client_secret=config.client_secret,
            )
            # Refresh requests are never retried. Persist both rotated values before returning.
            self.tokens = self.credentials.save(self.oauth._token_request(body, user_id=current.user_id))
        return self.tokens.access_token
