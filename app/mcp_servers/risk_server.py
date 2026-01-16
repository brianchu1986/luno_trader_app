from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from app.libs.risk_guard import assess_trade

mcp = FastMCP("risk_guard_server")


@mcp.tool()
async def risk_check_trade(
    name: str,
    market_id: str,
    side: str,
    quantity: float,
    order_type: str = "MARKET",
    limit_price: float | None = None,
):
    """Run rule-based risk checks for a proposed trade.

    Args:
        name: Account holder name
        market_id: Luno market id (e.g. XBTMYR)
        side: BUY/SELL (or BID/ASK)
        quantity: Base-asset quantity
        order_type: MARKET or LIMIT
        limit_price: Required if order_type=LIMIT
    """
    return await asyncio.to_thread(
        assess_trade,
        name,
        market_id,
        side,
        quantity,
        order_type,
        limit_price,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
