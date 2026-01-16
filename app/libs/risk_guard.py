from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any, Literal

from pydantic import BaseModel

from app import get_counter_currency
from app.libs.account import Account
from app.libs.database import write_log
from app.libs.market import assert_tradable_myr_market, get_market_ticker, normalize_market_id


class RiskDecision(BaseModel):
    ok: bool
    action: Literal["RISK_CHECK"] = "RISK_CHECK"
    reason: str | None = None
    suggestion: str | None = None
    details: dict[str, Any] | None = None


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _normalize_side(side: str) -> str | None:
    s = str(side or "").strip().upper()
    if s in {"BUY", "BID"}:
        return "BUY"
    if s in {"SELL", "ASK"}:
        return "SELL"
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


def load_risk_config() -> dict[str, Any]:
    return {
        "max_trade_pct": _float_env("RISK_MAX_TRADE_PCT", 0.25),
        "max_position_pct": _float_env("RISK_MAX_POSITION_PCT", 0.5),
        "min_myr_balance": _float_env("RISK_MIN_MYR_BALANCE", 0.0),
        "max_notional_myr": _float_env("RISK_MAX_NOTIONAL_MYR", 0.0),
        "max_buys_24h": _int_env("RISK_MAX_BUYS_24H", 0),
        "trade_cooldown_minutes": _int_env("RISK_TRADE_COOLDOWN_MINUTES", 0),
    }


