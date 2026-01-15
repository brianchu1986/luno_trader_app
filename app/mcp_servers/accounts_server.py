# app/mcp_servers/accounts_server.py
from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP
from app.libs.account import Account, TradeResult

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
        refresh: If true, refresh from Luno before returning
    """
    acc = Account.get(name)
    if refresh:
        await _to_thread(acc.refresh_from_luno)
    return float(acc.balance)


@mcp.tool()
async def get_holdings(name: str, refresh: bool = False) -> dict[str, float]:
    """Get crypto holdings (available units) for an account.

    Args:
        name: Account holder name
        refresh: If true, refresh from Luno before returning
    """
    acc = Account.get(name)
    if refresh:
        await _to_thread(acc.refresh_from_luno)
    return acc.holdings


@mcp.tool()
async def refresh_account(name: str) -> str:
    """Refresh balance + holdings from Luno (source of truth).

    Args:
        name: Account holder name
    """
    acc = Account.get(name)
    try:
        await _to_thread(acc.refresh_from_luno)
        return "OK: Account refreshed"
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
        refresh: If true, refresh from Luno first (live) or seed paper wallet if empty (dry_run)
    """
    acc = Account.get(name)
    try:
        if refresh:
            if acc.account_type == "dry_run":
                if acc.paper_balance <= 0 and not acc.paper_holdings:
                    await _to_thread(acc.paper_reset_from_luno)
            else:
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
        refresh: If true, refresh from Luno first (recommended)
    """
    acc = Account.get(name)
    try:
        if refresh:
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
    - Then call buy_pair(market_id, quantity, rationale).
    - Check result.ok before using result.quantity.

    Args:
        name: Account holder name
        market_id: Luno market id (e.g. GRTMYR, XBTMYR)
        spend_myr: MYR budget to spend (> 0)
    """
    acc = Account.get(name)
    try:
        return await _to_thread(acc.get_estimate_qty, market_id, spend_myr)
    except Exception as e:
        return _trade_err("ESTIMATE_BUY_QTY", market_id, e)


@mcp.tool()
async def buy_pair(name: str, market_id: str, quantity: float, rationale: str) -> TradeResult:
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
async def sell_pair(name: str, market_id: str, quantity: float, rationale: str) -> TradeResult:
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
