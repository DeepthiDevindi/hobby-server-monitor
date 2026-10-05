"""Create/upgrade the SQLite schema and the metrics directory.

    python -m hsm.init_db            # idempotent; safe to re-run
    python -m hsm.init_db --check    # exit 1 if migrations are pending

Also invites BOOTSTRAP_ADMIN_EMAIL as admin, so the first admin exists before
anyone can sign in (see README "Bootstrap admin").
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings
from .db import MIGRATIONS, Database, connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only report whether migrations are pending")
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.check:
        version = connect(settings.sqlite_path).execute("PRAGMA user_version").fetchone()[0]
        print(f"schema version {version}/{len(MIGRATIONS)}")
        return 0 if version == len(MIGRATIONS) else 1

    db = Database(settings.sqlite_path)  # applies migrations
    Path(settings.tinyflux_dir).mkdir(parents=True, exist_ok=True)
    email = settings.bootstrap_admin_email
    if email and not db.get_user_by_email(email):
        db.invite_user(email, "admin", None, {})
        db.audit("bootstrap_admin_invited", target=email)
        print(f"invited bootstrap admin {email}")
    elif not email:
        print("warning: BOOTSTRAP_ADMIN_EMAIL is empty, nobody will be able to sign in", file=sys.stderr)
    version = db.conn.execute("PRAGMA user_version").fetchone()[0]
    print(f"database ready at {settings.sqlite_path} (schema v{version}); metrics dir {settings.tinyflux_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
