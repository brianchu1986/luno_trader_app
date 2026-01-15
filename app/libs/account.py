# app/libs/account.py

"""
Account & Trading Engine (Luno MYR Markets)

This module defines the Account model and all trading-related logic
for both LIVE trading and TRUE PAPER TRADING.

Key concepts:
- Counter currency is fixed (MYR by default).
- All markets traded must end with the counter currency (e.g. GRTMYR).
- LIVE mode sends real orders to Luno.
- DRY_RUN mode performs TRUE paper trading:
    - Maintains paper_balance and paper_holdings
    - Uses real market prices (ask/bid)
    - Never sends orders to Luno

Design goals:
- Safe for AI agents (no hard crashes on expected conditions)
- Deterministic, structured outputs via TradeResult (Pydantic)
- Luno balances are treated as the source of truth for LIVE mode
- Paper wallet is isolated and reproducible for simulations

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
from app import get_luno_client, get_counter_currency

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional, Literal

import pandas as pd

load_dotenv(override=True)
INITIAL_BALANCE = 0.00


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


class Transaction(BaseModel):
    market_id: str
    side: Literal["BUY", "SELL"]
    quantity: float          # base asset units intended
    price: float             # last_trade snapshot (not guaranteed fill)
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
    quantity: Optional[float] = None          # base quantity
    spend_myr: Optional[float] = None         # requested spend (estimate tool)
    est_cost: Optional[float] = None          # estimated MYR cost (buy)
    est_proceeds: Optional[float] = None      # estimated MYR proceeds (sell)
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
    - Reads balances from Luno
    - Sends real MARKET orders via Luno API
    - balance / holdings reflect available (balance - reserved)

    DRY_RUN mode (true paper trading):
    - Uses paper_balance and paper_holdings
    - Simulates trades using real-time prices
    - Never sends orders to Luno
    - Safe for strategy testing and AI experimentation

    Fields:
        name:
            Logical account name (used as DB key).

        account_id:
            Counter currency account_id from Luno (e.g. MYR account).

        balance:
            LIVE available counter currency (balance - reserved).

        holdings:
            LIVE available asset balances (balance - reserved).

        paper_balance:
            Simulated counter currency balance for DRY_RUN mode.

        paper_holdings:
            Simulated asset holdings for DRY_RUN mode.

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

    # ✅ PAPER WALLET (true paper trading)
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
        fields.setdefault("paper_balance", float(fields.get("balance", INITIAL_BALANCE)))
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
            raise ValueError(f"Invalid account_type '{normalized}'. Allowed: {sorted(allowed)}")

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
        use_paper = self.account_type == "dry_run"
        balance = self.paper_balance if use_paper else self.balance
        holdings = self.paper_holdings if use_paper else self.holdings
        wallet_label = "paper" if use_paper else "live"

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
        return self.paper_balance if self.account_type == "dry_run" else self.balance

    def _wallet_holdings(self) -> dict[str, float]:
        return self.paper_holdings if self.account_type == "dry_run" else self.holdings

    def _set_wallet_balance(self, v: float) -> None:
        if self.account_type == "dry_run":
            self.paper_balance = v
        else:
            self.balance = v

    def _set_wallet_holdings(self, h: dict[str, float]) -> None:
        if self.account_type == "dry_run":
            self.paper_holdings = h
        else:
            self.holdings = h

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
    def refresh_from_luno(self) -> None:
        """
        Source of truth sync from Luno.
        Updates:
          - self.balance as counter available (balance - reserved)
          - self.account_id from counter row
          - self.holdings for ALL non-counter assets as available (balance - reserved)
        """
        client = get_luno_client()
        res = client.get_balances()
        df = pd.DataFrame(res.get("balance", []))
        if df.empty:
            raise ValueError("No balances returned from Luno.")

        counter = get_counter_currency().upper()
        cc_row = df.loc[df["asset"] == counter]
        if cc_row.empty:
            raise ValueError(f"{counter} balance not found.")

        self.account_id = str(cc_row["account_id"].iloc[0])
        self.balance = _parse_available(cc_row["balance"].iloc[0], cc_row["reserved"].iloc[0])

        new_holdings: dict[str, float] = {}
        for _, r in df.iterrows():
            asset = str(r.get("asset", "")).upper()
            if not asset or asset == counter:
                continue
            avail = _parse_available(r.get("balance"), r.get("reserved"))
            if avail > 0:
                new_holdings[asset] = avail

        self.holdings = new_holdings
        self.save()

    # ✅ Initialize/reset paper wallet based on current live balances
    def paper_reset_from_luno(self) -> str:
        """
        Initialize or reset the paper wallet using current LIVE Luno balances.
    
        Behavior:
        - Reads LIVE balances from Luno (read-only).
        - Copies available MYR → paper_balance.
        - Copies available assets → paper_holdings.
        - Does NOT send any orders.
    
        When to use:
        - Before starting a paper trading session.
        - When you want paper trading to reflect current real balances.
    
        Safe:
        - No trading side effects.
        """

        self.refresh_from_luno()
        self.paper_balance = float(self.balance)
        self.paper_holdings = dict(self.holdings)
        self.save()
        write_log(self.name, "paper", "Paper wallet reset from Luno balances")
        return "Paper wallet reset from Luno balances"

    # ---------------- Paper trade apply ----------------
    def _apply_paper_buy(self, base_asset: str, qty: float, cost: float) -> None:
        pb = float(self.paper_balance)
        if cost > pb:
            raise ValueError("paper insufficient balance")
        self.paper_balance = pb - cost
        ph = dict(self.paper_holdings)
        ph[base_asset] = float(ph.get(base_asset, 0.0)) + qty
        # keep tiny dust? optional, keep it
        self.paper_holdings = ph

    def _apply_paper_sell(self, base_asset: str, qty: float, proceeds: float) -> None:
        ph = dict(self.paper_holdings)
        have = float(ph.get(base_asset, 0.0))
        if qty > have:
            raise ValueError("paper insufficient asset")
        ph[base_asset] = have - qty
        if ph[base_asset] <= 0:
            ph.pop(base_asset, None)
        self.paper_holdings = ph
        self.paper_balance = float(self.paper_balance) + proceeds

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
        - Uses available wallet:
            - paper_balance in DRY_RUN
            - live balance in LIVE mode
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
            return TradeResult(ok=False, action="ESTIMATE_BUY_QTY", market_id=m,
                               reason="INVALID_SPEND", want=spend_myr,
                               suggestion="spend_myr must be > 0")

        client = get_luno_client()
        ticker = client.get_ticker(pair=m)
        ask = float(ticker["ask"])
        bid = float(ticker["bid"])
        last_trade = float(ticker["last_trade"])

        if ask <= 0:
            return TradeResult(ok=False, action="ESTIMATE_BUY_QTY", market_id=m,
                               reason="INVALID_ASK", ask=ask, bid=bid, last_trade=last_trade)

        # ✅ in dry_run, use paper_balance; in live, use real available balance
        available_ccy = self._wallet_balance()
        if spend_myr > available_ccy:
            return TradeResult(ok=False, action="ESTIMATE_BUY_QTY", market_id=m,
                               reason="INSUFFICIENT_MYR", have=available_ccy, want=spend_myr,
                               ask=ask, bid=bid, last_trade=last_trade,
                               suggestion="Reduce spend_myr or reset paper wallet")

        raw_qty = spend_myr / ask
        try:
            qty = self._validate_and_round_volume(m, raw_qty)
        except ValueError as e:
            return TradeResult(ok=False, action="ESTIMATE_BUY_QTY", market_id=m,
                               reason="BELOW_MIN_VOLUME", suggestion=str(e),
                               ask=ask, bid=bid, last_trade=last_trade)

        mid = (ask + bid) / 2 if (ask and bid) else ask
        spread_pct = (ask - bid) / mid if mid else 0.0

        return TradeResult(ok=True, action="ESTIMATE_BUY_QTY", market_id=m,
                           quantity=qty, spend_myr=spend_myr,
                           ask=ask, bid=bid, last_trade=last_trade,
                           spread_pct=spread_pct)

    # ---------------- BUY / SELL ----------------
    def buy_pair(self, market_id: str, quantity: float, rationale: str, max_spread_pct: float = 0.03) -> TradeResult:
        """
        Execute or simulate a BUY on a MYR market.
    
        BUY mechanics:
        - Uses ASK price for cost estimation.
        - Enforces min_volume and volume_scale.
        - Optional spread guard to avoid illiquid fills.
    
        Execution:
        - LIVE:
            Sends MARKET BUY with counter_volume to Luno.
        - DRY_RUN:
            Deducts paper_balance.
            Increases paper_holdings.
    
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
            return TradeResult(ok=False, action="BUY", market_id=m,
                               reason="INVALID_QTY", want=quantity,
                               suggestion="quantity must be > 0")

        try:
            quantity = self._validate_and_round_volume(m, quantity)
        except ValueError as e:
            return TradeResult(ok=False, action="BUY", market_id=m,
                               reason="INVALID_QTY_RULES", want=quantity, suggestion=str(e))

        client = get_luno_client()
        ticker = client.get_ticker(pair=m)
        ask = float(ticker["ask"])
        bid = float(ticker["bid"])
        last_trade = float(ticker["last_trade"])

        if ask <= 0:
            return TradeResult(ok=False, action="BUY", market_id=m, reason="INVALID_ASK",
                               ask=ask, bid=bid, last_trade=last_trade)

        mid = (ask + bid) / 2 if (ask and bid) else ask
        spread_pct = (ask - bid) / mid if mid else 0.0
        if spread_pct > max_spread_pct:
            return TradeResult(ok=False, action="BUY", market_id=m, reason="SPREAD_TOO_WIDE",
                               ask=ask, bid=bid, last_trade=last_trade, spread_pct=spread_pct,
                               suggestion=f"Wait spread <= {max_spread_pct:.2%}")

        est_cost = quantity * ask
        available_ccy = self._wallet_balance()
        if est_cost > available_ccy:
            return TradeResult(ok=False, action="BUY", market_id=m, reason="INSUFFICIENT_MYR",
                               have=available_ccy, want=est_cost,
                               ask=ask, bid=bid, last_trade=last_trade, spread_pct=spread_pct,
                               suggestion="Call get_estimate_qty() with smaller spend_myr")

        order = None
        if self.account_type == "live":
            order = client.post_market_order(pair=m, type="BUY", counter_volume=est_cost)
        else:
            # ✅ PAPER EXECUTION
            self._apply_paper_buy(base_asset, quantity, est_cost)

        self.transactions.append(Transaction(
            market_id=m, side="BUY", quantity=float(quantity),
            price=last_trade, timestamp=_utc_now_iso(), rationale=rationale
        ))

        write_log(self.name, "trade",
                  f"{self.account_type.upper()} BUY qty={quantity} {m} ask={ask} bid={bid} "
                  f"est_cost~{est_cost:.2f} spread={spread_pct:.2%}")

        self.save()

        return TradeResult(ok=True, action="BUY", market_id=m,
                           quantity=quantity, est_cost=est_cost,
                           ask=ask, bid=bid, last_trade=last_trade,
                           spread_pct=spread_pct, order=order)

    def sell_pair(self, market_id: str, quantity: float, rationale: str, max_spread_pct: float = 0.03) -> TradeResult:
        """
        Execute or simulate a SELL on a MYR market.
    
        SELL mechanics:
        - Uses BID price for proceeds estimation.
        - Enforces min_volume and volume_scale.
        - Optional spread guard.
    
        Execution:
        - LIVE:
            Sends MARKET SELL with base_volume to Luno.
        - DRY_RUN:
            Reduces paper_holdings.
            Increases paper_balance.
    
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
            return TradeResult(ok=False, action="SELL", market_id=m,
                               reason="INVALID_QTY", want=quantity,
                               suggestion="quantity must be > 0")

        try:
            quantity = self._validate_and_round_volume(m, quantity)
        except ValueError as e:
            return TradeResult(ok=False, action="SELL", market_id=m,
                               reason="INVALID_QTY_RULES", want=quantity, suggestion=str(e))

        client = get_luno_client()
        ticker = client.get_ticker(pair=m)
        ask = float(ticker["ask"])
        bid = float(ticker["bid"])
        last_trade = float(ticker["last_trade"])

        if bid <= 0:
            return TradeResult(ok=False, action="SELL", market_id=m, reason="INVALID_BID",
                               ask=ask, bid=bid, last_trade=last_trade)

        mid = (ask + bid) / 2 if (ask and bid) else bid
        spread_pct = (ask - bid) / mid if mid else 0.0
        if spread_pct > max_spread_pct:
            return TradeResult(ok=False, action="SELL", market_id=m, reason="SPREAD_TOO_WIDE",
                               ask=ask, bid=bid, last_trade=last_trade, spread_pct=spread_pct,
                               suggestion=f"Wait spread <= {max_spread_pct:.2%}")

        wallet_holdings = self._wallet_holdings()
        have = float(wallet_holdings.get(base_asset, 0.0))
        if quantity > have:
            return TradeResult(ok=False, action="SELL", market_id=m, reason="INSUFFICIENT_ASSET",
                               have=have, want=quantity,
                               suggestion=f"Resize <= available {base_asset} ({have})")

        est_proceeds = quantity * bid

        order = None
        if self.account_type == "live":
            order = client.post_market_order(pair=m, type="SELL", base_volume=quantity)
        else:
            # ✅ PAPER EXECUTION
            self._apply_paper_sell(base_asset, quantity, est_proceeds)

        self.transactions.append(Transaction(
            market_id=m, side="SELL", quantity=float(quantity),
            price=last_trade, timestamp=_utc_now_iso(), rationale=rationale
        ))

        write_log(self.name, "trade",
                  f"{self.account_type.upper()} SELL qty={quantity} {m} bid={bid} ask={ask} "
                  f"est_proceeds~{est_proceeds:.2f} spread={spread_pct:.2%}")

        self.save()

        return TradeResult(ok=True, action="SELL", market_id=m,
                           quantity=quantity, est_proceeds=est_proceeds,
                           ask=ask, bid=bid, last_trade=last_trade,
                           spread_pct=spread_pct, order=order)
    
    def compute_portfolio_value(self) -> float:
        """
        Compute total portfolio value in MYR.
    
        Valuation rules:
        - Counter currency (MYR) uses available balance.
        - Crypto assets are valued using BID price (realistic liquidation value).
        - LIVE mode uses real Luno balances.
        - DRY_RUN mode uses paper balances.
    
        Returns:
            Total portfolio value in MYR as float.
        """
        client = get_luno_client()
        counter = get_counter_currency().upper()
    
        total = Decimal("0")
    
        # Choose wallet source
        if self.account_type == "dry_run":
            total += Decimal(str(self.paper_balance))
            holdings = self.paper_holdings
        else:
            # Ensure live balances are up-to-date
            self.refresh_from_luno()
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


