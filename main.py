# main.py
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import random
import signal
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import List
from decimal import Decimal, InvalidOperation

from app import load_env, get_counter_currency, get_luno_client
from app.libs.account import (
    Account,
    assign_myr_accounts_to_traders,
    assign_portfolio_holdings_to_traders,
    parse_portfolio_holdings_arg,
)
from app.libs.admin import distribute_myr_balances, parse_myr_balances_arg
from app.libs.tracers import LogTracer
from app.libs.database import list_account_names, read_account
from app.libs.client import Client
from app.libs.traders import Trader
from app.libs.strategy import STRATEGY_REGISTRY
from agents import add_trace_processor


# =========================
# Scheduler safety controls
# =========================
JITTER_SECONDS = 3  # per-trader random jitter
TRADER_TIMEOUT_SECONDS = 90  # hard timeout per trader run
HEARTBEAT_DETAILS = False  # verbose per-trader heartbeat
DEFAULT_NAMES = [
    "Warren",
    "George",
    # "Ray",
    # "Cathie"
]
DEFAULT_LASTNAMES = [
    "Patience",
    "Bold",
    # "Systematic",
    # "Crypto"
]


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


def parse_int_list(raw: str | int | None, default: int) -> List[int]:
    if raw is None:
        return [default]
    if isinstance(raw, int):
        return [raw if raw > 0 else default]
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    if not parts:
        return [default]
    values = []
    for p in parts:
        try:
            v = int(p)
        except ValueError:
            v = default
        if v <= 0:
            v = default
        values.append(v)
    return values


def align_list(values: List[int], target_len: int) -> List[int]:
    if target_len <= 0:
        return []
    if not values:
        return []
    if len(values) < target_len:
        values = values + [values[-1]] * (target_len - len(values))
    return values[:target_len]


def parse_strategies_arg(raw: str | None, trader_count: int) -> List[str]:
    if raw is None or trader_count <= 0:
        return []
    text = str(raw).strip()
    if not text:
        return []
    parts = [p.strip() for p in text.split(",")]
    if len(parts) == 1 and trader_count > 1:
        parts = parts * trader_count
    elif len(parts) < trader_count:
        parts = parts + [parts[-1]] * (trader_count - len(parts))
    elif len(parts) > trader_count:
        raise ValueError(
            f"Too many strategies: {len(parts)} (traders={trader_count})"
        )
    return parts


def resolve_strategy_entry(entry: str) -> str | None:
    text = str(entry or "").strip()
    if not text:
        return None
    key = text.lower()
    if key in {"none", "clear", "reset"}:
        return ""
    if key in STRATEGY_REGISTRY:
        return STRATEGY_REGISTRY[key]
    return text


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Luno Trader Bot Scheduler")

    p.add_argument(
        "--run-every",
        default=None,
        help=(
            "Run every N minutes (override env RUN_EVERY_N_MINUTES). "
            "Use comma-separated values aligned with --names (e.g. 5,60)."
        ),
    )
    p.add_argument(
        "--many-models",
        action="store_true",
        help="Use different models per trader (override env USE_MANY_MODELS)",
    )
    p.add_argument(
        "--model-default",
        default=None,
        help="Override env MODEL_DEFAULT for all traders",
    )
    p.add_argument(
        "--models",
        default=None,
        help="Comma-separated model names aligned to trader names (implies many-models)",
    )
    p.add_argument("--once", action="store_true", help="Run one cycle then exit")
    p.add_argument(
        "--names",
        default=None,
        help="Comma-separated trader names override (e.g. Warren,George)",
    )
    p.add_argument(
        "--strategies",
        default=None,
        help=(
            "Comma-separated strategy keys or text aligned with --names "
            "(e.g. warren,george). Use 'none' to clear."
        ),
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
        "--force-run",
        action="store_true",
        help="Run immediately even if still within the cooldown window",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="Run in LIVE mode (default is dry_run)",
    )
    p.add_argument(
        "--myr-balances",
        default=None,
        help=(
            "Admin: reset MYR accounts to MYR_0 and distribute balances to MYR_1..MYR_9. "
            "Comma-separated list, max 9 items (e.g. 20,10,3.5). "
            "Requires LUNO_ADMIN_KEY/LUNO_ADMIN_SECRET."
        ),
    )
    p.add_argument(
        "--holdings",
        default=None,
        help=(
            "Per-trader portfolio holdings aligned with --names. "
            "Use ';' between traders and ',' between assets: "
            "XBT:0.1,ETH:2;XBT:0.05. "
            "Single group applies to all traders."
        ),
    )
    p.add_argument(
        "--show-portfolio",
        action="store_true",
        help="Show trader names with portfolio balance/holdings and exit.",
    )
    p.add_argument(
        "--show-luno-balances",
        action="store_true",
        help="Show balances from Luno client.get_balances() and exit.",
    )
    p.add_argument(
        "--live-only",
        action="store_true",
        help="Filter --show-portfolio output to live accounts only.",
    )
    p.add_argument(
        "--dry-run-only",
        action="store_true",
        help="Filter --show-portfolio output to dry_run accounts only.",
    )
    return p.parse_args()


