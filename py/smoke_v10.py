"""End-to-end smoke test for the v10 Command Center API.

Starts the real aiohttp application on an ephemeral local port with a temporary
paper database, then exercises every read-only endpoint, the optional token auth
and the analytics page.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
for _path in (ROOT, PROJECT_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

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
        assert '"v12.0"' in health, health

        state = await fetch(session, "/api/state")
        for key in ("strategyProfile", "priceCache", "security", "totpEnabled", "live_trading_stage"):
            assert f'"{key}"' in state, key

        risk = await fetch(session, "/api/portfolio_risk")
        for key in ("concentrationHhi", "var95Sol", "expectedShortfall95Sol", "profitFactor"):
            assert f'"{key}"' in risk, key

        await fetch(session, "/api/metrics")
        await fetch(session, "/api/backtest")
        await fetch(session, "/api/audit?limit=5")
        await fetch(session, "/api/ml/status")
        models = await fetch(session, "/api/llm/models")
        for key in ("reachable", "models", "selection", "active"):
            assert f'"{key}"' in models, key
        llm_status = await fetch(session, "/api/llm/status")
        for key in ("providers", "selection", "active", "retries"):
            assert f'"{key}"' in llm_status, key
        await fetch(session, "/api/llm/loaded")
        async with session.post(
            f"{BASE}/api/llm/select",
            headers={"X-Auth-Token": TOKEN, "Content-Type": "application/json"},
            json={"provider": "auto"},
        ) as resp:
            body = await resp.text()
            assert resp.status == 200 and '"ok": true' in body, body
        await fetch(session, "/api/equity")
        await fetch(session, "/api/journal?limit=5")
        await fetch(session, "/api/circuit_breaker")

        ready = await fetch(session, "/ready")
        assert '"database": true' in ready, ready
        assert '"ok": true' in ready, ready

        metrics = await fetch(session, "/metrics")
        for key in ("bettercalltrader_info", "bettercalltrader_db_up", "bettercalltrader_http_requests_total"):
            assert key in metrics, key
        assert "# TYPE bettercalltrader_http_request_duration_seconds histogram" in metrics

        auth = await fetch(session, "/api/health", token=None, expect=401)
        assert "unauthorized" in auth
        await fetch(session, "/metrics", token=None, expect=401)

        async with session.get(
            f"{BASE}/api/health",
            headers={"Origin": "http://127.0.0.1:9999", "X-Auth-Token": TOKEN},
        ) as resp:
            assert resp.status == 200
            assert resp.headers.get("Access-Control-Allow-Origin") == "http://127.0.0.1:9999"
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"

        async with session.get(
            f"{BASE}/api/health",
            headers={"Origin": "https://evil.example.com", "X-Auth-Token": TOKEN},
        ) as resp:
            assert resp.status == 200
            assert "Access-Control-Allow-Origin" not in resp.headers

        async with session.options(
            f"{BASE}/api/state",
            headers={"Origin": "https://evil.example.com"},
        ) as resp:
            assert resp.status == 403

        async with session.get(f"{BASE}/analytics") as resp:
            html = await resp.text()
            assert resp.status == 200 and "BetterCallTrader" in html

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
