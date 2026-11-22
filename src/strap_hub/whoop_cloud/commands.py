"""CLI commands and the one-shot loopback authorization callback."""

from __future__ import annotations

import argparse
import json
import re
import time
import warnings
from datetime import date
from getpass import GetPassWarning, getpass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from strap_hub.whoop_cloud.client import WhoopClient, http_client
from strap_hub.whoop_cloud.oauth import (
    OAuth, OAuthConfig, TokenStore, WhoopError, authorization_url, callback_code, new_state,
    parse_redirect_uri, token_path,
)
from strap_hub.whoop_cloud.sync import store_status, sync, sync_window

CALLBACK_WAIT_SECONDS = 180


def _iso_date(value: str) -> date:
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return date.fromisoformat(value)
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("Use YYYY-MM-DD for --since")


def register(commands: argparse._SubParsersAction) -> None:
    auth = commands.add_parser("whoop-auth", help="authorize the hub with WHOOP")
    auth.add_argument("--paste", action="store_true", help="paste the redirect URL in a hidden prompt")
    poll = commands.add_parser("whoop-sync", help="fetch WHOOP v2 records")
    poll.add_argument("--since", type=_iso_date, metavar="DATE", help="backfill from this UTC date (YYYY-MM-DD)")
    poll.add_argument("--dry-run", action="store_true", help="print the UTC sync window without contacting WHOOP")
    commands.add_parser("whoop-status", help="show WHOOP token state, last sync and stored row counts")
    commands.add_parser("whoop-revoke", help="revoke the WHOOP grant and remove its local tokens")


def _loopback_code(config: OAuthConfig, auth_url: str, state: str) -> str:
    redirect = parse_redirect_uri(config.redirect_uri)
    code = None

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, *args) -> None:
            pass  # Request targets can contain authorization codes.

        def do_GET(self) -> None:
            nonlocal code
            try:
                target = urlsplit(self.path)
                if target.scheme or target.netloc or target.path != (redirect.path or "/"):
                    raise WhoopError("Unexpected callback path")
                callback_url = redirect._replace(query=target.query, fragment=target.fragment).geturl()
                code = callback_code(callback_url, config.redirect_uri, state)
                status, message = 200, "Callback received. Return to strap-hub to finish authorization."
            except ValueError:
                status, message = 400, "Invalid WHOOP callback. Retry from the authorization URL."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(message.encode())

    try:
        server = HTTPServer(("127.0.0.1", redirect.port or 80), Handler)
    except OSError:
        raise WhoopError("Could not bind the WHOOP callback. Run whoop-auth --paste to authorize manually.") from None

    with server:
        print(auth_url, flush=True)
        deadline = time.monotonic() + CALLBACK_WAIT_SECONDS
        remaining = deadline - time.monotonic()
        while code is None and remaining > 0:
            server.timeout = remaining
            server.handle_request()
            remaining = deadline - time.monotonic()
    if code is None:
        raise WhoopError("WHOOP callback timed out. Run whoop-auth --paste to authorize manually.")
    return code


def _authorize(oauth: OAuth, *, paste: bool) -> None:
    state = new_state()
    url = authorization_url(oauth.config, state)
    if paste or urlsplit(oauth.config.redirect_uri).scheme != "http":
        print(url, flush=True)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                callback = getpass("Paste the complete WHOOP redirect URL (hidden): ")
        except (EOFError, GetPassWarning):
            raise WhoopError("Pasting a WHOOP callback requires an interactive terminal") from None
        code = callback_code(callback.strip(), oauth.config.redirect_uri, state)
    else:
        code = _loopback_code(oauth.config, url, state)
    oauth.exchange(code)
    print("WHOOP authorization saved.")


def run(args: argparse.Namespace, db_path: str | None) -> None:
    if not db_path and args.command in {"whoop-sync", "whoop-status"}:
        raise WhoopError("HUB_DB_PATH is required")
    if args.command == "whoop-sync" and args.dry_run:
        window = sync_window(db_path, args.since)
        print(json.dumps({"start": window.start, "end": window.end}))
        return

    store = TokenStore(token_path(db_path))
    if args.command == "whoop-status":
        print(json.dumps({**store.status(), **store_status(db_path)}))
        return

    config = OAuthConfig.from_env()
    with http_client() as http:
        oauth = OAuth(config, store, http)
        if args.command == "whoop-auth":
            _authorize(oauth, paste=args.paste)
        elif args.command == "whoop-sync":
            print(json.dumps(sync(db_path, oauth, args.since)))
        elif args.command == "whoop-revoke":
            with oauth.session() as session:
                WhoopClient(session).revoke()
            print("WHOOP access revoked.")