def resolve_names(args: argparse.Namespace) -> List[str]:
    if args.names:
        return [x.strip() for x in args.names.split(",") if x.strip()]
    return list(DEFAULT_NAMES)


def resolve_portfolio_names(args: argparse.Namespace) -> List[str]:
    if args.names:
        return [x.strip() for x in args.names.split(",") if x.strip()]
    return list_account_names()


def resolve_config(args: argparse.Namespace) -> dict:
    # RUN_EVERY_MINUTES reads from env by default
    run_every_env_raw = os.getenv("RUN_EVERY_N_MINUTES", "10")
    run_every_env_list = parse_int_list(run_every_env_raw, default=10)
    use_many_env = str_to_bool(os.getenv("USE_MANY_MODELS", "false"))
    timeout_env_raw = os.getenv("TRADER_TIMEOUT_SECONDS", str(TRADER_TIMEOUT_SECONDS))
    try:
        timeout_env = int(timeout_env_raw)
    except ValueError:
        timeout_env = TRADER_TIMEOUT_SECONDS

    run_every_list = (
        parse_int_list(args.run_every, default=run_every_env_list[0])
        if args.run_every
        else run_every_env_list
    )
    use_many_models = bool(args.many_models) or use_many_env
    model_default = args.model_default or os.getenv("MODEL_DEFAULT", "gpt-5-mini")
    account_type = "live" if args.live else "dry_run"
    timeout_seconds = args.timeout_seconds or timeout_env
    if timeout_seconds <= 0:
        timeout_seconds = TRADER_TIMEOUT_SECONDS

    if args.names:
        names = [x.strip() for x in args.names.split(",") if x.strip()]
        lastnames = ["Trader"] * len(names)
    else:
        names = list(DEFAULT_NAMES)
        lastnames = list(DEFAULT_LASTNAMES)
    run_every_list = align_list(run_every_list, len(names))

    cli_models = []
    if args.models:
        cli_models = [x.strip() for x in args.models.split(",") if x.strip()]

    if cli_models:
        use_many_models = True
        model_names = cli_models[: len(names)]
        if len(model_names) < len(names):
            model_names += [model_names[-1]] * (len(names) - len(model_names))
    elif use_many_models:
        model_names = [
            os.getenv("MODEL_1", model_default),
            os.getenv("MODEL_2", "deepseek-chat"),
        ]
        model_names = model_names[: len(names)]
        if len(model_names) < len(names):
            model_names += [model_names[-1]] * (len(names) - len(model_names))
    else:
        model_names = [model_default] * len(names)

    config = {
        "run_every_minutes": run_every_list,
        "use_many_models": use_many_models,
        "names": names,
        "lastnames": lastnames,
        "model_names": model_names,
        "once": bool(args.once),
        "log_level": args.log_level or os.getenv("LOG_LEVEL"),
        "account_type": account_type,
        "timeout_seconds": int(timeout_seconds),
        "force_run": bool(args.force_run),
    }
    print("config : ", config)
    return config


def apply_account_type(names: List[str], account_type: str) -> None:
    for name in names:
        try:
            Account.get(name).set_account_type(account_type)
        except Exception as e:
            log.warning(f"Failed to set account_type for {name}: {e}")


