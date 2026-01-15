# app/__init__.py
from __future__ import annotations

import os
import logging
from typing import Optional

from dotenv import load_dotenv

# Your Luno client wrapper (your repo has app/libs/client.py)
from app.libs.client import Client


_LOGGER_CONFIGURED = False
_CLIENT: Optional[Client] = None
_ADMIN_CLIENT: Optional[Client] = None


def setup_logging(level: str | None = None) -> None:
    """Configure logging once for the entire app."""
    global _LOGGER_CONFIGURED
    if _LOGGER_CONFIGURED:
        return

    lvl = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    _LOGGER_CONFIGURED = True


def load_env() -> None:
    """Load environment variables from .env (if present)."""
    load_dotenv(override=False)


def get_luno_admin_client() -> Client:
    """
    Lazy-create a single Client instance (singleton-ish).
    Make sure your Client reads LUNO_API_KEY/LUNO_API_SECRET or accepts them.
    """
    global _ADMIN_CLIENT
    if _ADMIN_CLIENT is not None:
        return _ADMIN_CLIENT
    api_key = os.getenv("LUNO_ADMIN_KEY")
    api_secret = os.getenv("LUNO_ADMIN_SECRET")

    if not api_key or not api_secret:
        raise RuntimeError(
            "Missing LUNO_ADMIN_KEY / LUNO_ADMIN_SECRET in environment (.env)."
        )

    # If your Client() constructor differs, adjust here:
    _ADMIN_CLIENT = Client(api_key_id=api_key, api_key_secret=api_secret)
    return _ADMIN_CLIENT


def get_luno_client() -> Client:
    """
    Lazy-create a single Client instance (singleton-ish).
    Make sure your Client reads LUNO_API_KEY/LUNO_API_SECRET or accepts them.
    """
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    api_key = os.getenv("LUNO_API_KEY")
    api_secret = os.getenv("LUNO_API_SECRET")

    if not api_key or not api_secret:
        raise RuntimeError(
            "Missing LUNO_API_KEY / LUNO_API_SECRET in environment (.env)."
        )

    # If your Client() constructor differs, adjust here:
    _CLIENT = Client(api_key_id=api_key, api_key_secret=api_secret)
    return _CLIENT


def get_counter_currency() -> str:
    """
    return the counter currency that we'll trade all time that's MYR
    """
    return "MYR"
