"""Command line for the hub web service, backup import and WHOOP sync."""

import argparse
import os
import sqlite3
from collections.abc import Sequence

import uvicorn

from strap_hub.importer import DEFAULT_MAX_BYTES, import_backup
from strap_hub.ingest.api import create_app
from strap_hub.whoop_cloud import commands as whoop_commands

DEFAULT_BIND = "127.0.0.1:8770"


def parse_bind(bind: str) -> tuple[str, int]:
    host, separator, port_text = bind.rpartition(":")
    if not separator or not host or not port_text.isascii() or not port_text.isdecimal():
        raise ValueError("HUB_BIND must be a host and port, such as 127.0.0.1:8770")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("HUB_BIND port must be between 1 and 65535")
    return host.removeprefix("[").removesuffix("]"), port


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="strap-hub")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="serve the digest API")
    importer = commands.add_parser("import", help="import a local Edge database backup")
    importer.add_argument("file", help="path to a .db or .db.gz backup")
    importer.add_argument("--algo-ceiling", type=int, help="highest Edge algorithm version to import for nights")
    importer.add_argument(
        "--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="maximum expanded gzip bytes (default 2 GiB)"
    )
    whoop_commands.register(commands)
    args = parser.parse_args(argv)

    db_path = os.environ.get("HUB_DB_PATH")
    if not db_path and args.command in {"serve", "import"}:
        parser.error("HUB_DB_PATH is required")

    try:
        if args.command == "serve":
            os.umask(0o077)
            host, port = parse_bind(os.environ.get("HUB_BIND", DEFAULT_BIND))
            app = create_app(token=os.environ.get("HUB_TOKEN", ""), db_path=db_path)
            uvicorn.run(app, host=host, port=port, access_log=False)
        elif args.command == "import":
            counts = import_backup(args.file, db_path, algo_ceiling=args.algo_ceiling, max_bytes=args.max_bytes)
            print(
                f"Accepted {counts['days']} days, {counts['nights']} nights, "
                f"and {counts['workouts']} workouts."
            )
        else:
            os.umask(0o077)
            whoop_commands.run(args, db_path)
    except (ValueError, OSError, EOFError, sqlite3.Error) as exc:
        parser.exit(1, f"strap-hub: {exc}\n")


if __name__ == "__main__":
    main()