def apply_myr_accounts(names: List[str]) -> None:
    if not names:
        return
    summary = assign_myr_accounts_to_traders(names)
    if summary.get("created_accounts"):
        log.info(
            "Created MYR accounts: %s",
            ", ".join(summary["created_accounts"]),
        )
    if summary.get("assignments"):
        mapping = ", ".join(
            f"{a['trader']}->{a['myr_account']}" for a in summary["assignments"]
        )
        log.info("Assigned MYR accounts: %s", mapping)


def apply_portfolio_holdings(names: List[str], raw: str | None) -> None:
    if raw is None:
        return
    holdings_by_trader = parse_portfolio_holdings_arg(raw, len(names))
    if not holdings_by_trader:
        return
    summary = assign_portfolio_holdings_to_traders(names, holdings_by_trader)
    if summary.get("assignments"):
        mapping = ", ".join(
            f"{a['trader']}({len(a['assets'])} assets)" for a in summary["assignments"]
        )
        log.info("Assigned portfolio holdings: %s", mapping)


def apply_strategies(names: List[str], raw: str | None) -> None:
    if not names or raw is None:
        return
    entries = parse_strategies_arg(raw, len(names))
    if not entries:
        return
    for name, entry in zip(names, entries):
        resolved = resolve_strategy_entry(entry)
        if resolved is None:
            continue
        try:
            Account.get(name).change_strategy(resolved)
            label = entry.strip() or "cleared"
            log.info("Strategy set for %s (%s).", name, label)
        except Exception as e:
            log.warning("Failed to set strategy for %s: %s", name, e)


def apply_default_strategies(names: List[str]) -> None:
    if not names:
        return
    for name in names:
        try:
            acc = Account.get(name)
            if str(acc.strategy or "").strip():
                continue
            key = name.strip().lower()
            default = STRATEGY_REGISTRY.get(key)
            if not default:
                continue
            acc.change_strategy(default)
            log.info("Default strategy applied for %s (%s).", name, key)
        except Exception as e:
            log.warning("Failed to apply default strategy for %s: %s", name, e)


def run_admin_myr_balances(raw: str, log_level: str | None) -> None:
    setup_logging(log_level)
    balances = parse_myr_balances_arg(raw)
    if not balances:
        raise ValueError("--myr-balances requires at least one value.")

    summary = distribute_myr_balances(balances)
    reset = summary["reset"]

    log.info(
        "Admin MYR reset: renamed=%s moved_total=%s move_count=%s myr0_id=%s",
        reset["renamed"],
        reset["moved_total"],
        reset["move_count"],
        reset["myr0_account_id"],
    )
    if summary["created_accounts"]:
        log.info(
            "Admin MYR accounts created: %s",
            ", ".join(summary["created_accounts"]),
        )
    log.info(
        "Admin MYR distribution: total=%s move_count=%s remaining=%s myr0_id=%s",
        summary["distribution_total"],
        summary["move_count"],
        summary["remaining_balance"],
        summary["myr0_account_id"],
    )


