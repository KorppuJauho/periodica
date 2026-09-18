"""Entry point: ``python -m periodica`` runs the web app; ``scan`` runs a single scan from the CLI."""

from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="periodica")
    sub = parser.add_subparsers(dest="command")
    scan = sub.add_parser("scan", help="run one scan and exit")
    scan.add_argument("--dry-run", action="store_true", help="show what would happen without changing anything")
    args = parser.parse_args(argv)

    from .config import load_env

    env = load_env()
    logging.basicConfig(level=env.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "scan":
        from . import netsafe
        from .db import Database
        from .repo import Repo
        from .scanner import Scanner
        from .settings import SettingsStore

        netsafe.OFFLINE = env.offline
        db = Database(env.db_path)
        db.migrate()
        report = Scanner(env, SettingsStore(db, offline=env.offline), Repo(db)).run(dry_run=args.dry_run)
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
        return 1 if report.error else 0

    import uvicorn

    from .app import create_app

    uvicorn.run(
        create_app(env), host=env.host, port=env.port, log_level=env.log_level,
        proxy_headers=False, server_header=False, date_header=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
