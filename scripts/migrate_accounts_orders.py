from __future__ import annotations

import argparse
from typing import Any

import sys
from pathlib import Path

# Ensure repo root is on sys.path for "app" imports.
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.libs.database import list_account_names, read_account, write_account


def _ensure_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _migrate_account(account: dict[str, Any]) -> bool:
    changed = False

    if "orders" not in account or not isinstance(account.get("orders"), list):
        account["orders"] = _ensure_list(account.get("orders"))
        changed = True

    if "paper_orders" not in account or not isinstance(account.get("paper_orders"), list):
        account["paper_orders"] = _ensure_list(account.get("paper_orders"))
        changed = True

    return changed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add orders/paper_orders fields to existing account records."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing to the database.",
    )
    args = parser.parse_args()

    names = list_account_names()
    if not names:
        print("No accounts found.")
        return 0

    updated = 0
    skipped = 0
    for name in names:
        data = read_account(name)
        if not isinstance(data, dict):
            print(f"SKIP {name}: unreadable account data.")
            skipped += 1
            continue

        if _migrate_account(data):
            updated += 1
            if args.dry_run:
                print(f"DRY_RUN {name}: would update orders/paper_orders.")
            else:
                write_account(name, data)
                print(f"UPDATED {name}: orders/paper_orders added.")

    if args.dry_run:
        print(f"Dry run complete. Accounts to update: {updated}. Skipped: {skipped}.")
    else:
        print(f"Migration complete. Updated: {updated}. Skipped: {skipped}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
