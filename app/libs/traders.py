# app/libs/traders.py
from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from dotenv import load_dotenv
from openai import AsyncOpenAI

from agents import Agent, Tool, Runner, OpenAIChatCompletionsModel, trace
from agents.mcp import MCPServerStdio

from app.libs.accounts_client import read_accounts_resource, read_strategy_resource
from app.libs.tracers import make_trace_id  
from app.libs.templates import (
    researcher_instructions,
    trader_instructions,
    trade_message,
    rebalance_message,
    research_tool,
)

from mcp_params import trader_mcp_server_params, researcher_mcp_server_params

load_dotenv(override=True)

MAX_TURNS = 30

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

deepseek_client = AsyncOpenAI(base_url=DEEPSEEK_BASE_URL, api_key=DEEPSEEK_API_KEY)


def get_model(model_name: str):
    """
    Return an Agents SDK model config.
    - If model_name contains 'deepseek', route via DeepSeek OpenAI-compatible endpoint.
    - Else return the raw name (Agents SDK will use default OpenAI client).
    """
    if "deepseek" in (model_name or "").lower():
        return OpenAIChatCompletionsModel(model=model_name, openai_client=deepseek_client)
    return model_name


async def build_researcher_agent(mcp_servers, model_name: str) -> Agent:
    return Agent(
        name="Researcher",
        instructions=researcher_instructions(),
        model=get_model(model_name),
        mcp_servers=mcp_servers,
    )


async def build_researcher_tool(mcp_servers, model_name: str) -> Tool:
    researcher = await build_researcher_agent(mcp_servers, model_name)
    return researcher.as_tool(
        tool_name="Researcher",
        tool_description=research_tool(),
    )


def _safe_account_text(account_payload: str) -> str:
    """
    Your accounts resource might return JSON or markdown text.
    We try to parse JSON; if it fails, return as-is.
    """
    try:
        data = json.loads(account_payload)
        # optional: remove big arrays if present
        if isinstance(data, dict):
            data.pop("portfolio_value_time_series", None)
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return account_payload


class Trader:
    def __init__(self, name: str, lastname: str = "Trader", model_name: str = "gpt-5-mini"):
        self.name = name
        self.lastname = lastname
        self.model_name = model_name
        self.agent: Agent | None = None

        # toggle: trade -> rebalance -> trade...
        self.do_trade = True

    async def create_agent(self, trader_mcp_servers, researcher_mcp_servers) -> Agent:
        researcher_tool = await build_researcher_tool(researcher_mcp_servers, self.model_name)

        self.agent = Agent(
            name=self.name,
            instructions=trader_instructions(self.name),
            model=get_model(self.model_name),
            tools=[researcher_tool],
            mcp_servers=trader_mcp_servers,
        )
        return self.agent

    async def get_account_report(self) -> str:
        payload = await read_accounts_resource(self.name)
        return _safe_account_text(payload)

    async def run_agent(self, trader_mcp_servers, researcher_mcp_servers) -> None:
        self.agent = await self.create_agent(trader_mcp_servers, researcher_mcp_servers)

        account = await self.get_account_report()
        strategy = await read_strategy_resource(self.name)

        prompt = trade_message(self.name, strategy, account) if self.do_trade else rebalance_message(self.name, strategy, account)

        await Runner.run(self.agent, prompt, max_turns=MAX_TURNS)

    async def run_with_mcp_servers(self) -> None:
        """
        Open ALL MCP servers under ONE AsyncExitStack so they are properly closed.
        """
        async with AsyncExitStack() as stack:
            trader_mcp_servers = [
                await stack.enter_async_context(
                    MCPServerStdio(params, client_session_timeout_seconds=120)
                )
                for params in trader_mcp_server_params
            ]

            researcher_params_list = researcher_mcp_server_params(self.name)
            researcher_mcp_servers = [
                await stack.enter_async_context(
                    MCPServerStdio(params, client_session_timeout_seconds=120)
                )
                for params in researcher_params_list
            ]

            await self.run_agent(trader_mcp_servers, researcher_mcp_servers)

    async def run_with_trace(self) -> None:
        trace_name = f"{self.name}-trading" if self.do_trade else f"{self.name}-rebalancing"
        trace_id = make_trace_id(self.name.lower())

        # If you have a LogTracer processor somewhere else, you can register it globally in your app bootstrap.
        with trace(trace_name, trace_id=trace_id):
            await self.run_with_mcp_servers()

    async def run(self) -> None:
        try:
            await self.run_with_trace()
        except Exception as e:
            print(f"Error running trader {self.name}: {e}")

        # flip mode each run
        self.do_trade = not self.do_trade
