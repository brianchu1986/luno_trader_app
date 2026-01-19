# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]  # app/
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "accounts.db"


# ---- Internal helpers ----
def _connect() -> sqlite3.Connection:
    """
    Create a configured SQLite connection.
    - WAL: better concurrency
    - busy_timeout: avoid 'database is locked' on quick retries
    - foreign_keys: future-proof
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    # Pragmas (safe defaults for app usage)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _json_loads(s: str) -> Any:
    return json.loads(s)


def _serialize_agent_output(output: Any) -> Tuple[str, int]:
    if isinstance(output, str):
        return output, 0
    try:
        return _json_dumps(output), 1
    except Exception:
        return str(output), 0


# ---- Init schema (run on import) ----
def _init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                name TEXT PRIMARY KEY,
                account TEXT NOT NULL
            )
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                datetime_utc TEXT NOT NULL,
                type TEXT NOT NULL,
                message TEXT NOT NULL
            )
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                datetime_utc TEXT NOT NULL,
                trace_id TEXT,
                run_type TEXT,
                output TEXT NOT NULL,
                output_is_json INTEGER NOT NULL DEFAULT 0
            )
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS market (
                date TEXT PRIMARY KEY,
                data TEXT NOT NULL
            )
        """
        )

        # Helpful indexes
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_logs_name_dt ON logs(name, datetime_utc);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_outputs_name_dt ON agent_outputs(name, datetime_utc);"
        )


_init_db()


# ---- Public API (compatible with your current functions) ----
def write_account(name: str, account_dict: Dict[str, Any]) -> None:
    json_data = _json_dumps(account_dict)
    logger.debug("write_account: %s", json_data)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO accounts (name, account)
            VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET account=excluded.account
        """,
            (name.lower(), json_data),
        )


def read_account(name: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT account FROM accounts WHERE name = ?",
            (name.lower(),),
        ).fetchone()

    if not row:
        return None

    try:
        return _json_loads(row["account"])
    except Exception:
        # If old/corrupt JSON is stored, fail safely
        return None


def list_account_names() -> List[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT name FROM accounts ORDER BY name ASC").fetchall()
    return [str(r["name"]) for r in rows]


def write_log(name: str, type: str, message: str) -> None:
    """
    Store logs in UTC for consistency across servers/timezones.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO logs (name, datetime_utc, type, message)
            VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, ?)
        """,
            (name.lower(), type, message),
        )
    if type in {"order", "trade"}:
        try:
            logging.getLogger("orders").info("%s | %s | %s", name, type, message)
        except Exception:
            pass
    if type == "agent_output":
        try:
            logging.getLogger("agent_output").info("%s | %s | %s", name, type, message)
        except Exception:
            pass


def read_log(name: str, last_n: int = 10) -> List[Tuple[str, str, str]]:
    """
    Returns newest->oldest in DB, but we return oldest->newest for readability.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT datetime_utc, type, message
            FROM logs
            WHERE name = ?
            ORDER BY datetime_utc DESC
            LIMIT ?
        """,
            (name.lower(), last_n),
        ).fetchall()

    # Convert to tuples & reverse to chronological order
    result = [(r["datetime_utc"], r["type"], r["message"]) for r in rows]
    result.reverse()
    return result


def write_agent_output(
    name: str,
    output: Any,
    trace_id: Optional[str] = None,
    run_type: Optional[str] = None,
) -> None:
    output_text, output_is_json = _serialize_agent_output(output)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_outputs (name, datetime_utc, trace_id, run_type, output, output_is_json)
            VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, ?, ?, ?)
        """,
            (name.lower(), trace_id, run_type, output_text, output_is_json),
        )


def read_agent_outputs(name: str, last_n: int = 10) -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT datetime_utc, trace_id, run_type, output, output_is_json
            FROM agent_outputs
            WHERE name = ?
            ORDER BY datetime_utc DESC
            LIMIT ?
        """,
            (name.lower(), last_n),
        ).fetchall()

    result: List[Dict[str, Any]] = []
    for row in rows:
        output_value: Any = row["output"]
        if row["output_is_json"]:
            try:
                output_value = _json_loads(output_value)
            except Exception:
                pass
        result.append(
            {
                "datetime_utc": row["datetime_utc"],
                "trace_id": row["trace_id"],
                "run_type": row["run_type"],
                "output": output_value,
            }
        )
    result.reverse()
    return result


def write_market(date: str, data: Dict[str, Any]) -> None:
    data_json = _json_dumps(data)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO market (date, data)
            VALUES (?, ?)
            ON CONFLICT(date) DO UPDATE SET data=excluded.data
        """,
            (date, data_json),
        )


def read_market(date: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT data FROM market WHERE date = ?",
            (date,),
        ).fetchone()

    if not row:
        return None

    try:
        return _json_loads(row["data"])
    except Exception:
        return None


# Optional: quick debug helper
def get_db_path() -> str:
    return str(DB_PATH)