def assess_trade(
    name: str,
    market_id: str,
    side: str,
    quantity: float,
    order_type: str = "MARKET",
    limit_price: float | None = None,
) -> RiskDecision:
    cfg = load_risk_config()

    side_norm = _normalize_side(side)
    if side_norm is None:
        return RiskDecision(
            ok=False,
            reason="INVALID_SIDE",
            suggestion="Use BUY/SELL (or BID/ASK).",
        )

    if quantity <= 0:
        return RiskDecision(
            ok=False,
            reason="INVALID_QTY",
            suggestion="quantity must be > 0.",
        )

    try:
        m = assert_tradable_myr_market(market_id)
    except Exception as exc:
        return RiskDecision(
            ok=False,
            reason="INVALID_MARKET",
            suggestion=str(exc),
        )

    order_type_norm = str(order_type or "MARKET").strip().upper()
    if order_type_norm not in {"MARKET", "LIMIT"}:
        return RiskDecision(
            ok=False,
            reason="INVALID_ORDER_TYPE",
            suggestion="Use MARKET or LIMIT.",
        )
    if order_type_norm == "LIMIT" and (limit_price is None or limit_price <= 0):
        return RiskDecision(
            ok=False,
            reason="MISSING_LIMIT_PRICE",
            suggestion="Provide limit_price > 0 for LIMIT orders.",
        )

    acc = Account.get(name)
    counter = get_counter_currency().upper()
    base_asset = m[: -len(counter)] if m.endswith(counter) else m
    holdings = acc.holdings or {}
    base_qty = float(holdings.get(base_asset, 0.0))

    if side_norm == "SELL" and quantity > base_qty:
        return RiskDecision(
            ok=False,
            reason="INSUFFICIENT_ASSET",
            suggestion=f"Resize <= available {base_asset} ({base_qty}).",
        )

    ticker = get_market_ticker(m)
    ask = float(ticker.get("ask") or 0.0)
    bid = float(ticker.get("bid") or 0.0)
    last_trade = float(ticker.get("last_trade") or 0.0)

    if side_norm == "BUY":
        price_ref = limit_price if order_type_norm == "LIMIT" else ask
    else:
        price_ref = limit_price if order_type_norm == "LIMIT" else bid

    if price_ref is None or price_ref <= 0:
        return RiskDecision(
            ok=False,
            reason="INVALID_PRICE",
            suggestion="Check market price or limit_price.",
        )

    notional = float(quantity) * float(price_ref)
    balance = float(acc.balance or 0.0)

    try:
        portfolio_value = float(acc.compute_portfolio_value())
    except Exception:
        portfolio_value = balance + base_qty * price_ref

    if side_norm == "BUY":
        base_after = base_qty + quantity
        cash_after = balance - notional
    else:
        base_after = max(base_qty - quantity, 0.0)
        cash_after = balance + notional

    exposure_after = base_after * price_ref
    exposure_pct = (
        exposure_after / portfolio_value if portfolio_value > 0 else 0.0
    )

    if cfg["max_notional_myr"] > 0 and notional > cfg["max_notional_myr"]:
        return _finalize(
            name,
            side_norm,
            m,
            quantity,
            RiskDecision(
                ok=False,
                reason="MAX_NOTIONAL_EXCEEDED",
                suggestion=f"Keep trade <= {cfg['max_notional_myr']:.2f} MYR.",
                details=_details(
                    m,
                    side_norm,
                    order_type_norm,
                    quantity,
                    price_ref,
                    ask,
                    bid,
                    last_trade,
                    balance,
                    cash_after,
                    base_asset,
                    base_qty,
                    base_after,
                    portfolio_value,
                    exposure_pct,
                    cfg,
                ),
            ),
        )

    if side_norm == "BUY":
        if cfg["max_trade_pct"] > 0 and notional > balance * cfg["max_trade_pct"]:
            return _finalize(
                name,
                side_norm,
                m,
                quantity,
                RiskDecision(
                    ok=False,
                    reason="MAX_TRADE_PCT_EXCEEDED",
                    suggestion=(
                        "Reduce size to stay within "
                        f"{cfg['max_trade_pct']:.0%} of available MYR."
                    ),
                    details=_details(
                        m,
                        side_norm,
                        order_type_norm,
                        quantity,
                        price_ref,
                        ask,
                        bid,
                        last_trade,
                        balance,
                        cash_after,
                        base_asset,
                        base_qty,
                        base_after,
                        portfolio_value,
                        exposure_pct,
                        cfg,
                    ),
                ),
            )

        if cfg["min_myr_balance"] > 0 and cash_after < cfg["min_myr_balance"]:
            return _finalize(
                name,
                side_norm,
                m,
                quantity,
                RiskDecision(
                    ok=False,
                    reason="MIN_BALANCE_BREACH",
                    suggestion=(
                        "Keep cash >= "
                        f"{cfg['min_myr_balance']:.2f} MYR."
                    ),
                    details=_details(
                        m,
                        side_norm,
                        order_type_norm,
                        quantity,
                        price_ref,
                        ask,
                        bid,
                        last_trade,
                        balance,
                        cash_after,
                        base_asset,
                        base_qty,
                        base_after,
                        portfolio_value,
                        exposure_pct,
                        cfg,
                    ),
                ),
            )

        if cfg["max_position_pct"] > 0 and portfolio_value > 0:
            if exposure_pct > cfg["max_position_pct"]:
                return _finalize(
                    name,
                    side_norm,
                    m,
                    quantity,
                    RiskDecision(
                        ok=False,
                        reason="MAX_POSITION_PCT_EXCEEDED",
                        suggestion=(
                            "Reduce size so exposure <= "
                            f"{cfg['max_position_pct']:.0%} of portfolio."
                        ),
                        details=_details(
                            m,
                            side_norm,
                            order_type_norm,
                            quantity,
                            price_ref,
                            ask,
                            bid,
                            last_trade,
                            balance,
                            cash_after,
                            base_asset,
                            base_qty,
                            base_after,
                            portfolio_value,
                            exposure_pct,
                            cfg,
                        ),
                    ),
                )

        if cfg["max_buys_24h"] > 0:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=24)
            recent_buys = 0
            for tx in acc.transactions:
                tx_side = _normalize_side(getattr(tx, "side", ""))
                if tx_side != "BUY":
                    continue
                dt = _parse_iso_datetime(getattr(tx, "timestamp", None))
                if dt and dt >= cutoff:
                    recent_buys += 1
            if recent_buys >= cfg["max_buys_24h"]:
                return _finalize(
                    name,
                    side_norm,
                    m,
                    quantity,
                    RiskDecision(
                        ok=False,
                        reason="MAX_BUYS_24H_EXCEEDED",
                        suggestion=(
                            f"Max buys in 24h reached ({cfg['max_buys_24h']})."
                        ),
                        details=_details(
                            m,
                            side_norm,
                            order_type_norm,
                            quantity,
                            price_ref,
                            ask,
                            bid,
                            last_trade,
                            balance,
                            cash_after,
                            base_asset,
                            base_qty,
                            base_after,
                            portfolio_value,
                            exposure_pct,
                            cfg,
                        ),
                    ),
                )

    cooldown_minutes = cfg.get("trade_cooldown_minutes", 0)
    cooldown_seconds = int(cooldown_minutes) * 60 if cooldown_minutes > 0 else 0
    if cooldown_seconds > 0:
        now = datetime.now(timezone.utc)
        last_dt = None
        for tx in acc.transactions:
            tx_side = _normalize_side(getattr(tx, "side", ""))
            if tx_side != side_norm:
                continue
            tx_market = normalize_market_id(str(getattr(tx, "market_id", "")))
            if tx_market != m:
                continue
            dt = _parse_iso_datetime(getattr(tx, "timestamp", None))
            if dt and (last_dt is None or dt > last_dt):
                last_dt = dt
        if last_dt:
            elapsed = (now - last_dt).total_seconds()
            if elapsed < cooldown_seconds:
                return _finalize(
                    name,
                    side_norm,
                    m,
                    quantity,
                    RiskDecision(
                        ok=False,
                        reason="COOLDOWN_ACTIVE",
                        suggestion=(
                            "Wait for cooldown to expire "
                            f"({cooldown_minutes}m)."
                        ),
                        details=_details(
                            m,
                            side_norm,
                            order_type_norm,
                            quantity,
                            price_ref,
                            ask,
                            bid,
                            last_trade,
                            balance,
                            cash_after,
                            base_asset,
                            base_qty,
                            base_after,
                            portfolio_value,
                            exposure_pct,
                            cfg,
                        ),
                    ),
                )

    return _finalize(
        name,
        side_norm,
        m,
        quantity,
        RiskDecision(
            ok=True,
            reason="APPROVED",
            details=_details(
                m,
                side_norm,
                order_type_norm,
                quantity,
                price_ref,
                ask,
                bid,
                last_trade,
                balance,
                cash_after,
                base_asset,
                base_qty,
                base_after,
                portfolio_value,
                exposure_pct,
                cfg,
            ),
        ),
    )


