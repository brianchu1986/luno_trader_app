# main.py
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import signal
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import List

from app import load_env
from app.libs.account import Account
from app.libs.tracers import LogTracer
from app.libs.traders import Trader
from agents import add_trace_processor


# =========================
# Scheduler safety controls
# =========================
JITTER_SECONDS = 3  # per-trader random jitter
TRADER_TIMEOUT_SECONDS = 90  # hard timeout per trader run
HEARTBEAT_DETAILS = False  # verbose per-trader heartbeat


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -------------------------
# logging
# -------------------------
def setup_logging(level: str | None = None) -> None:
    log_level = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    numeric_level = getattr(logging, log_level, logging.INFO)

    log_dir = os.getenv("LOG_DIR", "logs")
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, "trader.log")

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    root = logging.getLogger()
    root.setLevel(numeric_level)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)


log = logging.getLogger("scheduler")


# -------------------------
# args
# -------------------------
def str_to_bool(s: str) -> bool:
    return str(s).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Luno Trader Bot Scheduler")

    p.add_argument(
        "--run-every",
        type=int,
        default=None,
        help="Run every N minutes (override env RUN_EVERY_N_MINUTES)",
    )
    p.add_argument(
        "--many-models",
        action="store_true",
        help="Use different models per trader (override env USE_MANY_MODELS)",
    )
    p.add_argument("--once", action="store_true", help="Run one cycle then exit")
    p.add_argument(
        "--names",
        default=None,
        help="Comma-separated trader names override (e.g. Warren,George)",
    )
    p.add_argument(
        "--log-level",
        default=None,
        help="Optional log level override (DEBUG/INFO/...)",
    )
    p.add_argument(
        "--timeout-seconds",
        type=int,
        default=None,
        help="Override per-trader timeout in seconds (env TRADER_TIMEOUT_SECONDS)",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="Run in LIVE mode (default is dry_run)",
    )
    return p.parse_args()


def resolve_config(args: argparse.Namespace) -> dict:
    # RUN_EVERY_MINUTES reads from env by default
    run_every_env = int(os.getenv("RUN_EVERY_N_MINUTES", "10"))
    use_many_env = str_to_bool(os.getenv("USE_MANY_MODELS", "false"))
    timeout_env_raw = os.getenv("TRADER_TIMEOUT_SECONDS", str(TRADER_TIMEOUT_SECONDS))
    try:
        timeout_env = int(timeout_env_raw)
    except ValueError:
        timeout_env = TRADER_TIMEOUT_SECONDS

    run_every = args.run_every or run_every_env
    use_many_models = bool(args.many_models) or use_many_env
    account_type = "live" if args.live else "dry_run"
    timeout_seconds = args.timeout_seconds or timeout_env
    if timeout_seconds <= 0:
        timeout_seconds = TRADER_TIMEOUT_SECONDS

    default_names = [
        "Warren",
        # "George",
        # "Ray",
        # "Cathie"
    ]
    default_lastnames = [
        "Patience",
        # "Bold",
        # "Systematic",
        # "Crypto"
    ]

    if args.names:
        names = [x.strip() for x in args.names.split(",") if x.strip()]
        lastnames = ["Trader"] * len(names)
    else:
        names = default_names
        lastnames = default_lastnames

    if use_many_models:
        model_names = [
            # os.getenv("MODEL_1", "gpt-5-mini"),
            os.getenv("MODEL_2", "deepseek-chat"),
        ]
        model_names = model_names[: len(names)]
        if len(model_names) < len(names):
            model_names += [model_names[-1]] * (len(names) - len(model_names))
    else:
        model_names = [os.getenv("MODEL_DEFAULT", "gpt-5-mini")] * len(names)

    config = {
        "run_every_minutes": int(run_every),
        "use_many_models": use_many_models,
        "names": names,
        "lastnames": lastnames,
        "model_names": model_names,
        "once": bool(args.once),
        "log_level": args.log_level or os.getenv("LOG_LEVEL"),
        "account_type": account_type,
        "timeout_seconds": int(timeout_seconds),
    }
    print("config : ", config)
    return config


def apply_account_type(names: List[str], account_type: str) -> None:
    for name in names:
        try:
            Account.get(name).set_account_type(account_type)
        except Exception as e:
            log.warning(f"Failed to set account_type for {name}: {e}")


# -------------------------
# traders
# -------------------------
def create_traders(cfg: dict) -> List[Trader]:
    traders: List[Trader] = []
    for name, lastname, model_name in zip(
        cfg["names"], cfg["lastnames"], cfg["model_names"]
    ):
        traders.append(Trader(name=name, lastname=lastname, model_name=model_name))
    return traders


async def run_trader_safe(trader: Trader) -> None:
    """
    Run trader with:
    - jitter to avoid burst
    - timeout guard to prevent hangs
    """
    jitter = random.uniform(0, JITTER_SECONDS)
    await asyncio.sleep(jitter)

    start = time.monotonic()
    try:
        await asyncio.wait_for(trader.run(), timeout=TRADER_TIMEOUT_SECONDS)
        dur = time.monotonic() - start
        log.info(f"✅ {trader.name} finished in {dur:.2f}s (jitter={jitter:.2f}s)")
    except asyncio.TimeoutError:
        log.error(f"⏱ {trader.name} TIMEOUT after {TRADER_TIMEOUT_SECONDS}s")
    except Exception as e:
        log.exception(f"❌ {trader.name} crashed: {e}")


async def run_one_cycle(traders: List[Trader]) -> None:
    log.info(f"🫀 HEARTBEAT start | traders={len(traders)} | t={utc_now()}")

    await asyncio.gather(
        *[run_trader_safe(t) for t in traders],
        return_exceptions=True,
    )

    if HEARTBEAT_DETAILS:
        # lightweight, no extra API calls here
        for t in traders:
            log.info(f"Heartbeat details: trader={t.name}")

    log.info(f"🫀 HEARTBEAT end | t={utc_now()}")


async def scheduler_loop(cfg: dict) -> None:
    add_trace_processor(LogTracer())

    traders = create_traders(cfg)
    run_every = int(cfg["run_every_minutes"])

    log.info(
        f"✅ Scheduler started | every={run_every}min | jitter≤{JITTER_SECONDS}s | timeout={TRADER_TIMEOUT_SECONDS}s"
    )
    log.info(f"Run mode: {cfg['account_type']}")
    log.info(f"✅ Traders: {', '.join([t.name for t in traders])}")

    stop_event = asyncio.Event()

    def _stop(*_):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _stop())

    while not stop_event.is_set():
        await run_one_cycle(traders)

        if cfg["once"]:
            break

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=run_every * 60)
        except asyncio.TimeoutError:
            pass

    log.info("👋 Scheduler stopped.")


def main() -> None:
    load_env()
    args = parse_args()
    cfg = resolve_config(args)

    global TRADER_TIMEOUT_SECONDS
    TRADER_TIMEOUT_SECONDS = cfg["timeout_seconds"]

    setup_logging(cfg["log_level"])
    apply_account_type(cfg["names"], cfg["account_type"])

    try:
        asyncio.run(scheduler_loop(cfg))
    except RuntimeError as e:
        if "already running" in str(e).lower():
            print(
                "⚠️ Async loop already running (Spyder/Jupyter).\n"
                "Run from terminal instead:\n"
                "  python main.py\n"
                "Or enable: Spyder → Run → Execute in external system terminal"
            )
        else:
            raise


if __name__ == "__main__":
    main()
