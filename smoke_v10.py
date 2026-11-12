"""End-to-end smoke test for the v10 Command Center API.

Starts the real aiohttp application on an ephemeral local port with a temporary
paper database, then exercises every read-only endpoint, the optional token auth
and the analytics page.
"""

import asyncio
import os
import tempfile
from pathlib import Path

os.environ.setdefault("DASHBOARD_TOKEN", "smoke-token")
os.environ["LIVE_TRADING_ENABLED"] = "false"

import aiohttp  # noqa: E402

import research_bot as rb  # noqa: E402

PORT = 18080
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = os.environ["DASHBOARD_TOKEN"]


async def fetch(session, path, token=TOKEN, expect=200):
    headers = {"X-Auth-Token": token} if token else {}
    async with session.get(f"{BASE}{path}", headers=headers) as resp:
        text = await resp.text()
        assert resp.status == expect, f"{path} -> {resp.status}: {text[:200]}"
        return text


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="at_smoke_"))
    db_path = str(tmp / "smoke.db")
    portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0)
    researcher = rb.ResearchAgent(gemini_api_key=None)
    discovery = rb.DiscoveryAgent()
    executor = rb.SolanaExecutionAgent(live_trading_enabled=False)

    async with aiohttp.ClientSession() as session:
        task = asyncio.create_task(
            rb.start_command_center_server(portfolio, researcher, discovery, executor, session, port=PORT)
        )
        await asyncio.sleep(0.8)

        health = await fetch(session, "/api/health")
        assert '"v10.0"' in health, health

        state = await fetch(session, "/api/state")
        for key in ("strategyProfile", "priceCache", "security"):
            assert f'"{key}"' in state, key

        risk = await fetch(session, "/api/portfolio_risk")
        for key in ("concentrationHhi", "var95Sol", "expectedShortfall95Sol", "profitFactor"):
            assert f'"{key}"' in risk, key

        await fetch(session, "/api/metrics")
        await fetch(session, "/api/backtest")
        await fetch(session, "/api/audit?limit=5")
        await fetch(session, "/api/ml/status")
        await fetch(session, "/api/equity")
        await fetch(session, "/api/journal?limit=5")
        await fetch(session, "/api/circuit_breaker")

        auth = await fetch(session, "/api/health", token=None, expect=401)
        assert "unauthorized" in auth

        async with session.get(f"{BASE}/analytics") as resp:
            html = await resp.text()
            assert resp.status == 200 and "AgenticTrade" in html

        async with session.get(f"{BASE}/") as resp:
            assert resp.status == 200

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    print("SMOKE OK")
    return db_path


if __name__ == "__main__":
    asyncio.run(main())