def show_portfolios(names: List[str], mode_filter: str | None) -> None:
    if not names:
        print("No accounts found in DB.")
        return
    print("Trader portfolios:")
    client = Client()
    counter = get_counter_currency().upper()
    printed = 0
    for name in names:
        data = read_account(name)
        if not data:
            print(f"- {name}: (not found in DB)")
            continue
        balance = float(data.get("balance") or 0.0)
        holdings = data.get("holdings") or {}
        strategy = str(data.get("strategy") or "").strip()
        account_type = str(data.get("account_type") or "unknown")
        if mode_filter and account_type != mode_filter:
            continue
        if holdings:
            holdings_text = ", ".join(
                f"{asset}:{qty}" for asset, qty in sorted(holdings.items())
            )
        else:
            holdings_text = "(empty)"
        print(
            f"- {name} ({account_type}): MYR={balance:.2f} | holdings={holdings_text}"
        )
        preview = _strategy_preview(strategy)
        if preview:
            print(f"  strategy: {preview}")
        else:
            print("  strategy: (empty)")
        total_value = _compute_portfolio_value(balance, holdings, client, counter)
        if total_value is None:
            print("  total=NA")
        else:
            print(f"  total=RM {total_value:.2f}")
        printed += 1
    if mode_filter and printed == 0:
        print(f"(no accounts with account_type={mode_filter})")


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def show_luno_balances() -> None:
    client = get_luno_client()
    res = client.get_balances()
    rows = res.get("balance", [])
    if not isinstance(rows, list) or not rows:
        print("No balances returned from Luno.")
        return
    counter = get_counter_currency().upper()
    holdings_totals: dict[str, Decimal] = {}
    print("Luno balances:")
    for row in rows:
        asset = str(row.get("asset", "")).upper()
        name = str(row.get("name", "")).strip()
        account_id = str(row.get("account_id", "")).strip()
        balance_raw = row.get("balance") or "0"
        reserved_raw = row.get("reserved") or "0"
        try:
            balance = Decimal(str(balance_raw))
        except InvalidOperation:
            balance = Decimal("0")
        try:
            reserved = Decimal(str(reserved_raw))
        except InvalidOperation:
            reserved = Decimal("0")
        available = balance - reserved
        if available < 0:
            available = Decimal("0")
        if asset and asset != counter and available > 0:
            holdings_totals[asset] = holdings_totals.get(asset, Decimal("0")) + available
        label = asset or "UNKNOWN"
        if name:
            label += f" ({name})"
        print(
            f"- {label}: id={account_id} balance={balance} reserved={reserved} available={available}"
        )
    if holdings_totals:
        holdings_parts = [
            f"{asset}:{_format_decimal(qty)}"
            for asset, qty in sorted(holdings_totals.items())
        ]
        holdings_text = ",".join(holdings_parts)
        print(f"Total holdings (for --holdings): {holdings_text}")
    else:
        print("Total holdings (for --holdings): (empty)")


def _compute_portfolio_value(
    balance: float,
    holdings: dict[str, float],
    client: Client,
    counter: str,
) -> float | None:
    try:
        total = Decimal(str(balance))
    except (InvalidOperation, ValueError):
        total = Decimal("0")

    priced_assets = 0
    for asset, qty in holdings.items():
        if qty <= 0:
            continue
        market_id = f"{asset}{counter}"
        try:
            ticker = client.get_ticker(pair=market_id)
            bid = Decimal(str(ticker["bid"]))
        except Exception:
            continue
        total += Decimal(str(qty)) * bid
        priced_assets += 1

    if holdings and priced_assets == 0:
        return None
    return float(total)


def _strategy_preview(strategy: str, max_len: int = 120) -> str:
    parts = [
        line.strip() for line in str(strategy or "").splitlines() if line.strip()
    ]
    text = " ".join(parts)
    if not text:
        return ""
    if max_len < 4:
        return text[:max_len]
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


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


async def run_trader_safe(
    trader: Trader, interval_minutes: int, force_run: bool = False
) -> float | None:
    """
    Run trader with:
    - jitter to avoid burst
    - timeout guard to prevent hangs
    - cooldown guard to avoid repeated runs after restart
    """
    cooldown_seconds = max(0, int(interval_minutes * 60)) if interval_minutes else 0
    acc = Account.get(trader.name)
    if cooldown_seconds > 0 and not force_run:
        remaining = acc.cooldown_remaining_seconds(cooldown_seconds)
        if remaining > 0:
            remaining_display = int(math.ceil(remaining))
            log.info(
                f"{trader.name} cooldown active ({remaining_display}s remaining)."
            )
            return remaining

    jitter = random.uniform(0, JITTER_SECONDS)
    await asyncio.sleep(jitter)

    acc.mark_run()
    start = time.monotonic()
    try:
        await asyncio.wait_for(trader.run(), timeout=TRADER_TIMEOUT_SECONDS)
        dur = time.monotonic() - start
        log.info(f"✅ {trader.name} finished in {dur:.2f}s (jitter={jitter:.2f}s)")
    except asyncio.TimeoutError:
        log.error(f"⏱ {trader.name} TIMEOUT after {TRADER_TIMEOUT_SECONDS}s")
    except Exception as e:
        log.exception(f"❌ {trader.name} crashed: {e}")


    return None

