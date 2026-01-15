# app/mcp_servers/mcp_params.py
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# -------------------------------------------------
# Paths (safe + explicit)
# -------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent  # adjust if needed

MCP_DIR = BASE_DIR
MEMORY_DIR = PROJECT_ROOT / "memory"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)

# -------------------------------------------------
# Env vars
# -------------------------------------------------
BRAVE_API_KEY = os.getenv("BRAVE_SEARCH_API")

brave_env = {}
if BRAVE_API_KEY:
    brave_env["BRAVE_API_KEY"] = BRAVE_API_KEY

# -------------------------------------------------
# Trader MCP servers
# -------------------------------------------------
# Use module execution so imports work reliably
trader_mcp_server_params = [
    {
        "command": "uv",
        "args": ["run", "-m", "app.mcp_servers.accounts_server"],
    },
    {
        "command": "uv",
        "args": ["run", "-m", "app.mcp_servers.market_server"],
    },
]


# -------------------------------------------------
# Researcher MCP servers
# -------------------------------------------------
def researcher_mcp_server_params(name: str):
    params = [
        # # Simple fetch server
        # {
        #     "command": "uvx",
        #     "args": ["mcp-server-fetch"],
        # },
    ]

    # # Brave search (only if API key exists)
    # if brave_env:
    #     params.append(
    #         {
    #             "command": "npx",
    #             "args": ["-y", "@modelcontextprotocol/server-brave-search"],
    #             "env": brave_env,
    #         }
    #     )

    # # Memory (per-trader persistent memory)
    # params.append(
    #     {
    #         "command": "npx",
    #         "args": ["-y", "mcp-memory-libsql"],
    #         "env": {"LIBSQL_URL": f"file:{MEMORY_DIR / f'{name}.db'}"},
    #     }
    # )

    return params
