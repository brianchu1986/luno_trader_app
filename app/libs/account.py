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

import pandas as pd

load_dotenv(override=True)
INITIAL_BALANCE = 0.00
MAX_MYR_TRADERS = 9
MYR_ACCOUNT_PREFIX = "MYR_"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    # Failure fields
    reason: Optional[str] = None
    have: Optional[float] = None
    want: Optional[float] = None
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

        transactions:
            History of intended trades (not guaranteed fills).

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
    portfolio_value_time_series: list[tuple[str, float]]
    account_type: str  # "dry_run" or "live"

    # Portfolio mirror (legacy)
    paper_balance: float
    paper_holdings: dict[str, float]

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
                "portfolio_value_time_series": [],
                "account_type": "dry_run",
                # paper defaults
                "paper_balance": INITIAL_BALANCE,
                "paper_holdings": {},
            }
            write_account(name, fields)

        # backward compat: if old DB record doesn’t have these keys
        fields.setdefault("account_type", "dry_run")
        fields.setdefault(
            "paper_balance", float(fields.get("balance", INITIAL_BALANCE))
        )
        fields.setdefault("paper_holdings", dict(fields.get("holdings", {})))
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

        return (
            f"Account: **{self.name}**\n"
            f"- Strategy: `{self.strategy}`\n"
            f"- {cc} Available ({wallet_label}): **{balance:.2f}**\n"
            f"- {cc} account_id: `{self.account_id}`\n"
            f"- Holdings ({wallet_label}):\n{holdings_text}\n"
            f"- Transactions: {len(self.transactions)}\n"
            f"- account_type: `{self.account_type}`"
        )

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

        order = None
        if self.account_type == "live":
            order = client.post_market_order(
                pair=m,
                type="BUY",
                counter_volume=est_cost,
                counter_account_id=self._counter_account_id(),
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

        write_log(
            self.name,
            "trade",
            f"{self.account_type.upper()} BUY qty={quantity} {m} ask={ask} bid={bid} "
            f"est_cost~{est_cost:.2f} spread={spread_pct:.2%}",
        )

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

        order = None
        if self.account_type == "live":
            order = client.post_market_order(
                pair=m,
                type="SELL",
                base_volume=quantity,
                counter_account_id=self._counter_account_id(),
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

        write_log(
            self.name,
            "trade",
            f"{self.account_type.upper()} SELL qty={quantity} {m} bid={bid} ask={ask} "
            f"est_proceeds~{est_proceeds:.2f} spread={spread_pct:.2%}",
        )

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
        )

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