async def _wait_with_countdown(
    name: str, seconds: float, stop_event: asyncio.Event | None = None
) -> bool:
    remaining = int(max(0, math.ceil(seconds)))
    if remaining <= 0:
        return False
    while remaining > 0:
        if stop_event is not None and stop_event.is_set():
            return True
        if remaining >= 60:
            label = f"{remaining // 60}m {remaining % 60}s"
        else:
            label = f"{remaining}s"
        log.info(f"{name} cooldown countdown: {label} remaining")
        step = 60 if remaining > 60 else remaining
        if stop_event is None:
            await asyncio.sleep(step)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=step)
                return True
            except asyncio.TimeoutError:
                pass
        remaining -= step
    return stop_event.is_set() if stop_event is not None else False


async def _run_trader_once_with_cooldown(
    trader: Trader, interval_minutes: int, force_run: bool
) -> None:
    force_next = force_run
    while True:
        remaining = await run_trader_safe(trader, interval_minutes, force_next)
        force_next = False
        if remaining is None or remaining <= 0:
            return
        await _wait_with_countdown(trader.name, remaining)


async def run_trader_loop(
    trader: Trader,
    interval_minutes: int,
    stop_event: asyncio.Event,
    force_run: bool = False,
) -> None:
    force_next = force_run
    while not stop_event.is_set():
        remaining = await run_trader_safe(trader, interval_minutes, force_next)
        force_next = False
        try:
            if remaining is not None and remaining > 0:
                should_stop = await _wait_with_countdown(
                    trader.name, remaining, stop_event
                )
                if should_stop:
                    return
                continue
            wait_seconds = max(1.0, interval_minutes * 60)
            await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            pass


async def run_one_cycle(
    traders: List[Trader], run_every_list: List[int], force_run: bool
) -> None:
    log.info(f"🫀 HEARTBEAT start | traders={len(traders)} | t={utc_now()}")

    await asyncio.gather(
        *[
            _run_trader_once_with_cooldown(t, m, force_run=force_run)
            for t, m in zip(traders, run_every_list)
        ],
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
    run_every_list = cfg["run_every_minutes"]
    if isinstance(run_every_list, int):
        run_every_list = [run_every_list] * len(traders)
    run_every_list = align_list([int(x) for x in run_every_list], len(traders))

    log.info(
        f"Scheduler started | jitter<={JITTER_SECONDS}s | timeout={TRADER_TIMEOUT_SECONDS}s"
    )
    if cfg.get("force_run"):
        log.info("Force-run enabled: initial cooldown bypassed.")
    log.info(f"Run mode: {cfg['account_type']}")
    log.info(f"Traders: {', '.join([t.name for t in traders])}")
    if run_every_list:
        if len(set(run_every_list)) == 1:
            log.info(f"Interval: every={run_every_list[0]}min")
        else:
            schedule = ", ".join(
                f"{t.name}={m}m" for t, m in zip(traders, run_every_list)
            )
            log.info(f"Intervals: {schedule}")

    stop_event = asyncio.Event()

    def _stop(*_):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _stop())

    if cfg["once"]:
        await run_one_cycle(traders, run_every_list, cfg.get("force_run", False))
        log.info("Scheduler stopped.")
        return

    tasks = [
        asyncio.create_task(
            run_trader_loop(t, m, stop_event, force_run=cfg.get("force_run", False))
        )
        for t, m in zip(traders, run_every_list)
    ]

    await stop_event.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("Scheduler stopped.")



def main() -> None:
    load_env()
    args = parse_args()

    if args.myr_balances is not None:
        try:
            run_admin_myr_balances(args.myr_balances, args.log_level)
        except Exception as e:
            log.exception(f"Admin MYR balance distribution failed: {e}")
            raise
        return
    if args.show_luno_balances:
        show_luno_balances()
        return
    if args.show_portfolio:
        if args.live_only and args.dry_run_only:
            raise ValueError("Use only one of --live-only or --dry-run-only.")
        mode_filter = None
        if args.live_only:
            mode_filter = "live"
        elif args.dry_run_only:
            mode_filter = "dry_run"
        names = resolve_portfolio_names(args)
        show_portfolios(names, mode_filter)
        return
    cfg = resolve_config(args)

    global TRADER_TIMEOUT_SECONDS
    TRADER_TIMEOUT_SECONDS = cfg["timeout_seconds"]

    setup_logging(cfg["log_level"])
    apply_myr_accounts(cfg["names"])
    apply_portfolio_holdings(cfg["names"], args.holdings)
    apply_strategies(cfg["names"], args.strategies)
    apply_default_strategies(cfg["names"])
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
