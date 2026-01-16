from __future__ import annotations

import argparse
from datetime import datetime
import json
import re
import sqlite3
from typing import Any

import sys
from pathlib import Path

# Ensure repo root is on sys.path for "app" imports.
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.libs.database import get_db_path, write_account


def _ensure_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or ""))
    cleaned = cleaned.strip("_")
    return cleaned or "account"


def _backup_raw(name: str, raw: str, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    filename = f"{_safe_filename(name)}-{ts}.json"
    path = backup_dir / filename
    path.write_text(raw or "", encoding="utf-8")
    return path


def _migrate_account(account: dict[str, Any]) -> bool:
    changed = False

    if "orders" not in account or not isinstance(account.get("orders"), list):
        account["orders"] = _ensure_list(account.get("orders"))
        changed = True

    if "paper_orders" not in account or not isinstance(account.get("paper_orders"), list):
        account["paper_orders"] = _ensure_list(account.get("paper_orders"))
        changed = True

    if "orders_history" not in account or not isinstance(account.get("orders_history"), list):
        account["orders_history"] = _ensure_list(account.get("orders_history"))
        changed = True

    return changed


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Add orders/paper_orders/orders_history fields to existing account records "
            "and back up unreadable account JSON."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing to the database.",
    )
    parser.add_argument(
        "--backup-dir",
        default=str(ROOT_DIR / "app" / "data" / "account_backups"),
        help="Directory to store backups of unreadable account JSON.",
    )
    parser.add_argument(
        "--backup-all",
        action="store_true",
        help="Back up raw JSON for every account, not just unreadable ones.",
    )
    args = parser.parse_args()

    db_path = get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, account FROM accounts ORDER BY name ASC"
        ).fetchall()

    if not rows:
        print("No accounts found.")
        return 0

    updated = 0
    skipped = 0
    backed_up = 0
    backup_dir = Path(args.backup_dir)

    for row in rows:
        name = str(row["name"])
        raw = row["account"]
        if args.backup_all:
            _backup_raw(name, raw, backup_dir)
            backed_up += 1

        try:
            data = json.loads(raw)
        except Exception:
            if not args.backup_all:
                path = _backup_raw(name, raw, backup_dir)
                backed_up += 1
                print(f"SKIP {name}: unreadable JSON (backed up to {path}).")
            else:
                print(f"SKIP {name}: unreadable JSON.")
            skipped += 1
            continue

        if not isinstance(data, dict):
            if not args.backup_all:
                path = _backup_raw(name, raw, backup_dir)
                backed_up += 1
                print(f"SKIP {name}: non-dict JSON (backed up to {path}).")
            else:
                print(f"SKIP {name}: non-dict JSON.")
            skipped += 1
            continue

        if _migrate_account(data):
            updated += 1
            if args.dry_run:
                print(f"DRY_RUN {name}: would update orders fields.")
            else:
                write_account(name, data)
                print(f"UPDATED {name}: orders fields added.")

    if args.dry_run:
        print(
            "Dry run complete. "
            f"Accounts to update: {updated}. "
            f"Skipped: {skipped}. "
            f"Backed up: {backed_up}."
        )
    else:
        print(
            "Migration complete. "
            f"Updated: {updated}. "
            f"Skipped: {skipped}. "
            f"Backed up: {backed_up}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
