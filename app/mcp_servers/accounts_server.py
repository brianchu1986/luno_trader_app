# app/mcp_servers/accounts_server.py
from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP
from app.libs.account import Account, TradeResult, OrderResult, LimitSizeResult

mcp = FastMCP("accounts_server")


# ----------------------------
# helpers
# ----------------------------
def _err(action: str, e: Exception) -> str:
    return f"ERROR[{action}]: {type(e).__name__}: {e}"


def _trade_err(action: str, market_id: str, e: Exception) -> TradeResult:
    return TradeResult(
        ok=False,
        action=action,
        market_id=market_id,
        reason="SERVER_ERROR",
        suggestion=str(e),
    )


def _order_err(action: str, e: Exception) -> OrderResult:
    return OrderResult(
        ok=False,
        action=action,
        reason="SERVER_ERROR",
        suggestion=str(e),
    )


def _size_err(action: str, market_id: str, e: Exception) -> LimitSizeResult:
    return LimitSizeResult(
        ok=False,
        action=action,
        market_id=market_id,
        reason="SERVER_ERROR",
        suggestion=str(e),
    )


async def _to_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# ----------------------------
# read tools (fast / safe)
# ----------------------------
@mcp.tool()
async def get_balance(name: str, refresh: bool = False) -> float:
    """Get the MYR available balance of an account.

    Args:
        name: Account holder name
        refresh: If true, refresh counter balance from Luno (live only)
    """
    acc = Account.get(name)
    if refresh and acc.account_type == "live":
        await _to_thread(acc.refresh_from_luno)
    return float(acc.balance)


@mcp.tool()
async def get_holdings(name: str, refresh: bool = False) -> dict[str, float]:
    """Get crypto holdings (available units) for an account.

    Args:
        name: Account holder name
        refresh: If true, refresh counter balance from Luno (live only)
    """
    acc = Account.get(name)
    if refresh and acc.account_type == "live":
        await _to_thread(acc.refresh_from_luno)
    return acc.holdings


@mcp.tool()
async def refresh_account(name: str) -> str:
    """Refresh counter balance from Luno (source of truth).

    Dry run behavior:
    - Portfolio holdings are kept in DB and not auto-synced.

    Args:
        name: Account holder name
    """
    acc = Account.get(name)
    try:
        if acc.account_type == "live":
            await _to_thread(acc.refresh_from_luno)
            return "OK: Account refreshed"
        return "OK: Portfolio unchanged"
    except Exception as e:
        return _err("refresh_account", e)


@mcp.tool()
async def paper_reset_from_luno(name: str) -> str:
    """Reset paper wallet using current live balances.

    Args:
        name: Account holder name
    """
    acc = Account.get(name)
    try:
        return await _to_thread(acc.paper_reset_from_luno)
    except Exception as e:
        return _err("paper_reset_from_luno", e)


@mcp.tool()
async def get_portfolio_value(name: str, refresh: bool = True) -> float:
    """Compute total portfolio value in MYR.

    Args:
        name: Account holder name
        refresh: If true, refresh counter balance from Luno (live only)
    """
    acc = Account.get(name)
    try:
        if refresh and acc.account_type == "live":
            await _to_thread(acc.refresh_from_luno)
        return await _to_thread(acc.compute_portfolio_value)
    except Exception as e:
        # for MCP, raise-less is better; return NaN-ish sentinel
        # but we keep float type: use -1 to indicate error
        return -1.0


@mcp.tool()
async def snapshot_portfolio_value(name: str, refresh: bool = True) -> str:
    """Compute and store a portfolio value snapshot (timestamp + MYR value).

    Args:
        name: Account holder name
        refresh: If true, refresh counter balance from Luno (live only)
    """
    acc = Account.get(name)
    try:
        if refresh and acc.account_type == "live":
            await _to_thread(acc.refresh_from_luno)
        value = await _to_thread(acc.snapshot_portfolio_value)
        return f"OK: Snapshot saved. portfolio_value=RM {value:.2f}"
    except Exception as e:
        return _err("snapshot_portfolio_value", e)


