# app/libs/account.py

"""
Account & Trading Engine (Luno MYR Markets)

This module defines the Account model and all trading-related logic
for live trading and portfolio simulation.

Key concepts:
- Counter currency is fixed (MYR by default).
- All markets traded must end with the counter currency (e.g. GRTMYR).
- account_type="live" sends real orders to Luno.
- account_type="dry_run" sends no orders.
- Portfolio (balance/holdings) is stored per trader in DB.

Design goals:
- Safe for AI agents (no hard crashes on expected conditions)
- Deterministic, structured outputs via TradeResult (Pydantic)
- Portfolio is isolated per trader for simulations and allocation

Typical agent flow:
1) account.refresh_from_luno()        # sync live balances
2) account.paper_reset_from_luno()    # initialize paper wallet
3) estimate = account.get_estimate_qty(market_id, spend_myr)
4) buy_pair(...) or sell_pair(...)
"""

from __future__ import annotations

from pydantic import BaseModel
from dotenv import load_dotenv

from app.libs.database import write_account, read_account, write_log
from app import get_luno_admin_client, get_luno_client, get_counter_currency

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional, Literal
from uuid import uuid4

import re

import pandas as pd

load_dotenv(override=True)
INITIAL_BALANCE = 0.00
MAX_MYR_TRADERS = 9
MYR_ACCOUNT_PREFIX = "MYR_"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts_ms_to_iso(ts: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _normalize_market_id(market_id: str) -> str:
    s = market_id.strip().upper()
    return s.replace("/", "").replace("_", "").replace("-", "")


def _assert_counter_market(market_id: str) -> str:
    """
    Enforce we only trade markets ending with our counter currency (default MYR).
    """
    m = _normalize_market_id(market_id)
    counter = get_counter_currency().upper()
    if not m.endswith(counter):
        raise ValueError(f"Only {counter} markets allowed. Got: {m}")
    return m


def _base_asset_from_market(market_id: str) -> str:
    """
    For counter markets, base asset = market_id without counter currency suffix.
    e.g. GRTMYR -> GRT
    """
    m = _assert_counter_market(market_id)
    counter = get_counter_currency().upper()
    return m[: -len(counter)]


def _to_decimal(x: Any) -> Decimal:
    return Decimal(str(x))


def _quantize_down(value: Decimal, scale: int) -> Decimal:
    """
    Round DOWN to a given decimal scale.
    scale=2 -> 0.01
    """
    if scale < 0:
        raise ValueError("scale must be >= 0")
    q = Decimal("1").scaleb(-scale)
    return value.quantize(q, rounding=ROUND_DOWN)


def _parse_available(balance_str: Any, reserved_str: Any) -> float:
    """
    Convert Luno (balance, reserved) -> available = max(balance - reserved, 0)
    """
    bal = float(balance_str or 0)
    res = float(reserved_str or 0)
    return max(bal - res, 0.0)


def _get_manage_client():
    try:
        return get_luno_admin_client(), True
    except Exception:
        return get_luno_client(), False


def _normalize_name(raw: Any) -> str:
    return str(raw or "").strip()


def _sanitize_client_order_prefix(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return "trader"
    # Luno allows alphanumeric plus _ ; , . - in client_order_id
    sanitized = re.sub(r"[^A-Za-z0-9_.;,-]+", "_", text)
    sanitized = sanitized.strip("_")
    return sanitized or "trader"


def _extract_float_field(data: dict[str, Any], keys: list[str]) -> float | None:
    for key in keys:
        if key not in data:
            continue
        value = data.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_str_field(data: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        if key not in data:
            continue
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _normalize_order_state(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_order_side(value: Any) -> str | None:
    side = str(value or "").strip().upper()
    if side in {"BUY", "BID"}:
        return "BUY"
    if side in {"SELL", "ASK"}:
        return "SELL"
    return None


def _is_filled_state(state: str | None) -> bool:
    return state in {"COMPLETE", "COMPLETED", "FILLED", "FILLED_FULL", "DONE"}


def _is_terminal_order_state(state: str | None) -> bool:
    if not state:
        return False
    return _is_filled_state(state) or state in {"CANCELLED", "CANCELED"}


def _load_currency_accounts(client: Any, currency: str) -> list[dict[str, Any]]:
    res = client.get_balances()
    rows = res.get("balance", [])
    if not isinstance(rows, list):
        raise ValueError("Unexpected balance response from Luno.")
    accounts: list[dict[str, Any]] = []
    for row in rows:
        asset = str(row.get("asset", "")).upper()
        if asset != currency:
            continue
        account = dict(row)
        account["account_id"] = str(account.get("account_id", "")).strip()
        account["name"] = _normalize_name(account.get("name"))
        accounts.append(account)
    if not accounts:
        raise ValueError(f"No {currency} accounts returned from Luno.")
    return accounts


def _index_accounts_by_name(
    accounts: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    name_map: dict[str, dict[str, Any]] = {}
    duplicates = set()
    for acct in accounts:
        name = acct.get("name", "")
        if not name:
            continue
        key = name.upper()
        if key in name_map:
            duplicates.add(key)
            continue
        name_map[key] = acct
    if duplicates:
        dup_names = ", ".join(sorted(duplicates))
        raise ValueError(f"Duplicate MYR account names: {dup_names}")
    return name_map


def _ensure_named_accounts(
    client: Any, currency: str, required_names: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    accounts = _load_currency_accounts(client, currency)
    name_map = _index_accounts_by_name(accounts)
    missing = [n for n in required_names if n.upper() not in name_map]
    if not missing:
        return accounts, []

    if len(accounts) + len(missing) > 10:
        raise ValueError("Creating accounts would exceed Luno limit of 10 per currency.")

    for name in missing:
        client.create_account(currency, name)

    accounts = _load_currency_accounts(client, currency)
    return accounts, missing


def assign_myr_accounts_to_traders(names: list[str]) -> dict[str, Any]:
    """
    Ensure MYR_{i} accounts exist and assign each trader to its index account.
    """
    if not names:
        return {"created_accounts": [], "assignments": [], "used_admin": False}
    if len(names) > MAX_MYR_TRADERS:
        raise ValueError(
            f"Too many traders: {len(names)} (max {MAX_MYR_TRADERS})"
        )

    client, used_admin = _get_manage_client()
    currency = get_counter_currency().upper()

    required_names = [
        f"{MYR_ACCOUNT_PREFIX}{i}" for i in range(1, len(names) + 1)
    ]
    accounts, created_accounts = _ensure_named_accounts(
        client, currency, required_names
    )

    name_map = _index_accounts_by_name(accounts)
    assignments = []
    for idx, trader_name in enumerate(names, 1):
        target_name = f"{MYR_ACCOUNT_PREFIX}{idx}"
        account = name_map.get(target_name.upper())
        if account is None:
            raise ValueError(f"Missing {target_name} account.")
        account_id = str(account.get("account_id", "")).strip()
        if not account_id:
            raise ValueError(f"Missing account_id for {target_name}.")
        balance = _parse_available(account.get("balance"), account.get("reserved"))

        acc = Account.get(trader_name)
        acc.account_id = account_id
        acc.balance = float(balance)
        acc.paper_balance = float(balance)
        acc.save()

        assignments.append(
            {
                "trader": trader_name,
                "myr_account": target_name,
                "account_id": account_id,
                "balance": float(balance),
            }
        )

    return {
        "created_accounts": created_accounts,
        "assignments": assignments,
        "used_admin": used_admin,
    }


def _parse_holdings_block(raw: str) -> dict[str, float]:
    holdings: dict[str, float] = {}
    text = str(raw or "").strip()
    if not text:
        return holdings
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"Invalid holding entry '{item}'. Use ASSET:QTY.")
        asset, qty_str = item.split(":", 1)
        asset = asset.strip().upper()
        if not asset:
            raise ValueError(f"Invalid holding entry '{item}'. Missing asset.")
        try:
            qty = float(qty_str)
        except ValueError as exc:
            raise ValueError(f"Invalid holding quantity '{qty_str}'.") from exc
        if qty < 0:
            raise ValueError(f"Negative holding not allowed: {asset}={qty}")
        if qty == 0:
            continue
        holdings[asset] = float(holdings.get(asset, 0.0)) + float(qty)
    return holdings


def parse_portfolio_holdings_arg(
    raw: str | None, trader_count: int
) -> list[dict[str, float]]:
    if raw is None or trader_count <= 0:
        return []
    text = str(raw).strip()
    if not text:
        return [{} for _ in range(trader_count)]
    blocks = [b.strip() for b in text.split(";")]
    if len(blocks) == 1 and trader_count > 1:
        blocks = blocks * trader_count
    elif len(blocks) < trader_count:
        blocks = blocks + [blocks[-1]] * (trader_count - len(blocks))
    elif len(blocks) > trader_count:
        raise ValueError(
            f"Too many holdings groups: {len(blocks)} (traders={trader_count})"
        )
    return [_parse_holdings_block(block) for block in blocks]


def assign_portfolio_holdings_to_traders(
    names: list[str], holdings_by_trader: list[dict[str, float]]
) -> dict[str, Any]:
    if not names or not holdings_by_trader:
        return {"assignments": []}
    if len(names) != len(holdings_by_trader):
        raise ValueError("Holdings list must match trader list length.")

    assignments: list[dict[str, Any]] = []
    for name, holdings in zip(names, holdings_by_trader):
        acc = Account.get(name)
        acc.holdings = dict(holdings)
        acc.paper_holdings = dict(holdings)
        acc.paper_balance = float(acc.balance)
        acc.save()
        assignments.append(
            {
                "trader": name,
                "assets": sorted(holdings.keys()),
            }
        )
    return {"assignments": assignments}


class Transaction(BaseModel):
    market_id: str
    side: Literal["BUY", "SELL"]
    quantity: float  # base asset units intended
    price: float  # last_trade snapshot (not guaranteed fill)
    timestamp: str
    rationale: str

    def total(self) -> float:
        return self.quantity * self.price

    def __repr__(self):
        return (
            f"{self.side} {abs(self.quantity)} "
            f"{self.market_id[:-3]} via {self.market_id} @ {self.price}"
        )


class TradeResult(BaseModel):
    ok: bool
    action: Literal["ESTIMATE_BUY_QTY", "BUY", "SELL"]
    market_id: str

    # Success fields
    quantity: Optional[float] = None  # base quantity
    spend_myr: Optional[float] = None  # requested spend (estimate tool)
    est_cost: Optional[float] = None  # estimated MYR cost (buy)
    est_proceeds: Optional[float] = None  # estimated MYR proceeds (sell)
    bid: Optional[float] = None
    ask: Optional[float] = None
    last_trade: Optional[float] = None
    spread_pct: Optional[float] = None
    order: Optional[Any] = None
    client_order_id: Optional[str] = None

    # Failure fields
    reason: Optional[str] = None
    have: Optional[float] = None
    want: Optional[float] = None
    suggestion: Optional[str] = None


class OrderResult(BaseModel):
    ok: bool
    action: Literal["POST_LIMIT", "CANCEL_ORDER", "GET_ORDER", "LIST_ORDERS"]
    market_id: Optional[str] = None
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    order: Optional[Any] = None
    orders: Optional[Any] = None
    have: Optional[float] = None
    want: Optional[float] = None
    reason: Optional[str] = None
    suggestion: Optional[str] = None


class LimitSizeResult(BaseModel):
    ok: bool
    action: Literal["MAX_LIMIT_BUY_QTY", "MAX_LIMIT_SELL_QTY"]
    market_id: str
    quantity: Optional[float] = None
    price: Optional[float] = None
    have: Optional[float] = None
    reason: Optional[str] = None
    suggestion: Optional[str] = None


class Account(BaseModel):
    """
    Represents a trading account that can operate in either LIVE or DRY_RUN mode.

    LIVE mode:
    - Sends real MARKET orders via Luno API

    DRY_RUN mode:
    - No orders sent to Luno

    Portfolio:
    - balance / holdings are stored per trader in the DB
    - trades update the portfolio in the DB

    Fields:
        name:
            Logical account name (used as DB key).

        account_id:
            Counter currency account_id from Luno (e.g. MYR account).

        balance:
            Portfolio counter currency balance stored in DB.

        holdings:
            Portfolio asset holdings stored in DB.

        paper_balance:
            Legacy mirror of portfolio balance.

        paper_holdings:
            Legacy mirror of portfolio holdings.

        account_type:
            Execution mode: "dry_run" or "live".

        last_run:
            Last run start time in UTC ISO format (for cooldown checks).

        transactions:
            History of intended trades (not guaranteed fills).

        orders:
            Stored live order records (market + limit).

        orders_history:
            Archived completed/canceled orders (live + paper).

        paper_orders:
            Stored paper order records.

    IMPORTANT:
    - Expected trade failures (insufficient balance, wide spread)
      return TradeResult(ok=False) instead of raising exceptions.
    """

    name: str
    account_id: str
    balance: float
    strategy: str
    holdings: dict[str, float]
    transactions: list[Transaction]
    orders: list[dict[str, Any]]
    orders_history: list[dict[str, Any]]
    portfolio_value_time_series: list[tuple[str, float]]
    account_type: str  # "dry_run" or "live"
    last_run: Optional[str] = None

    # Portfolio mirror (legacy)
    paper_balance: float
    paper_holdings: dict[str, float]
    paper_orders: list[dict[str, Any]]

    _market_rules_cache: dict[str, dict[str, Any]] = {}

    @classmethod
    def get(cls, name: str):
        fields = read_account(name.lower())
        if not fields:
            fields = {
                "name": name.lower(),
                "account_id": "",
                "balance": INITIAL_BALANCE,
                "strategy": "",
                "holdings": {},
                "transactions": [],
                "orders": [],
                "orders_history": [],
                "portfolio_value_time_series": [],
                "account_type": "dry_run",
                "last_run": None,
                # paper defaults
                "paper_balance": INITIAL_BALANCE,
                "paper_holdings": {},
                "paper_orders": [],
            }
            write_account(name, fields)

        # backward compat: if old DB record doesn’t have these keys
        fields.setdefault("account_type", "dry_run")
        fields.setdefault(
            "paper_balance", float(fields.get("balance", INITIAL_BALANCE))
        )
        fields.setdefault("paper_holdings", dict(fields.get("holdings", {})))
        fields.setdefault("orders", [])
        fields.setdefault("orders_history", [])
        fields.setdefault("paper_orders", [])
        fields.setdefault("last_run", None)
        if not isinstance(fields.get("orders"), list):
            fields["orders"] = []
        if not isinstance(fields.get("orders_history"), list):
            fields["orders_history"] = []
        if not isinstance(fields.get("paper_orders"), list):
            fields["paper_orders"] = []
        return cls(**fields)

    def save(self):
        write_account(self.name.lower(), self.model_dump())

    def set_account_type(self, account_type: str) -> str:
        """
        Set the account execution mode.

        Allowed values:
        - "dry_run": simulate trades only (no orders sent to Luno)
        - "live": execute real trades on Luno
        """
        allowed = {"dry_run", "live"}
        if not isinstance(account_type, str):
            raise TypeError("account_type must be a string")
        normalized = account_type.strip().lower()
        if normalized not in allowed:
            raise ValueError(
                f"Invalid account_type '{normalized}'. Allowed: {sorted(allowed)}"
            )

        self.account_type = normalized
        self.save()
        write_log(self.name, "account", f"Account type set to {normalized}")
        return f"Account type set to {normalized}"

    # ---------- strategy ----------
    def change_strategy(self, strategy: str) -> str:
        """At your discretion, if you choose to, call this to change your investment strategy for the future."""
        self.strategy = strategy
        self.save()
        write_log(self.name, "account", f"Changed strategy -> {self.strategy}")
        return "Changed strategy"

    def get_strategy(self) -> str:
        return self.strategy or ""

    # ---------- reporting ----------
    def report(self) -> str:
        balance = self.balance
        holdings = self.holdings
        wallet_label = "portfolio"

        holdings_lines = [f"- {a}: {q}" for a, q in sorted(holdings.items())]
        holdings_text = "\n".join(holdings_lines) if holdings_lines else "- (empty)"
        cc = get_counter_currency().upper()
        archived_count = len(self.orders_history)
        archived_recent = self.orders_history[-3:] if archived_count else []
        archived_text = f"{archived_count}"
        if archived_recent:
            recent_labels = []
            for record in archived_recent:
                oid = record.get("order_id") or record.get("client_order_id") or "?"
                state = record.get("state") or "?"
                recent_labels.append(f"{oid}:{state}")
            archived_text = f"{archived_text} (latest: {', '.join(recent_labels)})"

        return (
            f"Account: **{self.name}**\n"
            f"- Strategy: `{self.strategy}`\n"
            f"- {cc} Available ({wallet_label}): **{balance:.2f}**\n"
            f"- {cc} account_id: `{self.account_id}`\n"
            f"- Holdings ({wallet_label}):\n{holdings_text}\n"
            f"- Transactions: {len(self.transactions)}\n"
            f"- Archived orders: {archived_text}\n"
            f"- account_type: `{self.account_type}`"
        )

    def cooldown_remaining_seconds(self, cooldown_seconds: float) -> float:
        if cooldown_seconds <= 0:
            return 0.0
        last_run = _parse_iso_datetime(self.last_run)
        if last_run is None:
            return 0.0
        now = datetime.now(timezone.utc)
        elapsed = (now - last_run).total_seconds()
        remaining = cooldown_seconds - elapsed
        return remaining if remaining > 0 else 0.0

    def mark_run(self) -> None:
        self.last_run = _utc_now_iso()
        self.save()

    # ---------------- Wallet selectors ----------------
    def _wallet_balance(self) -> float:
        return self.balance

    def _wallet_holdings(self) -> dict[str, float]:
        return self.holdings

    def _set_wallet_balance(self, v: float) -> None:
        self.balance = v

    def _set_wallet_holdings(self, h: dict[str, float]) -> None:
        self.holdings = h

    def _counter_account_id(self) -> str | None:
        account_id = str(self.account_id or "").strip()
        return account_id if account_id else None

    # ---------------- Order tracking helpers ----------------
    def _make_client_order_id(self) -> str:
        prefix = _sanitize_client_order_prefix(self.name)
        unique = str(uuid4())
        max_prefix_len = 255 - len(unique) - 1
        if max_prefix_len < 1:
            prefix = "trader"
        else:
            prefix = prefix[:max_prefix_len]
        return f"{prefix}-{unique}"

    def _order_store(self) -> list[dict[str, Any]]:
        return self.orders if self.account_type == "live" else self.paper_orders

    def _find_order_record(
        self, order_id: str | None = None, client_order_id: str | None = None
    ) -> dict[str, Any] | None:
        if order_id is None and client_order_id is None:
            return None
        for record in self._order_store():
            if order_id and str(record.get("order_id") or "").strip() == str(order_id):
                return record
            if client_order_id and str(record.get("client_order_id") or "").strip() == str(client_order_id):
                return record
        return None

    def _ensure_order_tracking_fields(self, record: dict[str, Any]) -> None:
        record.setdefault("applied_volume", 0.0)
        record.setdefault("applied_counter", 0.0)
        record.setdefault("filled_volume", 0.0)
        record.setdefault("filled_counter", 0.0)
        record.setdefault("applied_trade_ids", [])

    def _archive_terminal_orders(self) -> int:
        moved = 0

        def _archive_store(store: list[dict[str, Any]]) -> None:
            nonlocal moved
            if not store:
                return
            remaining = []
            moved_here = 0
            for record in store:
                state = _normalize_order_state(record.get("state"))
                if _is_terminal_order_state(state):
                    archived = dict(record)
                    archived["archived_at"] = _utc_now_iso()
                    self.orders_history.append(archived)
                    moved_here += 1
                else:
                    remaining.append(record)
            if moved_here:
                store[:] = remaining
                moved += moved_here

        _archive_store(self.orders)
        _archive_store(self.paper_orders)
        return moved

    def _apply_order_fill(
        self, record: dict[str, Any], order_data: dict[str, Any]
    ) -> bool:
        if not isinstance(order_data, dict):
            return False

        self._ensure_order_tracking_fields(record)

        state = _normalize_order_state(
            order_data.get("state") or order_data.get("status") or record.get("state")
        )
        if state:
            record["state"] = state

        side = record.get("side") or _normalize_order_side(
            order_data.get("type") or order_data.get("side")
        )
        if side:
            record["side"] = side

        market_id = record.get("market_id") or _extract_str_field(
            order_data, ["pair", "market_id"]
        )
        if market_id:
            market_id = _normalize_market_id(market_id)
            record["market_id"] = market_id

        if not market_id or side not in {"BUY", "SELL"}:
            return False

        filled_base = _extract_float_field(
            order_data, ["base", "base_amount", "filled_volume", "filled_base"]
        )
        if filled_base is None and _is_filled_state(state):
            try:
                filled_base = float(record.get("volume") or 0.0)
            except (TypeError, ValueError):
                filled_base = None

        if filled_base is None or filled_base <= 0:
            return False

        applied_base = float(record.get("applied_volume") or 0.0)
        delta_base = float(filled_base) - applied_base
        if delta_base <= 0:
            return False

        filled_counter = _extract_float_field(
            order_data, ["counter", "counter_amount", "filled_counter"]
        )
        applied_counter = float(record.get("applied_counter") or 0.0)
        delta_counter = None
        if filled_counter is not None:
            delta_counter = float(filled_counter) - applied_counter
            if delta_counter <= 0:
                delta_counter = None

        price = None
        if delta_counter is not None and delta_base > 0:
            price = delta_counter / delta_base
        if price is None:
            price = _extract_float_field(order_data, ["limit_price", "price"])
        if price is None:
            try:
                price = float(record.get("price") or 0.0)
            except (TypeError, ValueError):
                price = 0.0

        try:
            base_asset = _base_asset_from_market(market_id)
        except ValueError:
            return False

        try:
            if side == "BUY":
                cost = delta_counter if delta_counter is not None else delta_base * price
                self._apply_portfolio_buy(base_asset, delta_base, float(cost))
            else:
                proceeds = (
                    delta_counter if delta_counter is not None else delta_base * price
                )
                self._apply_portfolio_sell(base_asset, delta_base, float(proceeds))
        except ValueError as exc:
            write_log(
                self.name,
                "order",
                f"Skipped fill apply for {record.get('order_id') or record.get('client_order_id')}: {exc}",
            )
            return False

        record["applied_volume"] = applied_base + delta_base
        if delta_counter is not None:
            record["applied_counter"] = applied_counter + delta_counter

        record["filled_volume"] = max(
            float(record.get("filled_volume") or 0.0), float(filled_base)
        )
        if filled_counter is not None:
            record["filled_counter"] = max(
                float(record.get("filled_counter") or 0.0), float(filled_counter)
            )

        rationale = str(record.get("rationale") or "").strip()
        self.transactions.append(
            Transaction(
                market_id=market_id,
                side=side,
                quantity=float(delta_base),
                price=float(price),
                timestamp=_utc_now_iso(),
                rationale=rationale,
            )
        )

        write_log(
            self.name,
            "trade",
            f"{self.account_type.upper()} LIMIT {side} fill qty={delta_base} {market_id} price={price:.8f}",
        )
        return True

    def _update_order_record(
        self, record: dict[str, Any], order_data: dict[str, Any]
    ) -> bool:
        if not isinstance(order_data, dict):
            return False

        updated = False
        self._ensure_order_tracking_fields(record)

        order_id = _extract_str_field(order_data, ["order_id", "id"])
        if order_id and record.get("order_id") != order_id:
            record["order_id"] = order_id
            updated = True

        client_order_id = _extract_str_field(order_data, ["client_order_id"])
        if client_order_id and record.get("client_order_id") != client_order_id:
            record["client_order_id"] = client_order_id
            updated = True

        state = _normalize_order_state(order_data.get("state") or order_data.get("status"))
        if state and record.get("state") != state:
            record["state"] = state
            updated = True

        market_id = _extract_str_field(order_data, ["pair", "market_id"])
        if market_id:
            market_id = _normalize_market_id(market_id)
            if record.get("market_id") != market_id:
                record["market_id"] = market_id
                updated = True

        side = _normalize_order_side(order_data.get("type") or order_data.get("side"))
        if side and record.get("side") != side:
            record["side"] = side
            updated = True

        price = _extract_float_field(order_data, ["limit_price", "price"])
        if price is not None:
            record["price"] = float(price)

        volume = _extract_float_field(order_data, ["limit_volume", "volume", "base_volume"])
        if volume is not None:
            record["volume"] = float(volume)

        filled_base = _extract_float_field(
            order_data, ["base", "base_amount", "filled_volume", "filled_base"]
        )
        if filled_base is not None:
            record["filled_volume"] = float(filled_base)

        filled_counter = _extract_float_field(
            order_data, ["counter", "counter_amount", "filled_counter"]
        )
        if filled_counter is not None:
            record["filled_counter"] = float(filled_counter)

        record["updated_at"] = _utc_now_iso()

        if self._apply_order_fill(record, order_data):
            updated = True

        return updated

    # ---------------- Market rules ----------------
    def _get_market_rules(self, market_id: str) -> dict[str, Any]:
        """
        Pull market constraints from Luno markets() for min_volume and volume_scale.
        Cached per market_id.
        """
        m = _assert_counter_market(market_id)
        if m in self._market_rules_cache:
            return self._market_rules_cache[m]

        client = get_luno_client()
        raw = client.markets()
        df = pd.DataFrame(raw.get("markets", []))
        if df.empty:
            raise ValueError("No markets returned from Luno.")

        row = df.loc[df["market_id"] == m]
        if row.empty:
            raise ValueError(f"Market not found: {m}")

        rules = row.iloc[0].to_dict()
        self._market_rules_cache[m] = rules
        return rules

    def _validate_and_round_volume(self, market_id: str, qty: float) -> float:
        """
        Enforce min_volume and volume_scale (round DOWN).
        """
        rules = self._get_market_rules(market_id)
        min_vol = _to_decimal(rules.get("min_volume", "0"))
        vol_scale = int(rules.get("volume_scale", 0))

        q = _quantize_down(_to_decimal(qty), vol_scale)
        if q < min_vol:
            raise ValueError(
                f"Quantity too small for {market_id}. min_volume={min_vol} volume_scale={vol_scale}. "
                f"After rounding down -> {q}"
            )
        return float(q)

    # ---------------- Sync live wallet from Luno (source of truth) ----------------
    def refresh_from_luno(self, sync_holdings: bool = False) -> None:
        """
        Source of truth sync from Luno.
        Updates:
        - self.balance as counter available (balance - reserved)
        - self.account_id from counter row
        - self.holdings for ALL non-counter assets when sync_holdings is True
        """
        client = get_luno_client()
        res = client.get_balances()
        df = pd.DataFrame(res.get("balance", []))
        if df.empty:
            raise ValueError("No balances returned from Luno.")

        counter = get_counter_currency().upper()
        cc_rows = df.loc[df["asset"] == counter]
        if cc_rows.empty:
            raise ValueError(f"{counter} balance not found.")

        target_id = str(self.account_id or "").strip()
        if target_id:
            match = cc_rows.loc[
                cc_rows["account_id"].astype(str) == target_id
            ]
            if match.empty:
                raise ValueError(
                    f"{counter} account_id not found: {target_id}"
                )
            cc_row = match
        else:
            cc_row = cc_rows

        self.account_id = str(cc_row["account_id"].iloc[0])
        self.balance = _parse_available(
            cc_row["balance"].iloc[0], cc_row["reserved"].iloc[0]
        )
        self.paper_balance = float(self.balance)

        if sync_holdings:
            new_holdings: dict[str, float] = {}
            for _, r in df.iterrows():
                asset = str(r.get("asset", "")).upper()
                if not asset or asset == counter:
                    continue
                avail = _parse_available(r.get("balance"), r.get("reserved"))
                if avail > 0:
                    new_holdings[asset] = avail

            self.holdings = new_holdings
            self.paper_holdings = dict(self.holdings)
        self.save()

    # ✅ Initialize/reset paper wallet based on current live balances
    def paper_reset_from_luno(self) -> str:
        """
        Initialize or reset the paper wallet using current LIVE Luno balances.

        Behavior:
        - Reads LIVE balances from Luno (read-only).
        - Copies available MYR into portfolio balance.
        - Copies available assets into portfolio holdings.
        - Mirrors portfolio into paper_balance/paper_holdings.
        - Does NOT send any orders.

        When to use:
        - Before starting a paper trading session.
        - When you want paper trading to reflect current real balances.

        Safe:
        - No trading side effects.
        """

        self.refresh_from_luno(sync_holdings=True)
        self.paper_balance = float(self.balance)
        self.paper_holdings = dict(self.holdings)
        self.save()
        write_log(self.name, "paper", "Paper wallet reset from Luno balances")
        return "Paper wallet reset from Luno balances"

    # ---------------- Portfolio apply ----------------
    def _apply_portfolio_buy(
        self, base_asset: str, qty: float, cost: float
    ) -> None:
        balance = float(self.balance)
        if cost > balance:
            raise ValueError("insufficient balance")
        self.balance = balance - cost
        holdings = dict(self.holdings)
        holdings[base_asset] = float(holdings.get(base_asset, 0.0)) + qty
        # keep tiny dust? optional, keep it
        self.holdings = holdings
        self.paper_balance = float(self.balance)
        self.paper_holdings = dict(self.holdings)

    def _apply_portfolio_sell(
        self, base_asset: str, qty: float, proceeds: float
    ) -> None:
        holdings = dict(self.holdings)
        have = float(holdings.get(base_asset, 0.0))
        if qty > have:
            raise ValueError("insufficient asset")
        holdings[base_asset] = have - qty
        if holdings[base_asset] <= 0:
            holdings.pop(base_asset, None)
        self.holdings = holdings
        self.balance = float(self.balance) + proceeds
        self.paper_balance = float(self.balance)
        self.paper_holdings = dict(self.holdings)

    # ---------------- Estimation (agent sizing) ----------------
    def get_estimate_qty(self, market_id: str, spend_myr: float) -> TradeResult:
        """
        Estimate how much base asset can be bought for a given MYR budget.

        This function MUST be called by an AI agent before buy_pair().

        Why:
        - buy_pair() requires a base-asset quantity.
        - Agents usually reason in MYR budgets.
        - This function converts MYR → quantity safely.

        How estimation works:
        - Uses current ASK price (BUY hits ask).
        - Uses portfolio balance stored in DB.
        - Applies market rules:
            - min_volume
            - volume_scale (rounded DOWN)

        Returns:
            TradeResult with:
            - ok=True and quantity if tradable
            - ok=False with reason and suggestion if not

        Agent pattern:
            est = get_estimate_qty(...)
            if est.ok:
                buy_pair(..., est.quantity)
            else:
                resize or skip trade
        """

        m = _assert_counter_market(market_id)
        if spend_myr <= 0:
            return TradeResult(
                ok=False,
                action="ESTIMATE_BUY_QTY",
                market_id=m,
                reason="INVALID_SPEND",
                want=spend_myr,
                suggestion="spend_myr must be > 0",
            )

        client = get_luno_client()
        ticker = client.get_ticker(pair=m)
        ask = float(ticker["ask"])
        bid = float(ticker["bid"])
        last_trade = float(ticker["last_trade"])

        if ask <= 0:
            return TradeResult(
                ok=False,
                action="ESTIMATE_BUY_QTY",
                market_id=m,
                reason="INVALID_ASK",
                ask=ask,
                bid=bid,
                last_trade=last_trade,
            )

        # portfolio balance stored in DB
        available_ccy = self._wallet_balance()
        if spend_myr > available_ccy:
            return TradeResult(
                ok=False,
                action="ESTIMATE_BUY_QTY",
                market_id=m,
                reason="INSUFFICIENT_MYR",
                have=available_ccy,
                want=spend_myr,
                ask=ask,
                bid=bid,
                last_trade=last_trade,
                suggestion="Reduce spend_myr or adjust portfolio balance",
            )

        raw_qty = spend_myr / ask
        try:
            qty = self._validate_and_round_volume(m, raw_qty)
        except ValueError as e:
            return TradeResult(
                ok=False,
                action="ESTIMATE_BUY_QTY",
                market_id=m,
                reason="BELOW_MIN_VOLUME",
                suggestion=str(e),
                ask=ask,
                bid=bid,
                last_trade=last_trade,
            )

        mid = (ask + bid) / 2 if (ask and bid) else ask
        spread_pct = (ask - bid) / mid if mid else 0.0

        return TradeResult(
            ok=True,
            action="ESTIMATE_BUY_QTY",
            market_id=m,
            quantity=qty,
            spend_myr=spend_myr,
            ask=ask,
            bid=bid,
            last_trade=last_trade,
            spread_pct=spread_pct,
        )

    def get_max_limit_buy_qty(
        self, market_id: str, price: float
    ) -> LimitSizeResult:
        """
        Compute the maximum BUY limit quantity based on available MYR balance.
        """
        m = _assert_counter_market(market_id)
        if price <= 0:
            return LimitSizeResult(
                ok=False,
                action="MAX_LIMIT_BUY_QTY",
                market_id=m,
                price=price,
                reason="INVALID_PRICE",
                suggestion="price must be > 0",
            )

        available_ccy = self._wallet_balance()
        if available_ccy <= 0:
            return LimitSizeResult(
                ok=False,
                action="MAX_LIMIT_BUY_QTY",
                market_id=m,
                price=price,
                have=available_ccy,
                reason="INSUFFICIENT_MYR",
                suggestion="Fund the account or reduce price",
            )

        raw_qty = available_ccy / float(price)
        try:
            qty = self._validate_and_round_volume(m, raw_qty)
        except ValueError as exc:
            return LimitSizeResult(
                ok=False,
                action="MAX_LIMIT_BUY_QTY",
                market_id=m,
                price=price,
                have=available_ccy,
                reason="BELOW_MIN_VOLUME",
                suggestion=str(exc),
            )

        return LimitSizeResult(
            ok=True,
            action="MAX_LIMIT_BUY_QTY",
            market_id=m,
            quantity=qty,
            price=price,
            have=available_ccy,
        )

    def get_max_limit_sell_qty(self, market_id: str) -> LimitSizeResult:
        """
        Compute the maximum SELL limit quantity based on available holdings.
        """
        m = _assert_counter_market(market_id)
        base_asset = _base_asset_from_market(m)
        have = float(self._wallet_holdings().get(base_asset, 0.0))
        if have <= 0:
            return LimitSizeResult(
                ok=False,
                action="MAX_LIMIT_SELL_QTY",
                market_id=m,
                have=have,
                reason="INSUFFICIENT_ASSET",
                suggestion=f"No {base_asset} available to sell",
            )

        try:
            qty = self._validate_and_round_volume(m, have)
        except ValueError as exc:
            return LimitSizeResult(
                ok=False,
                action="MAX_LIMIT_SELL_QTY",
                market_id=m,
                have=have,
                reason="BELOW_MIN_VOLUME",
                suggestion=str(exc),
            )

        return LimitSizeResult(
            ok=True,
            action="MAX_LIMIT_SELL_QTY",
            market_id=m,
            quantity=qty,
            have=have,
        )

    # ---------------- BUY / SELL ----------------
    def buy_pair(
        self,
        market_id: str,
        quantity: float,
        rationale: str,
        max_spread_pct: float = 0.03,
    ) -> TradeResult:
        """
        Execute or simulate a BUY on a MYR market.

        BUY mechanics:
        - Uses ASK price for cost estimation.
        - Enforces min_volume and volume_scale.
        - Optional spread guard to avoid illiquid fills.

        Execution:
        - LIVE: Sends MARKET BUY with counter_volume to Luno.
        - DRY_RUN: No order sent.
        - Portfolio: balance/holdings updated in DB.

        Returns:
            TradeResult:
            - ok=True if accepted
            - ok=False with reason if blocked (no exception)

        Notes for AI agents:
        - Quantity should come from get_estimate_qty().
        - Returned price is a decision snapshot, not guaranteed fill price.
        """

        m = _assert_counter_market(market_id)
        base_asset = _base_asset_from_market(m)

        if quantity <= 0:
            return TradeResult(
                ok=False,
                action="BUY",
                market_id=m,
                reason="INVALID_QTY",
                want=quantity,
                suggestion="quantity must be > 0",
            )

        try:
            quantity = self._validate_and_round_volume(m, quantity)
        except ValueError as e:
            return TradeResult(
                ok=False,
                action="BUY",
                market_id=m,
                reason="INVALID_QTY_RULES",
                want=quantity,
                suggestion=str(e),
            )

        client = get_luno_client()
        ticker = client.get_ticker(pair=m)
        ask = float(ticker["ask"])
        bid = float(ticker["bid"])
        last_trade = float(ticker["last_trade"])

        if ask <= 0:
            return TradeResult(
                ok=False,
                action="BUY",
                market_id=m,
                reason="INVALID_ASK",
                ask=ask,
                bid=bid,
                last_trade=last_trade,
            )

        mid = (ask + bid) / 2 if (ask and bid) else ask
        spread_pct = (ask - bid) / mid if mid else 0.0
        if spread_pct > max_spread_pct:
            return TradeResult(
                ok=False,
                action="BUY",
                market_id=m,
                reason="SPREAD_TOO_WIDE",
                ask=ask,
                bid=bid,
                last_trade=last_trade,
                spread_pct=spread_pct,
                suggestion=f"Wait spread <= {max_spread_pct:.2%}",
            )

        est_cost = quantity * ask
        available_ccy = self._wallet_balance()
        if est_cost > available_ccy:
            return TradeResult(
                ok=False,
                action="BUY",
                market_id=m,
                reason="INSUFFICIENT_MYR",
                have=available_ccy,
                want=est_cost,
                ask=ask,
                bid=bid,
                last_trade=last_trade,
                spread_pct=spread_pct,
                suggestion="Call get_estimate_qty() with smaller spend_myr",
            )

        client_order_id = self._make_client_order_id()
        order = None
        if self.account_type == "live":
            order = client.post_market_order(
                pair=m,
                type="BUY",
                counter_volume=est_cost,
                counter_account_id=self._counter_account_id(),
                client_order_id=client_order_id,
            )
            self._apply_portfolio_buy(base_asset, quantity, est_cost)
        else:
            # portfolio update
            self._apply_portfolio_buy(base_asset, quantity, est_cost)

        self.transactions.append(
            Transaction(
                market_id=m,
                side="BUY",
                quantity=float(quantity),
                price=last_trade,
                timestamp=_utc_now_iso(),
                rationale=rationale,
            )
        )

        order_record = {
            "order_id": str(order.get("order_id")) if isinstance(order, dict) else None,
            "client_order_id": client_order_id,
            "market_id": m,
            "side": "BUY",
            "order_type": "MARKET",
            "price": float(last_trade),
            "volume": float(quantity),
            "state": "COMPLETE",
            "created_at": _utc_now_iso(),
            "filled_volume": float(quantity),
            "filled_counter": float(est_cost),
            "applied_volume": float(quantity),
            "applied_counter": float(est_cost),
            "rationale": rationale,
        }
        self._order_store().append(order_record)

        write_log(
            self.name,
            "trade",
            f"{self.account_type.upper()} BUY qty={quantity} {m} ask={ask} bid={bid} "
            f"est_cost~{est_cost:.2f} spread={spread_pct:.2%} client_order_id={client_order_id}",
        )

        self._archive_terminal_orders()
        self.save()

        return TradeResult(
            ok=True,
            action="BUY",
            market_id=m,
            quantity=quantity,
            est_cost=est_cost,
            ask=ask,
            bid=bid,
            last_trade=last_trade,
            spread_pct=spread_pct,
            order=order,
            client_order_id=client_order_id,
        )

    def sell_pair(
        self,
        market_id: str,
        quantity: float,
        rationale: str,
        max_spread_pct: float = 0.03,
    ) -> TradeResult:
        """
        Execute or simulate a SELL on a MYR market.

        SELL mechanics:
        - Uses BID price for proceeds estimation.
        - Enforces min_volume and volume_scale.
        - Optional spread guard.

        Execution:
        - LIVE: Sends MARKET SELL with base_volume to Luno.
        - DRY_RUN: No order sent.
        - Portfolio: balance/holdings updated in DB.

        Returns:
            TradeResult:
            - ok=True if accepted
            - ok=False if insufficient asset or blocked

        AI guidance:
        - Always check available holdings before selling.
        """

        m = _assert_counter_market(market_id)
        base_asset = _base_asset_from_market(m)

        if quantity <= 0:
            return TradeResult(
                ok=False,
                action="SELL",
                market_id=m,
                reason="INVALID_QTY",
                want=quantity,
                suggestion="quantity must be > 0",
            )

        try:
            quantity = self._validate_and_round_volume(m, quantity)
        except ValueError as e:
            return TradeResult(
                ok=False,
                action="SELL",
                market_id=m,
                reason="INVALID_QTY_RULES",
                want=quantity,
                suggestion=str(e),
            )

        client = get_luno_client()
        ticker = client.get_ticker(pair=m)
        ask = float(ticker["ask"])
        bid = float(ticker["bid"])
        last_trade = float(ticker["last_trade"])

        if bid <= 0:
            return TradeResult(
                ok=False,
                action="SELL",
                market_id=m,
                reason="INVALID_BID",
                ask=ask,
                bid=bid,
                last_trade=last_trade,
            )

        mid = (ask + bid) / 2 if (ask and bid) else bid
        spread_pct = (ask - bid) / mid if mid else 0.0
        if spread_pct > max_spread_pct:
            return TradeResult(
                ok=False,
                action="SELL",
                market_id=m,
                reason="SPREAD_TOO_WIDE",
                ask=ask,
                bid=bid,
                last_trade=last_trade,
                spread_pct=spread_pct,
                suggestion=f"Wait spread <= {max_spread_pct:.2%}",
            )

        wallet_holdings = self._wallet_holdings()
        have = float(wallet_holdings.get(base_asset, 0.0))
        if quantity > have:
            return TradeResult(
                ok=False,
                action="SELL",
                market_id=m,
                reason="INSUFFICIENT_ASSET",
                have=have,
                want=quantity,
                suggestion=f"Resize <= available {base_asset} ({have})",
            )

        est_proceeds = quantity * bid

        client_order_id = self._make_client_order_id()
        order = None
        if self.account_type == "live":
            order = client.post_market_order(
                pair=m,
                type="SELL",
                base_volume=quantity,
                counter_account_id=self._counter_account_id(),
                client_order_id=client_order_id,
            )
            self._apply_portfolio_sell(base_asset, quantity, est_proceeds)
        else:
            # portfolio update
            self._apply_portfolio_sell(base_asset, quantity, est_proceeds)

        self.transactions.append(
            Transaction(
                market_id=m,
                side="SELL",
                quantity=float(quantity),
                price=last_trade,
                timestamp=_utc_now_iso(),
                rationale=rationale,
            )
        )

        order_record = {
            "order_id": str(order.get("order_id")) if isinstance(order, dict) else None,
            "client_order_id": client_order_id,
            "market_id": m,
            "side": "SELL",
            "order_type": "MARKET",
            "price": float(last_trade),
            "volume": float(quantity),
            "state": "COMPLETE",
            "created_at": _utc_now_iso(),
            "filled_volume": float(quantity),
            "filled_counter": float(est_proceeds),
            "applied_volume": float(quantity),
            "applied_counter": float(est_proceeds),
            "rationale": rationale,
        }
        self._order_store().append(order_record)

        write_log(
            self.name,
            "trade",
            f"{self.account_type.upper()} SELL qty={quantity} {m} bid={bid} ask={ask} "
            f"est_proceeds~{est_proceeds:.2f} spread={spread_pct:.2%} client_order_id={client_order_id}",
        )

        self._archive_terminal_orders()
        self.save()

        return TradeResult(
            ok=True,
            action="SELL",
            market_id=m,
            quantity=quantity,
            est_proceeds=est_proceeds,
            ask=ask,
            bid=bid,
            last_trade=last_trade,
            spread_pct=spread_pct,
            order=order,
            client_order_id=client_order_id,
        )

    # ---------------- LIMIT ORDERS ----------------
    def post_limit_order(
        self,
        market_id: str,
        side: str,
        price: float,
        volume: float,
        rationale: str = "",
        post_only: bool | None = None,
        time_in_force: str | None = None,
        stop_price: float | None = None,
        stop_direction: str | None = None,
    ) -> OrderResult:
        """
        Place a LIMIT order.

        Args:
            market_id: Luno market id (e.g. XBTMYR)
            side: BUY/SELL (or BID/ASK)
            price: Limit price
            volume: Base-asset quantity
            rationale: Optional rationale for the order
        """
        m = _assert_counter_market(market_id)
        side_norm = _normalize_order_side(side)
        if side_norm is None:
            return OrderResult(
                ok=False,
                action="POST_LIMIT",
                market_id=m,
                reason="INVALID_SIDE",
                suggestion="side must be BUY/SELL or BID/ASK",
            )

        if price <= 0 or volume <= 0:
            return OrderResult(
                ok=False,
                action="POST_LIMIT",
                market_id=m,
                reason="INVALID_PRICE_OR_VOLUME",
                suggestion="price and volume must be > 0",
            )

        try:
            volume = self._validate_and_round_volume(m, volume)
        except ValueError as exc:
            return OrderResult(
                ok=False,
                action="POST_LIMIT",
                market_id=m,
                reason="INVALID_QTY_RULES",
                suggestion=str(exc),
            )

        if side_norm == "BUY":
            est_cost = float(volume) * float(price)
            if est_cost > self._wallet_balance():
                return OrderResult(
                    ok=False,
                    action="POST_LIMIT",
                    market_id=m,
                    reason="INSUFFICIENT_MYR",
                    have=self._wallet_balance(),
                    want=est_cost,
                    suggestion="Reduce volume or price",
                )
        else:
            base_asset = _base_asset_from_market(m)
            have = float(self._wallet_holdings().get(base_asset, 0.0))
            if volume > have:
                return OrderResult(
                    ok=False,
                    action="POST_LIMIT",
                    market_id=m,
                    reason="INSUFFICIENT_ASSET",
                    have=have,
                    want=volume,
                    suggestion=f"Resize <= available {base_asset} ({have})",
                )

        client_order_id = self._make_client_order_id()
        order_id = None
        state = "PENDING"

        order_record = {
            "order_id": None,
            "client_order_id": client_order_id,
            "market_id": m,
            "side": side_norm,
            "order_type": "LIMIT",
            "price": float(price),
            "volume": float(volume),
            "state": state,
            "created_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
            "applied_volume": 0.0,
            "applied_counter": 0.0,
            "filled_volume": 0.0,
            "filled_counter": 0.0,
            "rationale": rationale,
        }

        if self.account_type == "live":
            client = get_luno_client()
            order = client.post_limit_order(
                pair=m,
                price=price,
                type="BID" if side_norm == "BUY" else "ASK",
                volume=volume,
                client_order_id=client_order_id,
                counter_account_id=self._counter_account_id(),
                post_only=post_only,
                stop_direction=stop_direction,
                stop_price=stop_price,
                time_in_force=time_in_force,
            )
            if isinstance(order, dict):
                order_id = order.get("order_id") or order.get("id")
                state = _normalize_order_state(order.get("state")) or state

            order_record["order_id"] = str(order_id) if order_id else None
            order_record["state"] = state
            self.orders.append(order_record)
            if isinstance(order, dict):
                self._update_order_record(order_record, order)
            try:
                latest = client.get_order_v3(client_order_id=client_order_id)
            except Exception:
                latest = None
            if isinstance(latest, dict):
                self._update_order_record(order_record, latest)

            write_log(
                self.name,
                "order",
                f"{self.account_type.upper()} LIMIT {side_norm} {m} price={price} "
                f"volume={volume} client_order_id={client_order_id}",
            )
            self._archive_terminal_orders()
            self.save()
            return OrderResult(
                ok=True,
                action="POST_LIMIT",
                market_id=m,
                order_id=str(order_id) if order_id else None,
                client_order_id=client_order_id,
                order=order,
            )

        # DRY_RUN: simulate order placement
        order_id = f"paper-{uuid4()}"
        order_record["order_id"] = order_id

        filled = False
        order_snapshot: dict[str, Any] = {}
        try:
            client = get_luno_client()
            ticker = client.get_ticker(pair=m)
            ask = float(ticker["ask"])
            bid = float(ticker["bid"])
        except Exception:
            ask = 0.0
            bid = 0.0

        if side_norm == "BUY" and ask > 0 and price >= ask:
            filled = True
            exec_price = ask
        elif side_norm == "SELL" and bid > 0 and price <= bid:
            filled = True
            exec_price = bid
        else:
            exec_price = price

        if filled:
            state = "COMPLETE"
            counter_amount = float(volume) * float(exec_price)
            order_snapshot = {
                "state": state,
                "type": "BID" if side_norm == "BUY" else "ASK",
                "pair": m,
                "limit_price": exec_price,
                "limit_volume": volume,
                "base": volume,
                "counter": counter_amount,
            }

        order_record["state"] = state
        self.paper_orders.append(order_record)

        if filled:
            self._apply_order_fill(order_record, order_snapshot)

        write_log(
            self.name,
            "order",
            f"{self.account_type.upper()} LIMIT {side_norm} {m} price={price} "
            f"volume={volume} client_order_id={client_order_id} state={state}",
        )
        self._archive_terminal_orders()
        self.save()
        return OrderResult(
            ok=True,
            action="POST_LIMIT",
            market_id=m,
            order_id=order_id,
            client_order_id=client_order_id,
            order=order_record,
        )

    def cancel_order(
        self, order_id: str | None = None, client_order_id: str | None = None
    ) -> OrderResult:
        """
        Cancel a LIMIT order.
        """
        if order_id is None and client_order_id is None:
            return OrderResult(
                ok=False,
                action="CANCEL_ORDER",
                reason="MISSING_ORDER_ID",
                suggestion="Provide order_id or client_order_id",
            )

        if self.account_type == "live":
            record = self._find_order_record(order_id=order_id, client_order_id=client_order_id)
            client = get_luno_client()
            if order_id is None and record is not None:
                order_id = record.get("order_id")
            if order_id is None and client_order_id:
                try:
                    lookup = client.get_order_v3(client_order_id=client_order_id)
                except Exception:
                    lookup = None
                if isinstance(lookup, dict):
                    order_id = lookup.get("order_id") or lookup.get("id")
            if not order_id:
                return OrderResult(
                    ok=False,
                    action="CANCEL_ORDER",
                    reason="ORDER_ID_NOT_FOUND",
                    suggestion="Provide order_id or ensure order is tracked",
                )
            result = client.stop_order(order_id)
            if record is not None:
                record["state"] = "CANCELLED"
                record["updated_at"] = _utc_now_iso()
            self._archive_terminal_orders()
            self.save()
            write_log(self.name, "order", f"CANCEL order_id={order_id}")
            return OrderResult(
                ok=True,
                action="CANCEL_ORDER",
                order_id=str(order_id),
                client_order_id=record.get("client_order_id") if record else None,
                order=result,
            )

        record = self._find_order_record(order_id=order_id, client_order_id=client_order_id)
        if record is None:
            return OrderResult(
                ok=False,
                action="CANCEL_ORDER",
                reason="ORDER_NOT_FOUND",
                suggestion="Provide a valid order_id or client_order_id",
            )

        record["state"] = "CANCELLED"
        record["updated_at"] = _utc_now_iso()
        self._archive_terminal_orders()
        self.save()
        write_log(
            self.name,
            "order",
            f"CANCEL order_id={record.get('order_id')} client_order_id={record.get('client_order_id')}",
        )
        return OrderResult(
            ok=True,
            action="CANCEL_ORDER",
            order_id=str(record.get("order_id") or ""),
            client_order_id=record.get("client_order_id"),
            order=record,
        )

    def get_order(
        self, order_id: str | None = None, client_order_id: str | None = None
    ) -> OrderResult:
        """
        Fetch an order and update holdings if it has traded.
        """
        if (order_id is None and client_order_id is None) or (
            order_id is not None and client_order_id is not None
        ):
            return OrderResult(
                ok=False,
                action="GET_ORDER",
                reason="INVALID_PARAMS",
                suggestion="Provide exactly one of order_id or client_order_id",
            )

        if self.account_type == "live":
            client = get_luno_client()
            result = client.get_order_v3(
                id=order_id,
                client_order_id=client_order_id,
            )
            updated = False
            record = self._find_order_record(order_id=order_id, client_order_id=client_order_id)
            if record is None and isinstance(result, dict):
                client_id = _extract_str_field(result, ["client_order_id"])
                prefix = f"{_sanitize_client_order_prefix(self.name)}-"
                if client_id and client_id.startswith(prefix):
                    record = {
                        "order_id": _extract_str_field(result, ["order_id", "id"]),
                        "client_order_id": client_id,
                        "market_id": _normalize_market_id(
                            _extract_str_field(result, ["pair", "market_id"]) or ""
                        ),
                        "side": _normalize_order_side(
                            result.get("type") or result.get("side")
                        ),
                        "order_type": "LIMIT",
                        "price": _extract_float_field(result, ["limit_price", "price"]),
                        "volume": _extract_float_field(result, ["limit_volume", "volume"]),
                        "state": _normalize_order_state(result.get("state")),
                        "created_at": _ts_ms_to_iso(result.get("creation_timestamp"))
                        or _utc_now_iso(),
                        "updated_at": _utc_now_iso(),
                        "applied_volume": 0.0,
                        "applied_counter": 0.0,
                        "filled_volume": 0.0,
                        "filled_counter": 0.0,
                        "rationale": "",
                    }
                    self.orders.append(record)
                    updated = True
            if record is not None and isinstance(result, dict):
                if self._update_order_record(record, result):
                    updated = True
            if self._archive_terminal_orders():
                updated = True
            if updated:
                self.save()
            return OrderResult(
                ok=True,
                action="GET_ORDER",
                order_id=order_id,
                client_order_id=client_order_id,
                order=result,
            )

        record = self._find_order_record(order_id=order_id, client_order_id=client_order_id)
        if record is None:
            return OrderResult(
                ok=False,
                action="GET_ORDER",
                reason="ORDER_NOT_FOUND",
                suggestion="Provide a valid order_id or client_order_id",
            )
        return OrderResult(
            ok=True,
            action="GET_ORDER",
            order_id=str(record.get("order_id") or ""),
            client_order_id=record.get("client_order_id"),
            order=record,
        )

    def list_orders(
        self,
        created_before: int | None = None,
        limit: int | None = None,
        pair: str | None = None,
        state: str | None = None,
    ) -> OrderResult:
        """
        List recent orders and update holdings for any filled orders.
        """
        if self.account_type == "live":
            client = get_luno_client()
            result = client.list_orders(
                created_before=created_before,
                limit=limit,
                pair=pair,
                state=state,
            )
            updated = False
            orders = []
            if isinstance(result, dict):
                orders = result.get("orders") or result.get("order") or []
            prefix = f"{_sanitize_client_order_prefix(self.name)}-"
            for order in orders:
                if not isinstance(order, dict):
                    continue
                order_id = _extract_str_field(order, ["order_id", "id"])
                client_id = _extract_str_field(order, ["client_order_id"])
                record = self._find_order_record(order_id=order_id, client_order_id=client_id)
                if record is None and client_id and client_id.startswith(prefix):
                    record = {
                        "order_id": order_id,
                        "client_order_id": client_id,
                        "market_id": _normalize_market_id(
                            _extract_str_field(order, ["pair", "market_id"]) or ""
                        ),
                        "side": _normalize_order_side(
                            order.get("type") or order.get("side")
                        ),
                        "order_type": "LIMIT",
                        "price": _extract_float_field(order, ["limit_price", "price"]),
                        "volume": _extract_float_field(order, ["limit_volume", "volume"]),
                        "state": _normalize_order_state(order.get("state")),
                        "created_at": _ts_ms_to_iso(order.get("creation_timestamp"))
                        or _utc_now_iso(),
                        "updated_at": _utc_now_iso(),
                        "applied_volume": 0.0,
                        "applied_counter": 0.0,
                        "filled_volume": 0.0,
                        "filled_counter": 0.0,
                        "rationale": "",
                    }
                    self.orders.append(record)
                if record is not None:
                    if self._update_order_record(record, order):
                        updated = True
            if self._archive_terminal_orders():
                updated = True
            if updated:
                self.save()
            return OrderResult(
                ok=True,
                action="LIST_ORDERS",
                orders=result,
            )

        # DRY_RUN: return local orders
        orders = list(self.paper_orders)
        if pair:
            m = _normalize_market_id(pair)
            orders = [o for o in orders if o.get("market_id") == m]
        if state:
            st = state.strip().upper()
            orders = [o for o in orders if str(o.get("state") or "").upper() == st]
        if created_before:
            cutoff = _ts_ms_to_iso(created_before)
            if cutoff:
                cutoff_dt = datetime.fromisoformat(cutoff)
                filtered = []
                for o in orders:
                    try:
                        created_at = datetime.fromisoformat(str(o.get("created_at")))
                    except (TypeError, ValueError):
                        continue
                    if created_at <= cutoff_dt:
                        filtered.append(o)
                orders = filtered
        if limit:
            orders = orders[: int(limit)]
        return OrderResult(
            ok=True,
            action="LIST_ORDERS",
            orders=orders,
        )

    def sync_user_trades(
        self,
        pair: str | None = None,
        since: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """
        Sync filled trades using list_user_trades and apply to tracked LIMIT orders.
        """
        summary: dict[str, Any] = {
            "ok": True,
            "account": self.name,
            "pairs": [],
            "orders_matched": 0,
            "trades_seen": 0,
            "trades_applied": 0,
            "trades_skipped": 0,
            "errors": [],
        }

        if self.account_type != "live":
            summary["ok"] = False
            summary["reason"] = "DRY_RUN"
            return summary

        if pair:
            pairs = [_normalize_market_id(pair)]
        else:
            pairs_set: set[str] = set()
            for record in self.orders:
                if str(record.get("order_type") or "").upper() != "LIMIT":
                    continue
                market_id = record.get("market_id")
                if not market_id:
                    continue
                pairs_set.add(_normalize_market_id(str(market_id)))
            pairs = sorted(pairs_set)

        if not pairs:
            summary["reason"] = "NO_LIMIT_ORDERS"
            return summary

        client = get_luno_client()
        updated = False

        for market_id in pairs:
            summary["pairs"].append(market_id)
            try:
                res = client.list_user_trades(
                    pair=market_id,
                    since=since,
                    limit=limit,
                )
            except Exception as exc:
                summary["errors"].append(f"{market_id}: {exc}")
                continue

            trades = []
            if isinstance(res, dict):
                trades = res.get("trades") or res.get("trade") or []
            if not isinstance(trades, list) or not trades:
                continue

            for trade in trades:
                if not isinstance(trade, dict):
                    continue
                summary["trades_seen"] += 1

                order_id = _extract_str_field(trade, ["order_id"])
                if not order_id:
                    summary["trades_skipped"] += 1
                    continue

                record = self._find_order_record(order_id=order_id)
                if record is None:
                    summary["trades_skipped"] += 1
                    continue

                if str(record.get("order_type") or "").upper() != "LIMIT":
                    summary["trades_skipped"] += 1
                    continue

                self._ensure_order_tracking_fields(record)
                summary["orders_matched"] += 1

                trade_id = _extract_str_field(
                    trade,
                    ["id", "trade_id", "sequence", "trade_sequence"],
                )
                if not trade_id:
                    timestamp = trade.get("timestamp")
                    volume_hint = trade.get("volume") or trade.get("base")
                    if timestamp is not None:
                        trade_id = f"{timestamp}-{trade.get('price')}-{volume_hint}"

                if trade_id:
                    trade_id = str(trade_id)
                    if trade_id in record.get("applied_trade_ids", []):
                        summary["trades_skipped"] += 1
                        continue

                trade_base = _extract_float_field(
                    trade,
                    [
                        "base",
                        "base_amount",
                        "volume",
                        "base_volume",
                        "filled_volume",
                        "filled_base",
                    ],
                )
                trade_counter = _extract_float_field(
                    trade,
                    ["counter", "counter_amount", "counter_volume", "filled_counter"],
                )
                price = _extract_float_field(trade, ["price"])

                if trade_base is None and trade_counter is not None and price:
                    trade_base = float(trade_counter) / float(price)
                if trade_counter is None and trade_base is not None and price:
                    trade_counter = float(trade_base) * float(price)

                if trade_base is None or trade_base <= 0:
                    summary["trades_skipped"] += 1
                    continue

                applied_base = float(record.get("applied_volume") or 0.0)
                applied_counter = float(record.get("applied_counter") or 0.0)

                order_data: dict[str, Any] = {
                    "order_id": order_id,
                    "pair": market_id,
                    "base": applied_base + float(trade_base),
                }
                if trade_counter is not None:
                    order_data["counter"] = applied_counter + float(trade_counter)
                if price is not None:
                    order_data["price"] = float(price)

                side = _normalize_order_side(trade.get("type") or trade.get("side"))
                if side is None:
                    is_buy = trade.get("is_buy")
                    if isinstance(is_buy, bool):
                        side = "BUY" if is_buy else "SELL"
                if side == "BUY":
                    order_data["type"] = "BID"
                elif side == "SELL":
                    order_data["type"] = "ASK"

                try:
                    target_volume = float(record.get("volume") or 0.0)
                except (TypeError, ValueError):
                    target_volume = 0.0
                if target_volume > 0 and order_data["base"] >= target_volume:
                    order_data["state"] = "COMPLETE"
                elif record.get("state"):
                    order_data["state"] = record.get("state")

                if self._apply_order_fill(record, order_data):
                    updated = True
                    summary["trades_applied"] += 1
                    if trade_id:
                        record["applied_trade_ids"].append(trade_id)
                else:
                    summary["trades_skipped"] += 1

        if self._archive_terminal_orders():
            updated = True

        if updated:
            for record in self.orders:
                trade_ids = record.get("applied_trade_ids")
                if not isinstance(trade_ids, list):
                    continue
                seen = set()
                unique_ids = []
                for tid in trade_ids:
                    tid_str = str(tid)
                    if tid_str in seen:
                        continue
                    seen.add(tid_str)
                    unique_ids.append(tid_str)
                record["applied_trade_ids"] = unique_ids
            self.save()

        return summary

    def compute_portfolio_value(self) -> float:
        """
        Compute total portfolio value in MYR.

        Valuation rules:
        - Counter currency (MYR) uses portfolio balance from DB.
        - Crypto assets are valued using BID price (realistic liquidation value).

        Returns:
            Total portfolio value in MYR as float.
        """
        client = get_luno_client()
        counter = get_counter_currency().upper()

        total = Decimal("0")

        total += Decimal(str(self.balance))
        holdings = self.holdings

        for asset, qty in holdings.items():
            if qty <= 0:
                continue

            market_id = f"{asset}{counter}"

            try:
                ticker = client.get_ticker(pair=market_id)
                bid = Decimal(ticker["bid"])
            except Exception:
                # If market not available, skip valuation
                continue

            total += Decimal(str(qty)) * bid

        return float(total)

    def snapshot_portfolio_value(self) -> float:
        """
        Compute and store a portfolio value snapshot with timestamp.

        Useful for:
        - Equity curve
        - Strategy evaluation
        - Drawdown analysis
        """
        value = self.compute_portfolio_value()
        self.portfolio_value_time_series.append((_utc_now_iso(), value))
        self.save()
        return value