def _details(
    market_id: str,
    side: str,
    order_type: str,
    quantity: float,
    price_ref: float,
    ask: float,
    bid: float,
    last_trade: float,
    balance: float,
    cash_after: float,
    base_asset: str,
    base_before: float,
    base_after: float,
    portfolio_value: float,
    exposure_pct: float,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    return {
        "market_id": market_id,
        "side": side,
        "order_type": order_type,
        "quantity": float(quantity),
        "price_ref": float(price_ref),
        "ask": float(ask),
        "bid": float(bid),
        "last_trade": float(last_trade),
        "notional_myr": float(quantity) * float(price_ref),
        "balance_myr": float(balance),
        "cash_after_myr": float(cash_after),
        "base_asset": base_asset,
        "base_qty_before": float(base_before),
        "base_qty_after": float(base_after),
        "portfolio_value_myr": float(portfolio_value),
        "exposure_pct_after": float(exposure_pct),
        "limits": {
            "max_trade_pct": cfg.get("max_trade_pct"),
            "max_position_pct": cfg.get("max_position_pct"),
            "min_myr_balance": cfg.get("min_myr_balance"),
            "max_notional_myr": cfg.get("max_notional_myr"),
            "max_buys_24h": cfg.get("max_buys_24h"),
            "trade_cooldown_minutes": cfg.get("trade_cooldown_minutes"),
        },
    }


def _finalize(
    name: str,
    side: str,
    market_id: str,
    quantity: float,
    decision: RiskDecision,
) -> RiskDecision:
    try:
        write_log(
            name,
            "risk_guard",
            (
                f"{side} {market_id} qty={quantity} "
                f"ok={decision.ok} reason={decision.reason}"
            ),
        )
    except Exception:
        pass
    return decision