# ----------------------------
# order sizing + trade tools
# ----------------------------
@mcp.tool()
async def get_estimate_qty(name: str, market_id: str, spend_myr: float) -> TradeResult:
    """Estimate buyable quantity for a MYR market using ASK price.

    Agent guidance:
    - Agents usually decide a MYR budget first.
    - Call this tool to convert spend_myr -> quantity that respects:
    - min_volume
    - volume_scale (rounded down)
    - For live accounts, the tool refreshes the latest MYR balance.
    - If spend_myr exceeds available MYR, it caps to available and sizes off that.
    - Then call buy_pair(market_id, quantity, rationale).
    - Check result.ok before using result.quantity.

    Args:
        name: Account holder name
        market_id: Luno market id (e.g. GRTMYR, XBTMYR)
        spend_myr: MYR budget to spend (> 0). Capped to available MYR if needed.
    """
    acc = Account.get(name)
    try:
        return await _to_thread(acc.get_estimate_qty, market_id, spend_myr)
    except Exception as e:
        return _trade_err("ESTIMATE_BUY_QTY", market_id, e)


@mcp.tool()
async def get_max_limit_buy_qty(
    name: str, market_id: str, price: float, refresh: bool = False
) -> LimitSizeResult:
    """Compute max BUY limit quantity based on available MYR balance.

    Args:
        name: Account holder name
        market_id: Luno market id (e.g. GRTMYR, XBTMYR)
        price: Limit price
        refresh: If true, refresh account from Luno (live only)
    """
    acc = Account.get(name)
    try:
        if refresh and acc.account_type == "live":
            await _to_thread(acc.refresh_from_luno)
        return await _to_thread(acc.get_max_limit_buy_qty, market_id, price)
    except Exception as e:
        return _size_err("MAX_LIMIT_BUY_QTY", market_id, e)


@mcp.tool()
async def get_max_limit_sell_qty(
    name: str, market_id: str, refresh: bool = False
) -> LimitSizeResult:
    """Compute max SELL limit quantity based on available holdings.

    Args:
        name: Account holder name
        market_id: Luno market id (e.g. GRTMYR, XBTMYR)
        refresh: If true, refresh account from Luno (live only)
    """
    acc = Account.get(name)
    try:
        if refresh and acc.account_type == "live":
            await _to_thread(acc.refresh_from_luno)
        return await _to_thread(acc.get_max_limit_sell_qty, market_id)
    except Exception as e:
        return _size_err("MAX_LIMIT_SELL_QTY", market_id, e)


@mcp.tool()
async def buy_pair(
    name: str, market_id: str, quantity: float, rationale: str
) -> TradeResult:
    """Buy base-asset units on a MYR market.

    Args:
        name: Account holder name
        market_id: Luno market id (e.g. GRTMYR, XBTMYR, ETHMYR)
        quantity: Base-asset quantity (e.g. 28.0)
        rationale: Reason for the trade
    """
    acc = Account.get(name)
    try:
        return await _to_thread(acc.buy_pair, market_id, quantity, rationale)
    except Exception as e:
        return _trade_err("BUY", market_id, e)


@mcp.tool()
async def sell_pair(
    name: str, market_id: str, quantity: float, rationale: str
) -> TradeResult:
    """Sell base-asset units on a MYR market.

    Args:
        name: Account holder name
        market_id: Luno market id (e.g. GRTMYR, XBTMYR, ETHMYR)
        quantity: Base-asset quantity to sell
        rationale: Reason for the trade
    """
    acc = Account.get(name)
    try:
        return await _to_thread(acc.sell_pair, market_id, quantity, rationale)
    except Exception as e:
        return _trade_err("SELL", market_id, e)


@mcp.tool()
async def post_limit_order(
    name: str,
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
    """Place a limit order.

    Args:
        name: Account holder name
        market_id: Luno market id (e.g. GRTMYR, XBTMYR)
        side: BID for a bid (buy) limit order; ASK for an ask (sell) limit order
              (also accepts BUY/SELL)
        price: Limit price
        volume: Base-asset quantity
        rationale: Optional rationale for the order
    """
    acc = Account.get(name)
    try:
        return await _to_thread(
            acc.post_limit_order,
            market_id,
            side,
            price,
            volume,
            rationale,
            post_only,
            time_in_force,
            stop_price,
            stop_direction,
        )
    except Exception as e:
        return _order_err("POST_LIMIT", e)


@mcp.tool()
async def cancel_order(
    name: str, order_id: str | None = None, client_order_id: str | None = None
) -> OrderResult:
    """Cancel a limit order.

    Args:
        name: Account holder name
        order_id: Luno order id
        client_order_id: Client order id (optional)
    """
    acc = Account.get(name)
    try:
        return await _to_thread(acc.cancel_order, order_id, client_order_id)
    except Exception as e:
        return _order_err("CANCEL_ORDER", e)


@mcp.tool()
async def get_order(
    name: str, order_id: str | None = None, client_order_id: str | None = None
) -> OrderResult:
    """Fetch an order by id or client_order_id.

    Args:
        name: Account holder name
        order_id: Luno order id
        client_order_id: Client order id
    """
    acc = Account.get(name)
    try:
        return await _to_thread(acc.get_order, order_id, client_order_id)
    except Exception as e:
        return _order_err("GET_ORDER", e)


@mcp.tool()
async def list_orders(
    name: str,
    created_before: int | None = None,
    limit: int | None = None,
    pair: str | None = None,
    state: str | None = None,
) -> OrderResult:
    """List recent orders.

    Args:
        name: Account holder name
        created_before: Unix ms timestamp filter
        limit: Max number of orders
        pair: Market pair filter (e.g. XBTMYR)
        state: Filter by state (e.g. PENDING)
    """
    acc = Account.get(name)
    try:
        return await _to_thread(
            acc.list_orders, created_before, limit, pair, state
        )
    except Exception as e:
        return _order_err("LIST_ORDERS", e)


# ----------------------------
# history tools
# ----------------------------
@mcp.tool()
async def get_orders_history(
    name: str,
    limit: int | None = None,
) -> dict[str, Any]:
    """Fetch archived orders (completed/canceled) for a trader.

    Args:
        name: Account holder name
        limit: Optional max number of archived orders to return (most recent first)
    """
    acc = Account.get(name)
    try:
        orders = list(acc.orders_history)
        if limit:
            orders = orders[-int(limit) :]
        orders.reverse()
        return {"count": len(acc.orders_history), "orders_history": orders}
    except Exception as e:
        return {"ok": False, "reason": "SERVER_ERROR", "suggestion": str(e)}


# ----------------------------
# trade sync tools
# ----------------------------
@mcp.tool()
async def sync_user_trades(
    name: str,
    pair: str | None = None,
    since: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Sync filled trades using list_user_trades and update tracked limit orders.

    Args:
        name: Account holder name
        pair: Market pair filter (e.g. XBTMYR). If omitted, uses tracked limit orders.
        since: Filter trades on/after this timestamp (Unix milliseconds)
        limit: Limit number of trades
    """
    acc = Account.get(name)
    try:
        return await _to_thread(acc.sync_user_trades, pair, since, limit)
    except Exception as e:
        return {"ok": False, "reason": "SERVER_ERROR", "suggestion": str(e)}


# ----------------------------
# account config tools
# ----------------------------
@mcp.tool()
async def set_account_type(name: str, account_type: str) -> str:
    """Set account execution mode.

    Args:
        name: Account holder name
        account_type: "dry_run" or "live"
    """
    acc = Account.get(name)
    try:
        return await _to_thread(acc.set_account_type, account_type)
    except Exception as e:
        return _err("set_account_type", e)


@mcp.tool()
async def change_strategy(name: str, strategy: str) -> str:
    """Change the account's strategy string.

    Args:
        name: Account holder name
        strategy: New strategy label/prompt
    """
    try:
        return await _to_thread(Account.get(name).change_strategy, strategy)
    except Exception as e:
        return _err("change_strategy", e)


# ----------------------------
# resources (read-only)
# ----------------------------
@mcp.resource("accounts://accounts_server/{name}")
async def read_account_resource(name: str) -> str:
    """Read-only account report (markdown string)."""
    acc = Account.get(name.lower())
    return acc.report()


@mcp.resource("accounts://strategy/{name}")
async def read_strategy_resource(name: str) -> str:
    """Read-only strategy string."""
    return Account.get(name.lower()).get_strategy()


# IMPORTANT:
# Do NOT call mcp.run() inside Spyder/Jupyter (it already runs an event loop).
# Run this server from terminal:
#   python -m app.mcp_servers.accounts_server
#
if __name__ == "__main__":
    # print("✅ accounts_server MCP started (stdio)")
    mcp.run(transport="stdio")
