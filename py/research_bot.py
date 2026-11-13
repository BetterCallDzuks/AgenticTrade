import asyncio
import itertools
import os
import json
import base64
import sqlite3
import sys
import time
import logging
import webbrowser
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Optional
import aiohttp
from aiohttp import web

SCRIPT_DIR = Path(__file__).resolve().parent
# The entry point lives in py/; the package, html/ and .env stay at the repo
# root, so make that root importable before the package imports below.
BASE_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "py" else SCRIPT_DIR
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

HTML_DIR = BASE_DIR / "html"
WEBUI_DIR = HTML_DIR / "webui"


# =====================================================================
# 0. AUTOMATSKO UČITAVANJE .env DATOTEKE & TRAJNO LOGIRANJE
# =====================================================================
def load_local_env(env_filename: str = ".env"):
    env_path = BASE_DIR / env_filename
    if not env_path.exists():
        env_path = Path.cwd() / env_filename

    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(dotenv_path=env_path, override=True)
        except ImportError:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key and val:
                        os.environ[key] = val
        print(f"✅ [.env Loader] Uspješno učitana konfiguracija iz: {env_path}")
    else:
        print("ℹ️ [.env Loader] Datoteka .env nije pronađena, koristim sistemske varijable.")


load_local_env()

# Modular components (incremental migration from the monolith).
from bettercalltrader.config import env_float, env_int, env_bool
from bettercalltrader.costs import CostModel, depth_used_fraction
from bettercalltrader.defaults import COST_DEFAULTS, EXIT_DEFAULTS, STRATEGY_DEFAULTS, config_hash
from bettercalltrader.exits import (
    BREAKEVEN_STOP,
    FAST_INVALIDATION,
    FLOW_EXIT,
    STEP_UP_STOP,
    STOP_LOSS,
    TAKE_PROFIT,
    TAKE_PROFIT_1,
    TIME_STOP,
    TRAILING_STOP,
    WHALE_EXIT,
    ExitPolicy,
)
from bettercalltrader.llm import LLMClient
from bettercalltrader.http import fetch_json_with_retry as _fetch_json_with_retry
from bettercalltrader.http import post_json_with_retry as _post_json_with_retry
from bettercalltrader.models import TokenAnalysisDecision
from bettercalltrader.telegram import TelegramNotifier

# Relative LOG_FILE_PATH values resolve against the project directory, not the
# caller's cwd, so `./run.sh` and a bare `python py/research_bot.py` agree.
LOG_FILE_PATH = Path(os.environ.get("LOG_FILE_PATH") or "bot_history.log")
if not LOG_FILE_PATH.is_absolute():
    LOG_FILE_PATH = BASE_DIR / LOG_FILE_PATH
LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
file_logger = logging.getLogger("BetterCallTraderFileLogger")
file_logger.setLevel(logging.INFO)
if not file_logger.handlers:
    fh = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    file_logger.addHandler(fh)

try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    SOLDERS_AVAILABLE = True
except ImportError:
    SOLDERS_AVAILABLE = False

# =====================================================================
# 0B. v9.0 POMOĆNE FUNKCIJE (vrijeme, env, paper fill model, ikone izlaza)
# =====================================================================

# env helpers are now provided by bettercalltrader.config

# Time helpers are now provided by bettercalltrader.timeutils.
from bettercalltrader.timeutils import (  # noqa: E402
    BOT_TIMEZONE_NAME,
    fmt_local,
    fmt_utc,
    local_day_bounds,
    to_local as to_local,
    utc_now,
)

# Fill model and exit metadata are now provided by bettercalltrader.fills.
from bettercalltrader.fills import (  # noqa: E402
    EXIT_ICONS as EXIT_ICONS,
    LOSING_EXIT_REASONS,
    exit_icon,
    paper_fill as paper_fill,
)

WHALE_SELL_EVENTS: dict = {}


def record_whale_sell(mint: str, wallet: str, label: str, ts: Optional[float] = None):
    WHALE_SELL_EVENTS[mint] = {"wallet": wallet, "label": label, "ts": ts if ts is not None else time.time()}


# =====================================================================
# TelegramNotifier is now provided by bettercalltrader.telegram.



TELEGRAM = TelegramNotifier()

# Optional dashboard protection (disabled when DASHBOARD_TOKEN is empty).
from bettercalltrader.security import (
    MUTATING_METHODS,
    RateLimiter,
    client_ip,
    is_authorized,
    is_mutation_allowed,
    is_origin_allowed,
    parse_allowed_origins,
)
from bettercalltrader.observability import AlertThrottle, audit_event, configure_json_logging, log_event, read_audit
from bettercalltrader.prometheus import MetricsRegistry
from bettercalltrader.shutdown import ShutdownController

DASHBOARD_TOKEN = (os.environ.get("DASHBOARD_TOKEN") or "").strip()
# v11: loopback by default. A non-loopback bind requires DASHBOARD_TOKEN (or an
# explicit ALLOW_INSECURE_DASHBOARD=true opt-out) or startup is refused.
COMMAND_CENTER_HOST = (os.environ.get("COMMAND_CENTER_HOST") or "127.0.0.1").strip()
COMMAND_CENTER_PORT = env_int("COMMAND_CENTER_PORT", 8080)
LOOPBACK_BIND_HOSTS = {"127.0.0.1", "localhost", "::1"}
TRUST_PROXY_HEADERS = env_bool("TRUST_PROXY_HEADERS", False)
API_RATE_LIMIT_PER_MIN = env_int("API_RATE_LIMIT_PER_MIN", 240)
API_RATE_LIMITER = RateLimiter(max_requests=API_RATE_LIMIT_PER_MIN, window_seconds=60.0)
AUDIT_LOG_PATH = os.environ.get("AUDIT_LOG_PATH") or str(BASE_DIR / "bot_audit.jsonl")
ALERT_THROTTLE = AlertThrottle(cooldown_sec=env_float("ALERT_COOLDOWN_SEC", 300.0))
ML_MODEL_PATH = os.environ.get("ML_MODEL_PATH") or str(BASE_DIR / "ml_model.json")

# v11: browser origin allowlist. Defaults to same-origin plus localhost on any
# port; set ALLOWED_ORIGINS="*" (or CORS_ALLOW_ALL=true) for the old behaviour.
CORS_ALLOW_ALL = env_bool("CORS_ALLOW_ALL", False)
ALLOWED_ORIGINS = parse_allowed_origins(os.environ.get("ALLOWED_ORIGINS", ""))
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
    "font-src 'self' data: https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
    "img-src 'self' data:; "
    "connect-src 'self' http://127.0.0.1:* http://localhost:* ws://127.0.0.1:* ws://localhost:*; "
    "frame-ancestors 'self'; base-uri 'self'; object-src 'none'"
)

# v11: in-process Prometheus registry. Scraped through GET /metrics.
START_MONOTONIC = time.monotonic()
HTTP_METRICS = MetricsRegistry()
HTTP_METRICS.describe(
    "bettercalltrader_http_requests_total",
    "HTTP requests handled by the Command Center API.",
    "counter",
)
HTTP_METRICS.describe(
    "bettercalltrader_http_request_duration_seconds",
    "Command Center request latency in seconds.",
    "histogram",
)
HTTP_METRICS.describe("bettercalltrader_uptime_seconds", "Process uptime in seconds.", "gauge")
HTTP_METRICS.describe("bettercalltrader_db_up", "1 when the portfolio database is readable.", "gauge")
HTTP_METRICS.describe("bettercalltrader_trades_total", "Closed trades recorded in the database.", "gauge")
HTTP_METRICS.describe("bettercalltrader_win_rate", "Win rate over closed trades (0-1).", "gauge")
HTTP_METRICS.describe("bettercalltrader_net_pnl_sol", "Realized net PnL in SOL.", "gauge")
HTTP_METRICS.describe("bettercalltrader_open_positions", "Currently open paper positions.", "gauge")
HTTP_METRICS.describe("bettercalltrader_wallet_balance_sol", "Paper wallet balance in SOL.", "gauge")
HTTP_METRICS.describe("bettercalltrader_circuit_breaker_active", "1 when the circuit breaker is tripped.", "gauge")
HTTP_METRICS.describe("bettercalltrader_info", "Build and execution mode information.", "gauge")

from bettercalltrader.price_feed import PriceCache

PRICE_CACHE_ENABLED = env_bool("PRICE_CACHE_ENABLED", True)
PRICE_CACHE = PriceCache(default_ttl_sec=env_float("PRICE_CACHE_TTL_SEC", 3.0))

JSON_LOGS = env_bool("JSON_LOGS", False)
JSON_LOGGER = configure_json_logging(str(BASE_DIR / "bot_history.jsonl")) if JSON_LOGS else None


# =====================================================================
# GLOBALNO STANJE ZA COMMAND CENTER SYNC
# =====================================================================
LIVE_AGENT_LOGS: List[dict] = []
RUNTIME_STATUS = {
    "current_phase": "STARTING",
    "cycle": 0,
    "active_token": None,
    "last_update": ""
}


def set_runtime_phase(phase: str, active_token: Optional[str] = None):
    RUNTIME_STATUS["current_phase"] = phase
    if active_token is not None:
        RUNTIME_STATUS["active_token"] = active_token
    RUNTIME_STATUS["last_update"] = datetime.now().strftime("%H:%M:%S")


def add_agent_log(agent: str, message: str, level: str = "info"):
    ts = datetime.now().strftime("%H:%M:%S")
    LIVE_AGENT_LOGS.insert(0, {
        "time": ts,
        "agent": agent,
        "message": message,
        "level": level
    })
    if len(LIVE_AGENT_LOGS) > 120:
        LIVE_AGENT_LOGS.pop()
    try:
        file_logger.info(f"[{agent}] {message}")
    except Exception:
        pass
    if JSON_LOGGER is not None:
        try:
            log_event(JSON_LOGGER, message, agent=agent, severity=level)
        except Exception:
            pass


# =====================================================================
# 1. PODATKOVNI MODELI
# =====================================================================

# TokenAnalysisDecision is now provided by bettercalltrader.models.

# =====================================================================
# 2. DISCOVERY AGENT ("Oči" - Skenira nove Solana tokene)
# =====================================================================

class DiscoveryAgent:
    DEXSCREENER_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
    DEXSCREENER_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
    FALLBACK_MINTS = [
        "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",  # BONK
        "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",  # WIF
        "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",  # POPCAT
        "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",  # MEW
    ]

    async def fetch_latest_solana_candidates(self, session: aiohttp.ClientSession, limit: int = 5) -> List[str]:
        set_runtime_phase("DEXSCREENER_SCAN")
        print("\n🔍 [Discovery Agent] Skeniram DexScreener za nove Solana tokene...")
        add_agent_log("DISCOVERY", "Povlačim najnovije Solana tokene s DexScreener API-ja...", "info")
        solana_mints = []

        for url in (self.DEXSCREENER_PROFILES_URL, self.DEXSCREENER_BOOSTS_URL):
            try:
                async with session.get(url, timeout=8) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, list):
                            for item in data:
                                if item.get("chainId") == "solana":
                                    mint = item.get("tokenAddress")
                                    if mint and mint not in solana_mints:
                                        solana_mints.append(mint)
                                    if len(solana_mints) >= limit:
                                        break
                if len(solana_mints) >= limit:
                    break
            except Exception as e:
                print(f"⚠️ [Discovery Agent] Upozorenje pri dohvatu ({url}): {e}")

        if not solana_mints:
            solana_mints = self.FALLBACK_MINTS[:limit]
            add_agent_log("DISCOVERY", "Koristim rezervnu listu aktivnih Solana tokena.", "warn")
        else:
            add_agent_log("DISCOVERY", f"Pronađeno {len(solana_mints)} aktivnih Solana kandidata.", "success")

        print(f"✅ [Discovery Agent] Pronađeno {len(solana_mints)} kandidata.")
        return solana_mints


# =====================================================================
# 2B. PUMP.FUN & SMART MONEY WEBSOCKET AGENT (v9.0: Graduation + Whale)
# =====================================================================

class PumpFunStreamAgent:
    def __init__(
        self,
        max_dev_buy_sol: float = 15.0,
        min_initial_sol_in_curve: float = 30.2
    ):
        self.max_dev_buy_sol = max_dev_buy_sol
        self.min_initial_sol_in_curve = min_initial_sol_in_curve

    async def listen_for_events(
        self,
        session: aiohttp.ClientSession,
        tracked_wallets_map: dict,
        duration_seconds: int = 8,
        max_candidates: int = 3
    ) -> List[dict]:
        set_runtime_phase("PUMPFUN_WS_LISTEN")
        wallet_addresses = list(tracked_wallets_map.keys())
        print(
            f"\n⚡ [Pump.fun + Whale WS] Slušam stream ({duration_seconds}s) | "
            f"Praćeni Smart Money novčanici: {len(wallet_addresses)}"
        )
        add_agent_log(
            "PUMP.FUN WS",
            f"Slušam live blokove + pratim {len(wallet_addresses)} Smart Money novčanika ({duration_seconds}s)...",
            "info"
        )
        candidates = []
        seen_mints = set()
        end_time = asyncio.get_event_loop().time() + duration_seconds
        reconnect_attempts = 0

        while asyncio.get_event_loop().time() < end_time and len(candidates) < max_candidates:
            try:
                async with session.ws_connect("wss://pumpportal.fun/api/data", heartbeat=20, timeout=12) as ws:
                    await ws.send_json({"method": "subscribeNewToken"})
                    await ws.send_json({"method": "subscribeMigration"})
                    if wallet_addresses:
                        await ws.send_json({
                            "method": "subscribeAccountTrade",
                            "keys": wallet_addresses
                        })

                    while asyncio.get_event_loop().time() < end_time and len(candidates) < max_candidates:
                        timeout_left = max(0.5, end_time - asyncio.get_event_loop().time())
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=timeout_left)
                        except asyncio.TimeoutError:
                            break

                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            mint = data.get("mint")
                            if not mint:
                                continue

                            tx_type = data.get("txType", "")
                            trader_pubkey = data.get("traderPublicKey", "")

                            if trader_pubkey in tracked_wallets_map and tx_type == "sell":
                                record_whale_sell(mint, trader_pubkey, tracked_wallets_map[trader_pubkey])
                                add_agent_log(
                                    "WHALE TRACKER",
                                    f"🐳 Whale SELL: '{tracked_wallets_map[trader_pubkey]}' prodaje {mint[:6]}...",
                                    "warn"
                                )
                                continue

                            if mint in seen_mints:
                                continue
                            name = data.get("name", "Unknown")
                            symbol = data.get("symbol", "PUMP")
                            v_sol = float(data.get("vSolInBondingCurve", 30.0) or 30.0)
                            mcap_sol = float(data.get("marketCapSol", 0.0) or 0.0)

                            real_sol_in_curve = max(0.0, v_sol - 30.0)
                            bonding_curve_pct = min(100.0, (real_sol_in_curve / 85.0) * 100.0)

                            if trader_pubkey in tracked_wallets_map and tx_type == "buy":
                                whale_label = tracked_wallets_map[trader_pubkey]
                                sol_spent = float(data.get("solAmount", 1.0) or 1.0)
                                add_agent_log(
                                    "WHALE TRACKER",
                                    f"🐋 SMART MONEY BUY! '{whale_label}' kupio ${symbol} ({mint[:6]}...) za {sol_spent:.2f} SOL!",
                                    "success"
                                )
                                seen_mints.add(mint)
                                candidates.append({
                                    "mint": mint,
                                    "source": "WHALE_COPY_SIGNAL",
                                    "whale_label": whale_label,
                                    "whale_wallet": trader_pubkey,
                                    "whale_sol_amount": sol_spent,
                                    "name": name,
                                    "symbol": symbol,
                                    "v_sol_in_curve": v_sol,
                                    "real_sol_in_curve": real_sol_in_curve,
                                    "bonding_curve_pct": bonding_curve_pct,
                                    "mcap_sol": mcap_sol,
                                    "uri": data.get("uri")
                                })
                                continue

                            if tx_type == "migrate" or "raydium" in str(data).lower():
                                add_agent_log("PUMP.FUN WS", f"🎓 Raydium migracija detektirana: ${symbol} ({mint[:6]}...)", "success")
                                seen_mints.add(mint)
                                candidates.append({
                                    "mint": mint,
                                    "source": "PUMPFUN_MIGRATION",
                                    "name": name,
                                    "symbol": symbol,
                                    "v_sol_in_curve": v_sol,
                                    "real_sol_in_curve": 85.0,
                                    "bonding_curve_pct": 100.0,
                                    "mcap_sol": mcap_sol,
                                    "uri": data.get("uri")
                                })
                                continue

                            if tx_type == "create":
                                if real_sol_in_curve > self.max_dev_buy_sol:
                                    add_agent_log(
                                        "PUMP.FUN WS",
                                        f"🛑 Odbacen ${symbol} — Prevelik Dev Buy ({real_sol_in_curve:.1f} SOL)",
                                        "danger"
                                    )
                                    seen_mints.add(mint)
                                    continue

                                if v_sol >= self.min_initial_sol_in_curve:
                                    add_agent_log(
                                        "PUMP.FUN WS",
                                        f"🆕 Uhvaćen novi token: ${symbol} (Krivulja: {bonding_curve_pct:.1f}%)",
                                        "info"
                                    )
                                    seen_mints.add(mint)
                                    candidates.append({
                                        "mint": mint,
                                        "source": "PUMPFUN_LIVE",
                                        "name": name,
                                        "symbol": symbol,
                                        "v_sol_in_curve": v_sol,
                                        "real_sol_in_curve": real_sol_in_curve,
                                        "bonding_curve_pct": bonding_curve_pct,
                                        "mcap_sol": mcap_sol,
                                        "uri": data.get("uri")
                                    })

                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break

            except Exception as e:
                reconnect_attempts += 1
                print(f"⚠️ [Pump.fun WS] WebSocket prekid ili nedostupan ({e}), pokušaj {reconnect_attempts}.")
                if reconnect_attempts >= 3 or asyncio.get_event_loop().time() >= end_time:
                    if ALERT_THROTTLE.should_send("pumpfun_ws"):
                        audit_event(AUDIT_LOG_PATH, "ws_unavailable", stream="pumpfun", attempts=reconnect_attempts, error=str(e))
                        TELEGRAM.enqueue("⚠️ <b>Pump.fun WS nedostupan</b> — bot nastavlja s DEXSCREENER fallbackom.")
                    break
                await asyncio.sleep(min(1.0, end_time - asyncio.get_event_loop().time()))

        print(f"✅ [Pump.fun WS] Uhvaćeno {len(candidates)} live događaja sa streama.")
        return candidates


# =====================================================================
# 3. RESEARCH AGENT ("Mozak" + POPRAVLJENA MINT/FREEZE PROVJERA)
# =====================================================================

class ResearchAgent:
    RUGCHECK_SUMMARY_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
    RUGCHECK_FULL_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
    DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
    PUMPFUN_COIN_URL = "https://frontend-api-v3.pump.fun/coins/{mint}"

    def __init__(self, gemini_api_key: Optional[str] = None, rpc_url: Optional[str] = None):
        # Gemini or any OpenAI-compatible local server; see bettercalltrader.llm.
        self.llm = LLMClient.from_env({**os.environ, "GEMINI_API_KEY": gemini_api_key}) if gemini_api_key \
            else LLMClient.from_env()
        self.api_key = self.llm.config.gemini_api_key or None
        self.model_name = self.llm.model_name
        self.rpc_url = rpc_url or os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        # Kept as the "is an AI configured?" flag used by the dashboard and logs.
        self.client = self.llm if self.llm.available else None

        self.min_liquidity_usd = float(os.environ.get("MIN_LIQUIDITY_USD", "5000.0"))
        self.min_volume_h1_usd = float(os.environ.get("MIN_VOLUME_H1_USD", "2500.0"))
        self.max_top10_holder_pct = float(os.environ.get("MAX_TOP10_HOLDER_PCT", "35.0"))
        self.reject_m5_dump_pct = float(os.environ.get("REJECT_M5_DUMP_PCT", "-8.0"))
        self.max_chase_h1_pct = float(os.environ.get("MAX_CHASE_H1_PCT", "400.0"))
        self.min_liq_fdv_ratio = float(os.environ.get("MIN_LIQ_FDV_RATIO", "0.05"))
        self.min_liq_fdv_apply_fdv = float(os.environ.get("MIN_LIQ_FDV_APPLY_FDV", "50000.0"))
        self.last_eval_mode = "HEURISTIC"
        # Pump.fun tokens that have not graduated yet have no DexScreener pair
        # for a while. A bonding-curve quote keeps confirmation/manual buys
        # working instead of failing closed with "no quote".
        self.curve_enabled = env_bool("PUMPFUN_CURVE_ENABLED", True)
        self.curve_cache_sec = env_float("PUMPFUN_CURVE_CACHE_SEC", 5.0)
        self.curve_miss_cache_sec = env_float("PUMPFUN_CURVE_MISS_CACHE_SEC", 60.0)
        self.curve_sol_price_usd = env_float("PAPER_SOL_PRICE_USD", COST_DEFAULTS["sol_price_usd"])
        self._curve_quotes: dict = {}
        self._curve_misses: dict = {}

    async def get_market_data(self, session: aiohttp.ClientSession, mint: str) -> Optional[dict]:
        if PRICE_CACHE_ENABLED:
            cached = PRICE_CACHE.get(mint)
            if cached:
                return cached
        url = self.DEXSCREENER_TOKEN_URL.format(mint=mint)
        try:
            data = await _fetch_json_with_retry(session, url, timeout=8, retries=3)
            if not data:
                return await self.fetch_pumpfun_curve_quote(session, mint)
            pairs = data.get("pairs")
            if not pairs:
                return await self.fetch_pumpfun_curve_quote(session, mint)

            solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
            if not solana_pairs:
                return await self.fetch_pumpfun_curve_quote(session, mint)

            best_pair = max(
                solana_pairs,
                key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0)
            )

            txns_all = best_pair.get("txns") or {}
            txns_m5 = txns_all.get("m5") or {}
            txns_h1 = txns_all.get("h1") or {}
            flow_ok = isinstance(txns_all.get("m5"), dict) and "buys" in txns_m5 and "sells" in txns_m5
            info = best_pair.get("info", {})
            dex_id = best_pair.get("dexId", "unknown")
            fdv_usd = float(best_pair.get("fdv", 0) or 0)
            is_pump = "pump" in dex_id.lower() or mint.endswith("pump")

            market = {
                "mint": mint,
                "name": best_pair.get("baseToken", {}).get("name", "Unknown"),
                "symbol": best_pair.get("baseToken", {}).get("symbol", "UNKNOWN"),
                "dex_id": dex_id,
                "price_usd": best_pair.get("priceUsd") or "0.00001",
                "fdv_usd": fdv_usd,
                "liquidity_usd": float(best_pair.get("liquidity", {}).get("usd", 0) or 0),
                "volume_m5": float(best_pair.get("volume", {}).get("m5", 0) or 0),
                "volume_h1": float(best_pair.get("volume", {}).get("h1", 0) or 0),
                "buys_m5": int(txns_m5.get("buys", 0)),
                "sells_m5": int(txns_m5.get("sells", 0)),
                "buys_h1": int(txns_h1.get("buys", 0)),
                "sells_h1": int(txns_h1.get("sells", 0)),
                "price_change_m5": float(best_pair.get("priceChange", {}).get("m5", 0) or 0),
                "price_change_h1": float(best_pair.get("priceChange", {}).get("h1", 0) or 0),
                "bonding_curve_pct": min(100.0, round((fdv_usd / 69000.0) * 100.0, 1)) if is_pump else 100.0,
                "websites": [w.get("url") for w in info.get("websites", [])],
                "socials": [
                    {"type": s.get("type"), "url": s.get("url")}
                    for s in info.get("socials", [])
                ],
                "fetched_at": time.time(),
                "flow_ok": flow_ok,
            }
            if PRICE_CACHE_ENABLED:
                PRICE_CACHE.update(mint, market)
            return market
        except Exception as e:
            print(f"⚠️ Greška pri dohvatu DexScreener podataka za {mint}: {e}")
            return await self.fetch_pumpfun_curve_quote(session, mint)

    def _curve_market_from_payload(self, mint: str, payload: dict) -> Optional[dict]:
        """Build a market quote from a Pump.fun bonding-curve payload."""
        if not isinstance(payload, dict):
            return None
        try:
            decimals = int(payload.get("decimals") or 6)
        except (TypeError, ValueError):
            decimals = 6
        divisor = 10 ** max(0, decimals)

        total_supply_raw = _safe_float(payload.get("total_supply")) or 0.0
        token_supply = total_supply_raw / divisor if total_supply_raw > 0 else 0.0

        v_sol_raw = _safe_float(payload.get("virtual_sol_reserves")) or 0.0
        # Reserves are lamports in the HTTP payload but plain SOL in the WS event.
        v_sol = v_sol_raw / 1e9 if v_sol_raw > 1_000_000 else v_sol_raw
        v_tokens_raw = _safe_float(payload.get("virtual_token_reserves")) or 0.0
        v_tokens = v_tokens_raw / divisor if v_tokens_raw > 0 else 0.0

        usd_mcap = _safe_float(payload.get("usd_market_cap")) or 0.0
        if usd_mcap <= 0:
            mcap_sol = _safe_float(payload.get("market_cap")) or 0.0
            if mcap_sol > 0:
                usd_mcap = mcap_sol * self.curve_sol_price_usd

        if usd_mcap > 0 and token_supply > 0:
            price_usd = usd_mcap / token_supply
        elif v_sol > 0 and v_tokens > 0:
            price_usd = (v_sol * self.curve_sol_price_usd) / v_tokens
            if token_supply > 0:
                usd_mcap = price_usd * token_supply
        else:
            return None
        if price_usd <= 0:
            return None

        liquidity_usd = v_sol * self.curve_sol_price_usd if v_sol > 0 else 0.0
        bonding_pct = min(100.0, max(0.0, ((v_sol - 30.0) / 85.0) * 100.0)) if v_sol > 0 else 0.0
        socials = []
        if payload.get("twitter"):
            socials.append({"type": "twitter", "url": payload["twitter"]})
        if payload.get("telegram"):
            socials.append({"type": "telegram", "url": payload["telegram"]})
        websites = [payload["website"]] if payload.get("website") else []

        return {
            "mint": mint,
            "name": payload.get("name") or "Unknown",
            "symbol": payload.get("symbol") or "PUMP",
            "dex_id": "PUMPFUN_CURVE",
            "price_usd": f"{price_usd:.10f}",
            "fdv_usd": usd_mcap,
            "liquidity_usd": liquidity_usd,
            "volume_m5": 0.0,
            "volume_h1": 0.0,
            "buys_m5": 0,
            "sells_m5": 0,
            "buys_h1": 0,
            "sells_h1": 0,
            "price_change_m5": 0.0,
            "price_change_h1": 0.0,
            "bonding_curve_pct": bonding_pct,
            "websites": websites,
            "socials": socials,
            "fetched_at": time.time(),
            "flow_ok": False,
            "curve": True,
            "curve_complete": bool(payload.get("complete")),
        }

    async def fetch_pumpfun_curve_quote(self, session: aiohttp.ClientSession, mint: str) -> Optional[dict]:
        """Fetch a live bonding-curve quote for a token with no DexScreener pair.

        Results are cached briefly and misses are negatively cached so a
        non-Pump token does not pay a request on every monitor tick.
        """
        if not self.curve_enabled or not mint:
            return None
        now = time.time()
        cached = self._curve_quotes.get(mint)
        if cached and now - cached[0] <= self.curve_cache_sec:
            return dict(cached[1])
        missed_at = self._curve_misses.get(mint)
        if missed_at is not None and now - missed_at <= self.curve_miss_cache_sec:
            return None
        try:
            payload = await _fetch_json_with_retry(
                session, self.PUMPFUN_COIN_URL.format(mint=mint), timeout=6, retries=2
            )
        except Exception:
            payload = None
        market = self._curve_market_from_payload(mint, payload or {})
        if not market:
            self._curve_misses[mint] = now
            return None
        self._curve_misses.pop(mint, None)
        self._curve_quotes[mint] = (now, market)
        if PRICE_CACHE_ENABLED:
            PRICE_CACHE.update(mint, market)
        return dict(market)

    def _remember_curve_event(self, market: dict) -> None:
        """Cache a WS/fallback quote so the next confirmation recheck can use it."""
        mint = market.get("mint")
        if not mint:
            return
        self._curve_quotes[mint] = (time.time(), dict(market))
        if PRICE_CACHE_ENABLED:
            PRICE_CACHE.update(mint, market)

    async def build_pumpfun_fallback_market_data(
        self,
        session: aiohttp.ClientSession,
        pf_event: dict,
        sol_price_usd: float = 185.0
    ) -> dict:
        socials = []
        websites = []
        uri = pf_event.get("uri")
        if uri and uri.startswith("http"):
            try:
                async with session.get(uri, timeout=4) as resp:
                    if resp.status == 200:
                        meta = await resp.json(content_type=None)
                        if meta.get("twitter"):
                            socials.append({"type": "twitter", "url": meta.get("twitter")})
                        if meta.get("telegram"):
                            socials.append({"type": "telegram", "url": meta.get("telegram")})
                        if meta.get("website"):
                            websites.append(meta.get("website"))
            except Exception:
                pass

        mcap_sol = pf_event.get("mcap_sol", 30.0)
        fdv_usd = mcap_sol * sol_price_usd
        price_usd = fdv_usd / 1_000_000_000.0
        virtual_liq_usd = pf_event.get("v_sol_in_curve", 30.0) * sol_price_usd

        market = {
            "mint": pf_event["mint"],
            "name": pf_event.get("name", "Unknown"),
            "symbol": pf_event.get("symbol", "PUMP"),
            "dex_id": pf_event.get("source", "pumpfun"),
            "price_usd": f"{price_usd:.10f}",
            "fdv_usd": fdv_usd,
            "liquidity_usd": virtual_liq_usd,
            "volume_m5": pf_event.get("real_sol_in_curve", 1.0) * sol_price_usd,
            "volume_h1": pf_event.get("real_sol_in_curve", 1.0) * sol_price_usd,
            "buys_m5": 15 if pf_event.get("real_sol_in_curve", 0) > 1.5 else 5,
            "sells_m5": 1,
            "buys_h1": 15,
            "sells_h1": 1,
            "price_change_m5": pf_event.get("bonding_curve_pct", 5.0),
            "price_change_h1": pf_event.get("bonding_curve_pct", 5.0),
            "bonding_curve_pct": pf_event.get("bonding_curve_pct", 0.0),
            "whale_signal": pf_event.get("whale_label"),
            "whale_wallet": pf_event.get("whale_wallet"),
            "websites": websites,
            "socials": socials,
            "fetched_at": time.time(),
            "flow_ok": False,
            "curve": True,
        }
        self._remember_curve_event(market)
        return market

    async def get_rpc_top_holders(self, session: aiohttp.ClientSession, mint: str) -> dict:
        supply_payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]}
        largest_payload = {"jsonrpc": "2.0", "id": 2, "method": "getTokenLargestAccounts", "params": [mint]}
        for attempt in range(2):
            try:
                s_json = await _post_json_with_retry(session, self.rpc_url, supply_payload, timeout=5, retries=1)
                l_json = await _post_json_with_retry(session, self.rpc_url, largest_payload, timeout=5, retries=1)
                if not s_json or not l_json:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue

                total_supply = float(
                    s_json.get("result", {}).get("value", {}).get("uiAmount") or 1_000_000_000.0
                )
                accounts = l_json.get("result", {}).get("value", [])
                if not accounts or total_supply <= 0:
                    return {}

                pcts = [round((float(a.get("uiAmount") or 0.0) / total_supply) * 100.0, 2) for a in accounts[:11]]
                if not pcts:
                    return {}

                non_lp_pcts = pcts[1:11] if pcts[0] > 20.0 else pcts[:10]
                top10_sum = round(sum(non_lp_pcts), 2)
                max_single = round(non_lp_pcts[0], 2) if non_lp_pcts else 0.0

                return {
                    "top10_holder_pct": top10_sum,
                    "max_single_holder_pct": max_single,
                    "top5_breakdown_pct": non_lp_pcts[:5]
                }
            except Exception:
                await asyncio.sleep(0.5 * (attempt + 1))
        return {}

    async def get_security_data(self, session: aiohttp.ClientSession, mint: str) -> dict:
        """
        v9.0 POPRAVAK: Kad RugCheck vrati HTTP 200 i 'mintAuthority': null,
        to znači da je Mint Authority USPJEŠNO UGAŠEN (True), a ne None!
        """
        full_url = self.RUGCHECK_FULL_URL.format(mint=mint)
        result = {
            "status": "unknown",
            "rugcheck_score": None,
            "top10_holder_pct": None,
            "max_single_holder_pct": None,
            "top5_breakdown_pct": [],
            "is_bundled": False,
            "bundle_warning": None,
            "mint_authority_revoked": None,
            "freeze_authority_revoked": None,
            "lp_locked_pct": None,
            "dev_sold": False,
            "risks": [],
            "data_complete": False,
        }

        try:
            data = await _fetch_json_with_retry(session, full_url, timeout=7, retries=3)
            if data is not None:
                    result["status"] = "ok"
                    result["rugcheck_score"] = data.get("score")
                    risks = data.get("risks", []) or []
                    result["risks"] = [
                        {
                            "name": r.get("name", ""),
                            "level": r.get("level", "info"),
                            "description": r.get("description", "")
                        }
                        for r in risks
                    ]

                    risk_names_joined = " ".join([r["name"].lower() + " " + r["description"].lower() for r in result["risks"]])

                    # v9.0 POPRAVAK: ispravno očitanje mintAuthority i freezeAuthority iz RugCheck JSON-a
                    token_meta = data.get("token")
                    if isinstance(token_meta, dict):
                        result["mint_authority_revoked"] = (token_meta.get("mintAuthority") is None)
                        result["freeze_authority_revoked"] = (token_meta.get("freezeAuthority") is None)
                    else:
                        if "mint authority" in risk_names_joined:
                            result["mint_authority_revoked"] = False
                        elif result["rugcheck_score"] is not None:
                            result["mint_authority_revoked"] = True

                        if "freeze authority" in risk_names_joined:
                            result["freeze_authority_revoked"] = False
                        elif result["rugcheck_score"] is not None:
                            result["freeze_authority_revoked"] = True

                    top_holders = data.get("topHolders") or []
                    if top_holders:
                        holder_pcts = []
                        for h in top_holders:
                            pct = float(h.get("pct", 0.0) or 0.0)
                            if pct > 24.0 and len(holder_pcts) == 0:
                                continue
                            holder_pcts.append(round(pct, 2))
                            if len(holder_pcts) >= 10:
                                break

                        if holder_pcts:
                            result["top10_holder_pct"] = round(sum(holder_pcts), 2)
                            result["max_single_holder_pct"] = holder_pcts[0]
                            result["top5_breakdown_pct"] = holder_pcts[:5]

                    if "bundle" in risk_names_joined or "sniper" in risk_names_joined or "insider" in risk_names_joined:
                        result["is_bundled"] = True
                        result["bundle_warning"] = "Detektiran koordinirani Sniper / Jito Bundle ulaz u ranim blokovima"
                    elif len(result["top5_breakdown_pct"]) >= 4:
                        top4 = result["top5_breakdown_pct"][:4]
                        if max(top4) - min(top4) < 0.25 and sum(top4) > 9.0:
                            result["is_bundled"] = True
                            result["bundle_warning"] = f"Sumnjiva simetrija Top 4 novčanika ({top4}) ukazuje na razbijeni Dev Bundle"

                    if "creator" in risk_names_joined and "sold" in risk_names_joined:
                        result["dev_sold"] = True

        except Exception as e:
            result["status"] = "partial"
            result["error"] = str(e)

        if result["top10_holder_pct"] is None:
            rpc_holders = await self.get_rpc_top_holders(session, mint)
            if rpc_holders:
                result.update(rpc_holders)

        has_holders = result["top10_holder_pct"] is not None
        has_rug = result["rugcheck_score"] is not None or result["status"] == "ok"
        result["data_complete"] = bool(has_holders and has_rug and result["mint_authority_revoked"] is not None)
        if result["status"] == "unknown":
            result["status"] = "incomplete"

        return result

    def pass_hard_filters(self, market: dict, security: dict) -> tuple[bool, str]:
        is_live_pumpfun = str(market.get("dex_id", "")).startswith("PUMPFUN") or str(market.get("dex_id", "")).startswith("WHALE")

        if market["liquidity_usd"] < self.min_liquidity_usd:
            return False, f"Premala likvidnost (${market['liquidity_usd']:,.0f} < ${self.min_liquidity_usd:,.0f})"

        if not is_live_pumpfun and market["volume_h1"] < self.min_volume_h1_usd:
            return False, f"Premali 1h volumen (${market['volume_h1']:,.0f} < ${self.min_volume_h1_usd:,.0f})"

        if not security.get("data_complete") and security.get("rugcheck_score") is None and security.get("top10_holder_pct") is None:
            return False, "Fail-closed: nema potvrđenih RugCheck/RPC sigurnosnih podataka"

        raw_top10 = security.get("top10_holder_pct")
        if raw_top10 is None:
            return False, "Fail-closed: udio Top 10 holdera nije potvrđen"
        top10_pct = float(raw_top10)
        if top10_pct > self.max_top10_holder_pct:
            return False, f"Top 10 holdera drži previše ponude ({top10_pct:.1f}% > max {self.max_top10_holder_pct:.1f}%)"

        mint_revoked = security.get("mint_authority_revoked")
        if mint_revoked is False:
            return False, "KRITIČNO: Mint Authority nije ugašen (kreator može printati nove tokene!)"
        if mint_revoked is None:
            return False, "Fail-closed: Mint Authority nije potvrđen"

        freeze_revoked = security.get("freeze_authority_revoked")
        if freeze_revoked is False:
            return False, "KRITIČNO: Freeze Authority nije ugašen (kreator može zamrznuti novčanike!)"

        danger_risks = [
            r["name"] for r in security.get("risks", []) 
            if r.get("level") == "danger"
        ]
        if danger_risks:
            return False, f"Kritični RugCheck rizici (DANGER): {', '.join(danger_risks)}"

        if security.get("is_bundled"):
            return False, security.get("bundle_warning") or "Detektiran Jito Bundle / Sniper klaster"

        price_change_m5 = float(market.get("price_change_m5", 0) or 0)
        price_change_h1 = float(market.get("price_change_h1", 0) or 0)
        buys_m5 = int(market.get("buys_m5", 0) or 0)
        sells_m5 = int(market.get("sells_m5", 0) or 0)

        if price_change_m5 <= self.reject_m5_dump_pct:
            return False, (
                f"Dump u tijeku: 5m cijena {price_change_m5:+.1f}% "
                f"(prag {self.reject_m5_dump_pct:.1f}%)"
            )

        if sells_m5 > buys_m5 and sells_m5 >= 3:
            return False, f"Prodajni pritisak u 5m ({sells_m5} sells vs {buys_m5} buys)"

        if price_change_h1 >= self.max_chase_h1_pct and price_change_m5 < 0:
            return False, (
                f"Chase filter: već {price_change_h1:+.0f}% u 1h, "
                f"a 5m već pada ({price_change_m5:+.1f}%)"
            )

        fdv = float(market.get("fdv_usd", 0) or 0)
        liq = float(market.get("liquidity_usd", 0) or 0)
        if fdv >= self.min_liq_fdv_apply_fdv and liq > 0 and (liq / fdv) < self.min_liq_fdv_ratio:
            return False, (
                f"Preslab omjer likvidnosti/FDV ({(liq / fdv) * 100:.1f}% "
                f"< {self.min_liq_fdv_ratio * 100:.1f}%)"
            )

        return True, f"Prošao Hard Filtere (Top 10 Holdera: {top10_pct:.1f}%, Mint/Freeze OK)"

    async def ai_evaluate_token(self, market: dict, security: dict) -> TokenAnalysisDecision:
        if not self.llm.available:
            self.last_eval_mode = "HEURISTIC"
            return self._heuristic_fallback(market, security)

        prompt = f"""
        Ti si elitni kvantitativni i on-chain AI analitičar za Solana memecoine.
        Procijeni sljedeći token na temelju stvarnih on-chain, Smart Money i holder podataka:

        TRŽIŠNI PODACI (DexScreener / Whale Tracker):
        {json.dumps(market, indent=2)}

        ON-CHAIN FORENZIKA TOP HOLDERA & BUNDLE ANALIZA:
        {json.dumps(security, indent=2)}

        PRAVILA EVALUACIJE (v9.0 — budi strog, kapital je prioritet):
        1. Analiziraj 'top10_holder_pct' i 'is_bundled'. Ako je top10_holder_pct < 20% i is_bundled == false, to je izvrstan 'green_flag'. Bundle = SKIP.
        2. Ako postoji 'whale_signal' (Smart Money je kupio), to je snažan bikovski signal (+20 Hype Score), ali nije dovoljno samo za BUY ako je cijena već u 5m dumpu.
        3. Analiziraj zamah (buys vs sells u 5m i 1h) i omjer likvidnosti/FDV-a. Omjer ispod 5% na većem FDV-u je red_flag.
        4. Provjeri ima li token X (Twitter), Telegram ili web stranicu. Bez socijala + rana bonding krivulja = najviše WATCH, ne BUY.
        5. NEMOJ davati BUY ako: 5m cijena već pada (price_change_m5 negativan i sells >= buys), ili je token već ekstremno ispumpan u 1h (>400%) i počinje korekcija.
        6. BUY samo uz visoku konfluenciju: čist ugovor, zdravi holderi, kupovni pritisak, prihvatljiv liq/FDV. Inače WATCH ili SKIP.
        7. Budi strog i precizan! Napiši konkretne brojke u 'summary_hr'.
        """

        try:
            decision = await self.llm.complete_json(prompt, TokenAnalysisDecision)
        except Exception as e:  # noqa: BLE001 - any provider failure falls back
            print(f"⚠️ Greška u AI pozivu ({e}), prebacujem na heurističku procjenu...")
            self.last_eval_mode = "HEURISTIC"
            return self._heuristic_fallback(market, security)

        self.model_name = self.llm.model_name
        self.last_eval_mode = "LOCAL_AI" if self.llm.is_local else "AI"
        return decision

    def _heuristic_fallback(self, market: dict, security: dict) -> TokenAnalysisDecision:
        risk_score = 55
        hype_score = 40
        red_flags = []
        green_flags = []

        if market.get("whale_signal"):
            hype_score += 24
            green_flags.append(f"🐋 Smart Money kupnja: '{market['whale_signal']}' ušao u poziciju")

        raw_top10 = security.get("top10_holder_pct")
        top10_pct = float(raw_top10) if raw_top10 is not None else 100.0
        if raw_top10 is None:
            risk_score -= 20
            red_flags.append("Udio Top 10 holdera nije potvrđen (fail-closed heuristika)")
        if top10_pct <= 20.0:
            risk_score += 8
            green_flags.append(f"Zdrava distribucija: Top 10 holdera drži samo {top10_pct:.1f}% ponude")
        elif top10_pct > 26.0:
            risk_score -= 15
            red_flags.append(f"Povišena koncentracija: Top 10 holdera drži {top10_pct:.1f}% ponude")

        if security.get("is_bundled"):
            risk_score -= 22
            red_flags.append(security.get("bundle_warning") or "Sumnja na Jito Bundle / Sniper klaster")
        else:
            green_flags.append("Čista on-chain distribucija (nema detektiranih dev bundleova)")

        warnings = [r["name"] for r in security.get("risks", []) if r.get("level") == "warn"]
        if warnings:
            risk_score -= min(30, len(warnings) * 12)
            red_flags.extend(warnings[:3])

        buys, sells = market["buys_m5"], market["sells_m5"]
        if buys > sells * 1.3 and buys > 8:
            hype_score += 22
            green_flags.append(f"Jak kupovni pritisak u 5m ({buys} kupnji vs {sells} prodaja)")
        elif sells > buys:
            hype_score -= 15
            red_flags.append(f"Više prodaja nego kupnji u zadnjih 5m ({sells} vs {buys})")

        if market["socials"] or market["websites"]:
            hype_score += 14
            green_flags.append("Prisutni verificirani linkovi na društvene mreže / web")
        else:
            risk_score -= 12
            red_flags.append("Nema navedenih društvenih mreža ni weba")

        risk_score = max(5, min(98, risk_score))
        hype_score = max(5, min(98, hype_score))

        has_whale = bool(market.get("whale_signal"))
        if has_whale and risk_score >= 72 and hype_score >= 75:
            decision = "BUY"
        elif risk_score >= 62 and hype_score >= 58:
            decision = "WATCH"
        else:
            decision = "SKIP"

        return TokenAnalysisDecision(
            decision=decision,
            risk_score=risk_score,
            hype_score=hype_score,
            summary_hr=(
                f"[Heuristički mod] Token ${market['symbol']} ima Top 10 holder udio od {top10_pct:.1f}%, "
                f"likvidnost ${market['liquidity_usd']:,.0f} i 5m volumen ${market['volume_m5']:,.0f}. "
                f"Konačna preporuka: {decision}."
            ),
            red_flags=red_flags,
            green_flags=green_flags
        )


# =====================================================================
# 3B. EXECUTION AGENT ("Snajper" - Jupiter v6 + Pump.fun + Jito MEV)
# =====================================================================

class SolanaExecutionAgent:
    SOL_MINT = "So11111111111111111111111111111111111111112"
    JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
    JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
    PUMPPORTAL_LOCAL_TRADE_URL = "https://pumpportal.fun/api/trade-local"
    JITO_MAINNET_RPC = "https://mainnet.block-engine.jito.wtf/api/v1/transactions"

    def __init__(
        self,
        rpc_url: Optional[str] = None,
        private_key_b58: Optional[str] = None,
        live_trading_enabled: Optional[bool] = None,
        slippage_bps: Optional[int] = None,
        priority_fee_sol: Optional[float] = None,
        live_trading_confirmed: Optional[bool] = None,
        max_live_trade_sol: Optional[float] = None,
    ):
        self.rpc_url = rpc_url or os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        # ``""`` means "no key" (it must not silently fall back to the env), while
        # ``None`` keeps the historical env lookup.
        self.private_key_b58 = (
            private_key_b58
            if private_key_b58 is not None
            else os.environ.get("SOLANA_PRIVATE_KEY")
        )
        
        if live_trading_enabled is None:
            self.live_trading_enabled = env_bool("LIVE_TRADING_ENABLED", False)
        else:
            self.live_trading_enabled = live_trading_enabled

        self.slippage_bps = slippage_bps or int(os.environ.get("SLIPPAGE_BPS", "500"))
        self.priority_fee_sol = priority_fee_sol or float(os.environ.get("PRIORITY_FEE_SOL", "0.0015"))
        self.live_trading_confirmed = (
            env_bool("LIVE_TRADING_CONFIRMED", False)
            if live_trading_confirmed is None
            else bool(live_trading_confirmed)
        )
        self.max_live_trade_sol = (
            float(os.environ.get("MAX_LIVE_TRADE_SOL", "0.05"))
            if max_live_trade_sol is None
            else float(max_live_trade_sol)
        )
        self.keypair = None

        if self.private_key_b58 and SOLDERS_AVAILABLE:
            try:
                self.keypair = Keypair.from_base58_string(self.private_key_b58)
            except Exception as e:
                print(f"⚠️ [Execution Agent] Neispravan SOLANA_PRIVATE_KEY format: {e}")

    def get_public_key_str(self) -> str:
        if self.keypair:
            return str(self.keypair.pubkey())
        return "DRY_RUN_WALLET_NOT_CONNECTED"

    async def get_jupiter_quote(
        self,
        session: aiohttp.ClientSession,
        input_mint: str,
        output_mint: str,
        amount_raw: int
    ) -> Optional[dict]:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": str(self.slippage_bps),
            "onlyDirectRoutes": "false",
        }
        try:
            async with session.get(self.JUPITER_QUOTE_URL, params=params, timeout=8) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception:
            return None

    async def execute_buy(
        self,
        session: aiohttp.ClientSession,
        mint: str,
        amount_sol: float,
        dex_id: str = "raydium"
    ) -> dict:
        if self.live_trading_enabled:
            if not self.live_trading_confirmed:
                reason = "LIVE_TRADING_CONFIRMED must be true for live trading"
                audit_event(AUDIT_LOG_PATH, "live_buy_blocked", mint=mint, amountSol=amount_sol, reason=reason)
                return {"status": "BLOCKED", "reason": reason}
            if float(amount_sol) > float(self.max_live_trade_sol):
                reason = f"amount {amount_sol} exceeds MAX_LIVE_TRADE_SOL {self.max_live_trade_sol}"
                audit_event(AUDIT_LOG_PATH, "live_buy_blocked", mint=mint, amountSol=amount_sol, reason=reason)
                return {"status": "BLOCKED", "reason": reason}
        is_pumpfun_curve = "pumpfun" in dex_id.lower() and "migration" not in dex_id.lower()
        lamports = int(amount_sol * 1_000_000_000)

        if is_pumpfun_curve:
            if not self.live_trading_enabled:
                audit_event(AUDIT_LOG_PATH, "dry_run_buy", router="PUMPFUN_BONDING_CURVE", mint=mint, amountSol=amount_sol)
                return {
                    "status": "DRY_RUN_READY",
                    "router": "PUMPFUN_BONDING_CURVE",
                    "amount_sol": amount_sol,
                    "slippage_pct": self.slippage_bps / 100.0,
                    "priority_fee_sol": self.priority_fee_sol
                }
            return await self._execute_pumpportal_tx(
                session, action="buy", mint=mint, amount=amount_sol, denominated_in_sol="true"
            )

        quote = await self.get_jupiter_quote(session, self.SOL_MINT, mint, lamports)
        if not quote or "outAmount" not in quote:
            return {"status": "NO_ROUTE", "router": "JUPITER_V6"}

        out_amount = int(quote.get("outAmount", 0))
        price_impact = float(quote.get("priceImpactPct", 0.0) or 0.0) * 100.0

        if not self.live_trading_enabled:
            audit_event(AUDIT_LOG_PATH, "dry_run_buy", router="JUPITER_V6", mint=mint, amountSol=amount_sol,
                        priceImpactPct=price_impact)
            return {
                "status": "DRY_RUN_QUOTED",
                "router": "JUPITER_V6",
                "out_amount_raw": out_amount,
                "price_impact_pct": price_impact
            }

        return await self._execute_jupiter_swap_tx(session, quote)

    async def _execute_jupiter_swap_tx(self, session: aiohttp.ClientSession, quote: dict) -> dict:
        if not self.keypair or not SOLDERS_AVAILABLE:
            return {"status": "ERROR", "reason": "Nedostaje SOLANA_PRIVATE_KEY ili 'solders' paket"}

        priority_lamports = int(self.priority_fee_sol * 1_000_000_000)
        payload = {
            "quoteResponse": quote,
            "userPublicKey": str(self.keypair.pubkey()),
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": priority_lamports,
            "dynamicComputeUnitLimit": True
        }

        try:
            async with session.post(self.JUPITER_SWAP_URL, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    return {"status": "ERROR", "reason": f"Jupiter swap HTTP {resp.status}: {err_text}"}
                swap_data = await resp.json()
                raw_tx_b64 = swap_data.get("swapTransaction")
                if not raw_tx_b64:
                    return {"status": "ERROR", "reason": "Nema swapTransaction polja u odgovoru"}

                tx_bytes = base64.b64decode(raw_tx_b64)
                return await self._sign_and_send_raw_transaction(session, tx_bytes)
        except Exception as e:
            return {"status": "ERROR", "reason": str(e)}

    async def _execute_pumpportal_tx(
        self,
        session: aiohttp.ClientSession,
        action: str,
        mint: str,
        amount,
        denominated_in_sol: str = "true"
    ) -> dict:
        if not self.keypair or not SOLDERS_AVAILABLE:
            return {"status": "ERROR", "reason": "Nedostaje SOLANA_PRIVATE_KEY ili 'solders' paket"}

        payload = {
            "publicKey": str(self.keypair.pubkey()),
            "action": action,
            "mint": mint,
            "denominatedInSol": denominated_in_sol,
            "amount": amount,
            "slippage": int(self.slippage_bps / 100),
            "priorityFee": self.priority_fee_sol,
            "pool": "pump"
        }

        try:
            async with session.post(self.PUMPPORTAL_LOCAL_TRADE_URL, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    return {"status": "ERROR", "reason": f"PumpPortal HTTP {resp.status}: {err_text}"}
                tx_bytes = await resp.read()
                return await self._sign_and_send_raw_transaction(session, tx_bytes)
        except Exception as e:
            return {"status": "ERROR", "reason": str(e)}

    async def _sign_and_send_raw_transaction(self, session: aiohttp.ClientSession, tx_bytes: bytes) -> dict:
        try:
            unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(unsigned_tx.message, [self.keypair])
            encoded_tx = base64.b64encode(bytes(signed_tx)).decode("utf-8")

            rpc_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [
                    encoded_tx,
                    {
                        "encoding": "base64",
                        "skipPreflight": True,
                        "maxRetries": 3
                    }
                ]
            }

            target_rpc = self.JITO_MAINNET_RPC if "mainnet" in self.rpc_url else self.rpc_url
            async with session.post(target_rpc, json=rpc_payload, timeout=10) as resp:
                res_json = await resp.json()
                if "result" in res_json:
                    sig = res_json["result"]
                    audit_event(AUDIT_LOG_PATH, "live_tx_sent", signature=sig)
                    return {"status": "SENT", "signature": sig}
                audit_event(AUDIT_LOG_PATH, "live_tx_rpc_error", details=res_json)
                return {"status": "RPC_ERROR", "details": res_json}
        except Exception as e:
            audit_event(AUDIT_LOG_PATH, "live_tx_sign_error", reason=str(e))
            return {"status": "SIGN_SEND_ERROR", "reason": str(e)}


# =====================================================================
# 4. PAPER TRADING, SMART MONEY TRACKER & SQLITE BAZA (v9.0 Risk Engine)
# =====================================================================

class PaperTradingManager:
    DEFAULT_WHALES = [
        ("suqh5sHtr8HyJ7q8scBimULPkPpA557prMG47xCHQfK", "Cupsey (High-Freq Alpha)"),
        ("DNfuF1L62WWyW3pNakVkyGGFzVVhj4Yr52jSmdTyeBHm", "Gake (Smart Money Sniper)"),
        ("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1", "Raydium Whale Watcher"),
    ]

    PENDING_STATE_VERSION = 1

    POSITION_EXTRA_COLUMNS = [
        ("signal_price_usd", "REAL"),
        ("entry_slippage_bps", "REAL"),
        ("slippage_sol", "REAL DEFAULT 0.0"),
        ("exit_fill_usd", "REAL"),
        ("liquidity_usd", "REAL"),
        ("last_price_at", "TEXT"),
        ("size_reason", "TEXT"),
        ("conviction", "TEXT"),
        ("whale_wallet", "TEXT"),
        ("whale_label", "TEXT"),
        ("source", "TEXT"),
        ("stop_price_usd", "REAL"),
    ]

    def __init__(
        self,
        db_path: str = "paper_portfolio.db",
        initial_sol: Optional[float] = None,
        trade_size_sol: Optional[float] = None,
        stop_loss_pct: Optional[float] = None,
        tp1_pct: Optional[float] = None,
        trailing_stop_pct: Optional[float] = None,
        clock=None,
    ):
        self.db_path = db_path
        self.clock = clock or utc_now
        self.initial_sol = initial_sol or env_float("INITIAL_PAPER_SOL", 10.0)
        self.trade_size_sol = trade_size_sol or env_float("TRADE_SIZE_SOL", STRATEGY_DEFAULTS["TRADE_SIZE_SOL"])
        self.stop_loss_pct = stop_loss_pct or env_float("STOP_LOSS_PCT", STRATEGY_DEFAULTS["STOP_LOSS_PCT"])
        self.tp1_pct = tp1_pct if tp1_pct is not None else env_float("TP1_PCT", STRATEGY_DEFAULTS["TP1_PCT"])
        self.trailing_stop_pct = trailing_stop_pct or env_float("TRAILING_STOP_PCT", STRATEGY_DEFAULTS["TRAILING_STOP_PCT"])
        self.trailing_stop_pct_after_tp1 = env_float(
            "TRAILING_STOP_PCT_AFTER_TP1", STRATEGY_DEFAULTS["TRAILING_STOP_PCT_AFTER_TP1"]
        )
        self.max_open_positions = env_int("MAX_OPEN_POSITIONS", int(STRATEGY_DEFAULTS["MAX_OPEN_POSITIONS"]))
        self.min_seconds_between_entries = env_int("MIN_SECONDS_BETWEEN_ENTRIES", 60)
        self.stop_loss_cooldown_sec = env_int("STOP_LOSS_COOLDOWN_SEC", 180)
        self.fast_invalidation_pct = env_float("FAST_INVALIDATION_PCT", STRATEGY_DEFAULTS["FAST_INVALIDATION_PCT"])
        self.fast_invalidation_window_sec = env_int("FAST_INVALIDATION_WINDOW_SEC", 90)
        # Shared exit policy knobs (see bettercalltrader.exits).
        self.tp1_fraction = env_float("TP1_FRACTION", EXIT_DEFAULTS["tp1_fraction"])
        self.trailing_arm_gain_pct = env_float("TRAILING_ARM_GAIN_PCT", EXIT_DEFAULTS["trailing_arm_gain_pct"])
        self.final_take_profit_pct = env_float("FINAL_TAKE_PROFIT_PCT", 0.0)
        self.position_monitor_sec = env_float("POSITION_MONITOR_SEC", 4)

        # v8.0 postavke
        self.flow_exit_enabled = env_bool("FLOW_EXIT_ENABLED", True)
        self.flow_exit_min_sells = env_int("FLOW_EXIT_MIN_SELLS", 3)
        self.flow_exit_sell_ratio = env_float("FLOW_EXIT_SELL_RATIO", 1.3)
        self.whale_exit_enabled = env_bool("WHALE_EXIT_ENABLED", True)
        self.whale_event_ttl_sec = env_float("WHALE_EVENT_TTL_SEC", 1800.0)
        self.max_daily_stop_losses = env_int("MAX_DAILY_STOP_LOSSES", int(STRATEGY_DEFAULTS["MAX_DAILY_STOP_LOSSES"]))
        self.max_daily_drawdown_pct = env_float("MAX_DAILY_DRAWDOWN_PCT", STRATEGY_DEFAULTS["MAX_DAILY_DRAWDOWN_PCT"])
        self.max_daily_trades = env_int("MAX_DAILY_TRADES", int(STRATEGY_DEFAULTS["MAX_DAILY_TRADES"]))
        self.entry_confirm_sec = env_float("ENTRY_CONFIRM_SEC", 20)
        self.entry_confirm_max_drop_pct = env_float("ENTRY_CONFIRM_MAX_DROP_PCT", 4.0)
        self.paper_slippage_bps = env_float("PAPER_SLIPPAGE_BPS", COST_DEFAULTS["base_bps"])
        self.paper_exit_slippage_bps = env_float("PAPER_EXIT_SLIPPAGE_BPS", COST_DEFAULTS["exit_base_bps"])
        self.paper_impact_coeff = env_float("PAPER_IMPACT_COEFF", COST_DEFAULTS["impact_coeff"])
        self.paper_impact_exponent = env_float("PAPER_IMPACT_EXPONENT", COST_DEFAULTS["impact_exponent"])
        self.paper_sol_price_usd = env_float("PAPER_SOL_PRICE_USD", COST_DEFAULTS["sol_price_usd"])
        self.paper_amm_fee_bps = env_float("PAPER_AMM_FEE_BPS", COST_DEFAULTS["amm_fee_bps"])
        self.paper_priority_fee_sol = env_float("PAPER_PRIORITY_FEE_SOL", COST_DEFAULTS["priority_fee_sol"])
        self.paper_failed_tx_prob = env_float("PAPER_FAILED_TX_PROB", COST_DEFAULTS["failed_tx_prob"])
        self.size_after_loss_mult = env_float("SIZE_AFTER_LOSS_MULT", 0.5)
        self.size_heuristic_mult = env_float("SIZE_HEURISTIC_MULT", 0.6)
        self.size_ai_only_mult = env_float("SIZE_AI_ONLY_MULT", 0.8)
        self.size_drawdown_mult = env_float("SIZE_DRAWDOWN_MULT", 0.75)
        self.size_drawdown_trigger_pct = env_float("SIZE_DRAWDOWN_TRIGGER_PCT", 1.0)
        self.min_position_sol = env_float("MIN_POSITION_SOL", 0.1)
        raw_max = os.environ.get("MAX_POSITION_SOL", "")
        self._max_position_explicit = raw_max not in (None, "")
        self.max_position_sol = env_float("MAX_POSITION_SOL", self.trade_size_sol)
        self.cluster_cooldown_sec = env_int("CLUSTER_COOLDOWN_SEC", 300)
        self.cluster_open_dd_pct = env_float("CLUSTER_OPEN_DD_PCT", 8.0)
        self.cluster_unproven_sec = env_int("CLUSTER_UNPROVEN_SEC", 90)
        self.wallet_quality_window = env_int("WALLET_QUALITY_WINDOW", 5)
        self.wallet_quality_min_samples = env_int("WALLET_QUALITY_MIN_SAMPLES", 3)
        self.wallet_quality_min_win_rate = env_float("WALLET_QUALITY_MIN_WIN_RATE", 0.4)
        self.stale_price_sec = env_float("STALE_PRICE_SEC", 15)
        self.move_stop_to_breakeven_after_tp1 = env_bool("MOVE_STOP_TO_BREAKEVEN_AFTER_TP1", True)
        self.breakeven_buffer_pct = env_float("BREAKEVEN_BUFFER_PCT", 1.0)

        # v9.0 postavke: Time-Stop & Step-Up Trailing
        self.time_stop_enabled = env_bool("TIME_STOP_ENABLED", True)
        self.max_hold_no_pump_sec = env_int("MAX_HOLD_NO_PUMP_SEC", 900)
        self.time_stop_min_profit_pct = env_float("TIME_STOP_MIN_PROFIT_PCT", 20.0)
        self.step1_trigger_pct = env_float("STEP1_TRIGGER_PCT", 30.0)
        self.step1_floor_pct = env_float("STEP1_FLOOR_PCT", -5.0)
        self.step2_trigger_pct = env_float("STEP2_TRIGGER_PCT", 50.0)
        self.step2_floor_pct = env_float("STEP2_FLOOR_PCT", 15.0)

        # v10.0 strategy profile: fills only env vars that are not explicitly set.
        # An override profile (high_risk) is an explicit risk mode and wins over
        # the environment; explicit constructor arguments still win over both.
        # The profile itself is applied after the database is open, because a
        # Command Center selection is persisted in bot_state and wins over .env.
        from bettercalltrader.strategies import ATTRIBUTE_MAP

        self._profile_baseline = {
            attr: getattr(self, attr) for attr in ATTRIBUTE_MAP.values() if hasattr(self, attr)
        }
        self._profile_explicit = {
            "trade_size_sol": trade_size_sol,
            "stop_loss_pct": stop_loss_pct,
            "tp1_pct": tp1_pct,
            "trailing_stop_pct": trailing_stop_pct,
        }
        self.strategy_profile = "balanced"
        self.high_risk_mode = False

        self.pending_confirmations: dict = {}
        self.last_entry_block: Optional[dict] = None
        self.last_size_info: Optional[dict] = None
        self._stale_logged: set = set()
        self._journal_inserts = 0
        self._init_db()
        stored_profile = self._get_state("strategy_profile")
        self.apply_strategy_profile(
            stored_profile or os.environ.get("STRATEGY_PROFILE", "balanced"),
            refresh=False,
            persist=False,
        )
        for name, value in self._profile_explicit.items():
            if value is not None:
                setattr(self, name, value)
        if not self._max_position_explicit:
            self.max_position_sol = self.trade_size_sol
        self.refresh_config_hash()
        self.restore_pending_confirmations()

    def now(self) -> datetime:
        return self.clock()

    def now_ts(self) -> float:
        return self.now().replace(tzinfo=timezone.utc).timestamp()

    def _effective_config(self) -> dict:
        from bettercalltrader.strategies import ATTRIBUTE_MAP

        return {key: getattr(self, attr) for key, attr in ATTRIBUTE_MAP.items() if hasattr(self, attr)}

    def apply_strategy_profile(
        self,
        name: str,
        refresh: bool = True,
        persist: bool = True,
        force: Optional[bool] = None,
    ) -> dict:
        """Apply a named risk profile, at startup or from the Command Center.

        Standard profiles only fill knobs that are absent from the environment.
        The ``high_risk`` override profile always wins over them. Knobs not
        pinned in the environment are reset to their pre-profile baseline first,
        so switching back from ``high_risk`` restores the balanced (or .env)
        numbers instead of leaving the 1.5 SOL sizing behind.

        ``force=True`` applies every profile value over the environment. The
        Command Center uses it: an explicit click on the dropdown is an explicit
        instruction, and a .env that pins all the strategy keys would otherwise
        make the selector look broken.

        ``persist`` stores the choice in ``bot_state`` so a Command Center
        switch survives a restart (it then wins over ``STRATEGY_PROFILE``).
        """
        from bettercalltrader.strategies import is_override_profile, normalize_profile, resolve_profile

        profile = normalize_profile(name)
        if force is None:
            force = is_override_profile(profile)
        for attr, value in getattr(self, "_profile_baseline", {}).items():
            setattr(self, attr, value)
        self.strategy_profile = profile
        self.high_risk_mode = is_override_profile(profile)
        for attr, value in resolve_profile(os.environ, profile, force=force).items():
            setattr(self, attr, value)
        if not self._max_position_explicit:
            self.max_position_sol = self.trade_size_sol
        if persist:
            try:
                self._set_state("strategy_profile", profile)
            except Exception as e:
                add_agent_log("RISK MGR", f"Profil nije spremljen u bazu: {e}", "warn")
        if refresh:
            self.refresh_config_hash()
        return self.get_strategy_profile_info()

    def refresh_config_hash(self) -> str:
        """Recompute the effective strategy + cost fingerprint after a change."""
        self.config_hash = config_hash(
            self._effective_config(),
            {
                "profile": self.strategy_profile,
                "costModel": self._cost_model("BUY").as_dict(),
                "exitCostModel": self._cost_model("SELL").as_dict(),
                "exitPolicy": self.exit_policy().as_dict(),
            },
        )
        return self.config_hash

    def _cost_model(self, side: str = "BUY") -> CostModel:
        """Build the shared cost model for one side of a paper trade."""
        base = self.paper_exit_slippage_bps if str(side).upper() == "SELL" else self.paper_slippage_bps
        return CostModel(
            base_bps=base,
            amm_fee_bps=self.paper_amm_fee_bps,
            impact_coeff=self.paper_impact_coeff,
            impact_exponent=self.paper_impact_exponent,
            priority_fee_sol=self.paper_priority_fee_sol,
            failed_tx_prob=self.paper_failed_tx_prob,
            sol_price_usd=self.paper_sol_price_usd,
        )

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass
        return conn

    def _init_db(self):
        conn = self._conn()
        c = conn.cursor()
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass

        c.execute("""
            CREATE TABLE IF NOT EXISTS wallet (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                balance_sol REAL NOT NULL,
                realized_pnl_sol REAL NOT NULL DEFAULT 0.0,
                total_trades INTEGER NOT NULL DEFAULT 0,
                winning_trades INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("SELECT COUNT(*) FROM wallet")
        if c.fetchone()[0] == 0:
            c.execute("INSERT INTO wallet (id, balance_sol) VALUES (1, ?)", (self.initial_sol,))

        c.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                mint TEXT PRIMARY KEY,
                symbol TEXT,
                name TEXT,
                entry_price_usd REAL,
                current_price_usd REAL,
                highest_price_usd REAL,
                invested_sol REAL,
                remaining_ratio REAL DEFAULT 1.0,
                tp1_hit INTEGER DEFAULT 0,
                realized_sol REAL DEFAULT 0.0,
                status TEXT DEFAULT 'OPEN',
                opened_at TEXT,
                closed_at TEXT,
                exit_reason TEXT
            )
        """)

        c.execute("PRAGMA table_info(positions)")
        existing_cols = {row[1] for row in c.fetchall()}
        for col, col_type in self.POSITION_EXTRA_COLUMNS:
            if col not in existing_cols:
                c.execute(f"ALTER TABLE positions ADD COLUMN {col} {col_type}")

        c.execute("""
            CREATE TABLE IF NOT EXISTS recent_scans (
                mint TEXT PRIMARY KEY,
                symbol TEXT,
                name TEXT,
                source TEXT,
                market_json TEXT,
                security_json TEXT,
                verdict_json TEXT,
                scanned_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS tracked_wallets (
                address TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                added_at TEXT,
                signals_count INTEGER DEFAULT 0
            )
        """)

        c.execute("SELECT COUNT(*) FROM tracked_wallets")
        if c.fetchone()[0] == 0:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            for addr, label in self.DEFAULT_WHALES:
                c.execute(
                    "INSERT OR IGNORE INTO tracked_wallets (address, label, added_at, signals_count) VALUES (?, ?, ?, 0)",
                    (addr, label, now)
                )

        c.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT NOT NULL,
                symbol TEXT,
                opened_at TEXT,
                closed_at TEXT,
                invested_sol REAL,
                returned_sol REAL,
                net_pnl_sol REAL,
                exit_reason TEXT,
                tp1_hit INTEGER DEFAULT 0,
                signal_price_usd REAL,
                entry_fill_usd REAL,
                exit_fill_usd REAL,
                slippage_sol REAL DEFAULT 0.0,
                size_reason TEXT,
                conviction TEXT,
                whale_wallet TEXT,
                whale_label TEXT,
                source TEXT,
                backfilled INTEGER DEFAULT 0,
                UNIQUE(mint, opened_at)
            )
        """)

        c.execute("PRAGMA table_info(trade_history)")
        th_cols = {row[1] for row in c.fetchall()}
        if "source" not in th_cols:
            c.execute("ALTER TABLE trade_history ADD COLUMN source TEXT")

        c.execute("""
            CREATE TABLE IF NOT EXISTS decision_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                mint TEXT,
                symbol TEXT,
                action TEXT NOT NULL,
                rule TEXT,
                reasons TEXT,
                suggested_size_sol REAL,
                actual_size_sol REAL,
                signal_price_usd REAL,
                fill_price_usd REAL,
                mid_price_usd REAL,
                risk_score INTEGER,
                hype_score INTEGER,
                extra_json TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS wallet_quality (
                address TEXT PRIMARY KEY,
                label TEXT,
                samples INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                net_pnl_sol REAL DEFAULT 0.0,
                recent_json TEXT DEFAULT '[]',
                updated_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                day TEXT PRIMARY KEY,
                equity_sol REAL NOT NULL,
                taken_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_confirmations (
                mint TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL,
                due_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        c.execute("""
            INSERT OR IGNORE INTO trade_history
            (mint, symbol, opened_at, closed_at, invested_sol, returned_sol, net_pnl_sol, exit_reason, tp1_hit,
             signal_price_usd, entry_fill_usd, exit_fill_usd, slippage_sol, size_reason, conviction,
             whale_wallet, whale_label, source, backfilled)
            SELECT mint, symbol, opened_at, closed_at, invested_sol, realized_sol, realized_sol - invested_sol,
                   exit_reason, tp1_hit, COALESCE(signal_price_usd, entry_price_usd), entry_price_usd,
                   COALESCE(exit_fill_usd, current_price_usd), COALESCE(slippage_sol, 0.0),
                   COALESCE(size_reason, 'legacy'), conviction, whale_wallet, whale_label, source, 1
            FROM positions WHERE status = 'CLOSED' AND closed_at IS NOT NULL
        """)

        try:
            from bettercalltrader.migrations import ensure_schema_version
            ensure_schema_version(conn)
        except Exception:
            pass
        conn.commit()
        conn.close()

    def _get_state(self, key: str, default=None):
        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
        row = c.fetchone()
        conn.close()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return default

    def _set_state(self, key: str, value, cur=None):
        payload = json.dumps(value)
        if cur is not None:
            cur.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)", (key, payload))
            return
        conn = self._conn()
        conn.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)", (key, payload))
        conn.commit()
        conn.close()

    # ---- pending entry confirmations: persistence --------------------
    def _pending_row(self, pending: dict) -> tuple:
        verdict = pending.get("verdict")
        if verdict is None:
            verdict_payload = None
        elif hasattr(verdict, "model_dump"):
            verdict_payload = verdict.model_dump()
        else:
            verdict_payload = dict(verdict)
        payload = {
            "version": self.PENDING_STATE_VERSION,
            "mint": pending["mint"],
            "symbol": pending.get("symbol"),
            "name": pending.get("name"),
            "signal_price": float(pending["signal_price"]),
            "created_at": pending["created_at"].replace(tzinfo=timezone.utc).timestamp(),
            "due_at": pending["due_at"].replace(tzinfo=timezone.utc).timestamp(),
            "market": pending.get("market") or {},
            "security": pending.get("security"),
            "verdict": verdict_payload,
            "conviction": pending.get("conviction"),
            "source": pending.get("source"),
            "size_preview": pending.get("size_preview"),
            "size_reasons": pending.get("size_reasons"),
        }
        return (
            pending["mint"],
            json.dumps(payload, sort_keys=True, default=str),
            payload["created_at"],
            payload["due_at"],
            self.now_ts(),
        )

    def _persist_pending(self, pending: dict, cur=None) -> None:
        sql = """
            INSERT OR REPLACE INTO pending_confirmations
            (mint, payload, created_at, due_at, updated_at) VALUES (?, ?, ?, ?, ?)
        """
        row = self._pending_row(pending)
        if cur is not None:
            cur.execute(sql, row)
            return
        conn = self._conn()
        try:
            conn.execute(sql, row)
            conn.commit()
        finally:
            conn.close()

    def _delete_pending_row(self, mint: str, cur=None) -> None:
        sql = "DELETE FROM pending_confirmations WHERE mint = ?"
        if cur is not None:
            cur.execute(sql, (mint,))
            return
        conn = self._conn()
        try:
            conn.execute(sql, (mint,))
            conn.commit()
        finally:
            conn.close()

    def restore_pending_confirmations(self, max_age_sec: Optional[float] = None) -> List[str]:
        """Reload pending entries written before a restart.

        An entry older than ``ENTRY_CONFIRM_RECOVERY_MAX_SEC`` is dropped: the
        price signal behind it is no longer meaningful, and resuming it would
        turn a crash into a stale entry. Entries that are merely due are
        restored and evaluated by the normal confirmation loop.
        """
        if max_age_sec is None:
            max_age_sec = env_float("ENTRY_CONFIRM_RECOVERY_MAX_SEC", 900.0)
        max_age_sec = max(0.0, float(max_age_sec))
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT mint, payload, created_at, due_at FROM pending_confirmations ORDER BY due_at"
            ).fetchall()
        finally:
            conn.close()

        restored: List[str] = []
        now_ts = self.now_ts()
        for mint, payload_text, created_ts, _due_ts in rows:
            try:
                data = json.loads(payload_text)
            except Exception:
                self._delete_pending_row(mint)
                continue
            symbol = data.get("symbol") or "?"
            if data.get("version") != self.PENDING_STATE_VERSION:
                self._delete_pending_row(mint)
                self.journal("CONFIRM_CANCEL", mint=mint, symbol=symbol, rule="RESTORE_VERSION",
                             reasons="Dropped pending entry with an unknown state version")
                continue
            age = now_ts - float(created_ts or 0.0)
            signal_price = _safe_float(data.get("signal_price")) or 0.0
            if signal_price <= 0:
                self._delete_pending_row(mint)
                continue
            if age > max_age_sec:
                self._delete_pending_row(mint)
                self.journal(
                    "CONFIRM_CANCEL", mint=mint, symbol=symbol, rule="CONFIRM_EXPIRED",
                    reasons=f"Pending entry expired while the bot was offline ({age:.0f}s > {max_age_sec:.0f}s)",
                )
                add_agent_log("RISK MGR", f"⌛ Dropped stale pending ${symbol} after restart.", "warn")
                continue

            verdict_payload = data.get("verdict")
            verdict = TokenAnalysisDecision(**verdict_payload) if verdict_payload else None
            self.pending_confirmations[mint] = {
                "mint": data.get("mint") or mint,
                "symbol": symbol,
                "name": data.get("name") or symbol,
                "signal_price": signal_price,
                "created_at": datetime.fromtimestamp(float(created_ts), tz=timezone.utc).replace(tzinfo=None),
                "due_at": datetime.fromtimestamp(float(_due_ts), tz=timezone.utc).replace(tzinfo=None),
                "market": data.get("market") or {},
                "security": data.get("security"),
                "verdict": verdict,
                "conviction": data.get("conviction") or "manual",
                "source": data.get("source"),
                "size_preview": data.get("size_preview"),
                "size_reasons": data.get("size_reasons"),
            }
            restored.append(mint)
            self.journal(
                "CONFIRM", mint=mint, symbol=symbol, rule="ENTRY_CONFIRM_RESTORED",
                reasons="Pending entry restored from disk after restart", signal_price=signal_price,
            )
        if restored:
            add_agent_log("RISK MGR", f"♻️ Restored {len(restored)} pending confirmation(s) from disk.", "info")
        return restored

    def journal(
        self, action: str, mint: Optional[str] = None, symbol: Optional[str] = None, rule: Optional[str] = None,
        reasons=None, suggested_size: Optional[float] = None, actual_size: Optional[float] = None,
        signal_price: Optional[float] = None, fill_price: Optional[float] = None, mid_price: Optional[float] = None,
        risk_score: Optional[int] = None, hype_score: Optional[int] = None, extra: Optional[dict] = None, cur=None,
    ):
        if isinstance(reasons, (list, tuple)):
            reasons = " | ".join(str(r) for r in reasons if r)
        row = (
            fmt_utc(self.now()), mint, symbol, action, rule, reasons, suggested_size, actual_size,
            signal_price, fill_price, mid_price, risk_score, hype_score, json.dumps(extra) if extra else None,
        )
        sql = """
            INSERT INTO decision_journal
            (ts_utc, mint, symbol, action, rule, reasons, suggested_size_sol, actual_size_sol,
             signal_price_usd, fill_price_usd, mid_price_usd, risk_score, hype_score, extra_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        if cur is not None:
            cur.execute(sql, row)
        else:
            conn = self._conn()
            conn.execute(sql, row)
            conn.commit()
            conn.close()
        self._journal_inserts += 1
        if self._journal_inserts % 250 == 0 and cur is None:
            conn = self._conn()
            conn.execute("DELETE FROM decision_journal WHERE id <= (SELECT MAX(id) FROM decision_journal) - 5000")
            conn.commit()
            conn.close()

    def get_journal(self, limit: int = 100) -> List[dict]:
        conn = self._conn()
        c = conn.cursor()
        c.execute("""
            SELECT id, ts_utc, mint, symbol, action, rule, reasons, suggested_size_sol, actual_size_sol,
                   signal_price_usd, fill_price_usd, mid_price_usd, risk_score, hype_score, extra_json
            FROM decision_journal ORDER BY id DESC LIMIT ?
        """, (int(limit),))
        rows = c.fetchall()
        conn.close()
        out = []
        for r in rows:
            ts = self._parse_ts(r[1])
            out.append({
                "id": r[0], "tsUtc": r[1], "tsLocal": fmt_local(ts) if ts else r[1],
                "mint": r[2], "symbol": r[3], "action": r[4], "rule": r[5], "reasons": r[6],
                "suggestedSizeSol": r[7], "actualSizeSol": r[8], "signalPriceUsd": r[9],
                "fillPriceUsd": r[10], "midPriceUsd": r[11], "riskScore": r[12], "hypeScore": r[13],
                "extra": json.loads(r[14]) if r[14] else None,
            })
        return out

    def get_source_analytics(self) -> List[dict]:
        conn = self._conn()
        c = conn.cursor()
        c.execute("""
            SELECT COALESCE(NULLIF(source, ''), 'LEGACY_OR_MANUAL') AS src,
                   COUNT(*) AS trades,
                   SUM(CASE WHEN net_pnl_sol > 0 THEN 1 ELSE 0 END) AS wins,
                   COALESCE(SUM(net_pnl_sol), 0.0) AS net_pnl
            FROM trade_history
            GROUP BY src
            ORDER BY net_pnl DESC
        """)
        rows = c.fetchall()
        conn.close()
        return [
            {
                "source": r[0],
                "trades": r[1],
                "wins": r[2],
                "winRatePct": round((r[2] / r[1]) * 100.0, 1) if r[1] > 0 else 0.0,
                "netPnlSol": round(float(r[3]), 4)
            }
            for r in rows
        ]

    def get_tracked_wallets(self) -> List[dict]:
        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT address, label, added_at, signals_count FROM tracked_wallets ORDER BY signals_count DESC, rowid DESC")
        rows = c.fetchall()
        conn.close()
        return [{"address": r[0], "label": r[1], "addedAt": r[2], "signalsCount": r[3]} for r in rows]

    def get_tracked_wallets_map(self) -> dict:
        return {w["address"]: w["label"] for w in self.get_tracked_wallets()}

    def add_tracked_wallet(self, address: str, label: str) -> bool:
        address = address.strip()
        label = label.strip() or f"Whale {address[:5]}"
        if len(address) < 32:
            return False
        conn = self._conn()
        c = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        c.execute(
            "INSERT OR REPLACE INTO tracked_wallets (address, label, added_at, signals_count) VALUES (?, ?, ?, 0)",
            (address, label, now)
        )
        conn.commit()
        conn.close()
        add_agent_log("WHALE TRACKER", f"🐋 Dodan novi Smart Money novčanik za praćenje: {label} ({address[:6]}...)", "success")
        return True

    def remove_tracked_wallet(self, address: str) -> bool:
        conn = self._conn()
        c = conn.cursor()
        c.execute("DELETE FROM tracked_wallets WHERE address = ?", (address,))
        conn.commit()
        conn.close()
        add_agent_log("WHALE TRACKER", f"Uklonjen novčanik s liste praćenja: {address[:8]}...", "warn")
        return True

    def increment_whale_signal(self, address: str):
        conn = self._conn()
        c = conn.cursor()
        c.execute("UPDATE tracked_wallets SET signals_count = signals_count + 1 WHERE address = ?", (address,))
        conn.commit()
        conn.close()

    def _update_wallet_quality(self, cur, wallet: str, label: Optional[str], net_pnl_sol: float, mint: str, symbol: str):
        cur.execute("SELECT label, samples, wins, losses, net_pnl_sol, recent_json FROM wallet_quality WHERE address = ?", (wallet,))
        row = cur.fetchone()
        if row:
            old_label, samples, wins, losses, net, recent_json = row
            try:
                recent = json.loads(recent_json or "[]")
            except Exception:
                recent = []
        else:
            old_label, samples, wins, losses, net, recent = label, 0, 0, 0, 0.0, []
        is_win = net_pnl_sol > 0
        recent.append({"mint": mint, "symbol": symbol, "pnlSol": round(net_pnl_sol, 6), "win": is_win, "at": fmt_utc(self.now())})
        recent = recent[-20:]
        cur.execute("""
            INSERT OR REPLACE INTO wallet_quality (address, label, samples, wins, losses, net_pnl_sol, recent_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            wallet, label or old_label, samples + 1, wins + (1 if is_win else 0), losses + (0 if is_win else 1),
            (net or 0.0) + net_pnl_sol, json.dumps(recent), fmt_utc(self.now()),
        ))

    def get_wallet_quality(self, address: str, label: Optional[str] = None) -> dict:
        try:
            conn = self._conn()
            c = conn.cursor()
            c.execute("SELECT label, samples, wins, losses, net_pnl_sol, recent_json FROM wallet_quality WHERE address = ?", (address,))
            row = c.fetchone()
            conn.close()
            samples, wins, losses, net, recent = 0, 0, 0, 0.0, []
            if row:
                label = label or row[0]
                samples, wins, losses, net = row[1] or 0, row[2] or 0, row[3] or 0, row[4] or 0.0
                recent = json.loads(row[5] or "[]")
            window = recent[-max(1, int(self.wallet_quality_window)):]
            recent_n = len(window)
            recent_wins = sum(1 for x in window if x.get("win"))
            recent_wr = (recent_wins / recent_n) if recent_n else None
            if recent_n < self.wallet_quality_min_samples:
                trusted = True
                reason = f"neutral ({recent_n}/{self.wallet_quality_min_samples} samples)"
            elif recent_wr >= self.wallet_quality_min_win_rate:
                trusted = True
                reason = f"trusted ({recent_wins}/{recent_n} recent wins)"
            else:
                trusted = False
                reason = f"downweighted ({recent_wins}/{recent_n} recent wins < {self.wallet_quality_min_win_rate * 100:.0f}%)"
            return {
                "address": address, "label": label, "samples": samples, "wins": wins, "losses": losses,
                "netPnlSol": net, "recentSamples": recent_n, "recentWins": recent_wins,
                "recentWinRate": recent_wr, "trusted": trusted, "reason": reason,
            }
        except Exception as e:
            return {
                "address": address, "label": label, "samples": 0, "wins": 0, "losses": 0, "netPnlSol": 0.0,
                "recentSamples": 0, "recentWins": 0, "recentWinRate": None, "trusted": False,
                "reason": f"fail-closed: quality lookup error ({e})",
            }

    def is_wallet_trusted(self, address: Optional[str]) -> tuple:
        if not address:
            return False, "unknown wallet (fail-closed)"
        q = self.get_wallet_quality(address)
        return bool(q["trusted"]), q["reason"]

    def list_wallet_quality(self) -> List[dict]:
        seen = {}
        for w in self.get_tracked_wallets():
            seen[w["address"]] = self.get_wallet_quality(w["address"], w["label"])
        try:
            conn = self._conn()
            c = conn.cursor()
            c.execute("SELECT address, label FROM wallet_quality")
            for addr, label in c.fetchall():
                if addr not in seen:
                    seen[addr] = self.get_wallet_quality(addr, label)
            conn.close()
        except Exception:
            pass
        return list(seen.values())

    def save_scan_result(self, market: dict, security: dict, verdict: TokenAnalysisDecision, source: str):
        conn = self._conn()
        c = conn.cursor()
        now = datetime.now().strftime("%H:%M:%S")
        c.execute("""
            INSERT OR REPLACE INTO recent_scans
            (mint, symbol, name, source, market_json, security_json, verdict_json, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            market["mint"],
            market.get("symbol", "TOKEN"),
            market.get("name", "Unknown"),
            source,
            json.dumps(market),
            json.dumps(security),
            verdict.model_dump_json(),
            now
        ))
        conn.commit()
        conn.close()

    def get_recent_scans(self, limit: int = 40) -> List[dict]:
        conn = self._conn()
        c = conn.cursor()
        c.execute("""
            SELECT mint, source, market_json, security_json, verdict_json, scanned_at
            FROM recent_scans
            ORDER BY rowid DESC LIMIT ?
        """, (limit,))
        rows = c.fetchall()
        conn.close()
        result = []
        for r in rows:
            try:
                result.append({
                    "mint": r[0],
                    "source": r[1],
                    "market": json.loads(r[2]),
                    "security": json.loads(r[3]),
                    "verdict": json.loads(r[4]),
                    "scannedAt": r[5]
                })
            except Exception:
                pass
        return result

    def _depth_used(self, notional_sol: float, liquidity_usd: Optional[float]) -> float:
        """Share of the pool this position consumed at entry."""
        return depth_used_fraction(notional_sol, liquidity_usd, self.paper_sol_price_usd)

    def exit_policy(self) -> ExitPolicy:
        """Snapshot the current risk knobs as the shared exit policy."""
        return ExitPolicy(
            stop_loss_pct=self.stop_loss_pct,
            tp1_pct=self.tp1_pct,
            tp1_fraction=self.tp1_fraction,
            trailing_stop_pct=self.trailing_stop_pct,
            trailing_stop_pct_after_tp1=self.trailing_stop_pct_after_tp1,
            trailing_arm_gain_pct=self.trailing_arm_gain_pct,
            move_stop_to_breakeven_after_tp1=self.move_stop_to_breakeven_after_tp1,
            breakeven_buffer_pct=self.breakeven_buffer_pct,
            step1_trigger_pct=self.step1_trigger_pct,
            step1_floor_pct=self.step1_floor_pct,
            step2_trigger_pct=self.step2_trigger_pct,
            step2_floor_pct=self.step2_floor_pct,
            fast_invalidation_pct=self.fast_invalidation_pct,
            fast_invalidation_window_sec=self.fast_invalidation_window_sec,
            time_stop_enabled=self.time_stop_enabled,
            max_hold_no_pump_sec=self.max_hold_no_pump_sec,
            time_stop_min_profit_pct=self.time_stop_min_profit_pct,
            flow_exit_enabled=self.flow_exit_enabled,
            flow_exit_min_sells=self.flow_exit_min_sells,
            flow_exit_sell_ratio=self.flow_exit_sell_ratio,
            whale_exit_enabled=self.whale_exit_enabled,
            final_take_profit_pct=self.final_take_profit_pct,
        )

    def _effective_stop(self, entry_price: float, highest_price: float, tp1_hit: bool) -> tuple:
        return self.exit_policy().effective_stop(entry_price, highest_price, tp1_hit)

    def get_all_positions(self) -> List[dict]:
        conn = self._conn()
        c = conn.cursor()
        c.execute("""
            SELECT mint, symbol, name, entry_price_usd, current_price_usd, highest_price_usd,
                   invested_sol, remaining_ratio, tp1_hit, realized_sol, status, opened_at, closed_at, exit_reason,
                   signal_price_usd, entry_slippage_bps, slippage_sol, exit_fill_usd, liquidity_usd, last_price_at,
                   size_reason, conviction, whale_wallet, whale_label, source
            FROM positions
            ORDER BY CASE WHEN status = 'OPEN' THEN 0 ELSE 1 END, opened_at DESC
        """)
        rows = c.fetchall()
        conn.close()
        positions = []
        now = self.now()
        for r in rows:
            entry = r[3] or 0.0
            tp1 = bool(r[8])
            last_px_dt = self._parse_ts(r[19])
            stale = None
            stop_px = None
            if r[10] == "OPEN":
                stale = (last_px_dt is None) or ((now - last_px_dt).total_seconds() > self.stale_price_sec)
                stop_px, _, _ = self._effective_stop(entry, r[5] or entry, tp1)
            positions.append({
                "mint": r[0],
                "symbol": r[1],
                "name": r[2],
                "entryPriceUsd": r[3],
                "currentPriceUsd": r[4],
                "highestPriceUsd": r[5],
                "investedSol": r[6],
                "remainingRatio": r[7],
                "tp1Hit": tp1,
                "realizedSol": r[9],
                "status": r[10],
                "openedAt": r[11],
                "closedAt": r[12],
                "exitReason": r[13],
                "exitIcon": exit_icon(r[13]) if r[13] else None,
                "signalPriceUsd": r[14] if r[14] is not None else r[3],
                "entryFillUsd": r[3],
                "entrySlippageBps": r[15] if r[15] is not None else 0.0,
                "slippageBps": r[15] if r[15] is not None else 0.0,
                "slippageSol": r[16] if r[16] is not None else 0.0,
                "exitFillUsd": r[17],
                "liquidityUsd": r[18],
                "lastPriceAt": r[19],
                "priceStale": stale,
                "stopPriceUsd": stop_px,
                "breakevenArmed": bool((tp1 and self.move_stop_to_breakeven_after_tp1 or (stop_px and stop_px >= entry)) and r[10] == "OPEN"),
                "sizeSol": r[6],
                "sizeReason": r[20] or ("legacy (pre-v8)" if r[14] is None else None),
                "conviction": r[21],
                "whaleWallet": r[22],
                "whaleLabel": r[23],
                "source": r[24],
            })
        return positions

    def get_wallet_status(self) -> dict:
        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT balance_sol, realized_pnl_sol, total_trades, winning_trades FROM wallet WHERE id = 1")
        row = c.fetchone()
        c.execute("SELECT COUNT(*), COALESCE(SUM(invested_sol * remaining_ratio), 0) FROM positions WHERE status = 'OPEN'")
        open_count, locked_sol = c.fetchone()
        conn.close()

        return {
            "balance_sol": row[0],
            "locked_sol": locked_sol,
            "realized_pnl_sol": row[1],
            "total_trades": row[2],
            "winning_trades": row[3],
            "open_positions": open_count
        }

    def get_equity_now(self) -> float:
        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT balance_sol FROM wallet WHERE id = 1")
        balance = float(c.fetchone()[0])
        c.execute("SELECT entry_price_usd, current_price_usd, invested_sol, remaining_ratio FROM positions WHERE status = 'OPEN'")
        mark = 0.0
        for entry, cur_px, inv, rem in c.fetchall():
            if entry and entry > 0 and cur_px:
                mark += (inv or 0.0) * (rem or 0.0) * (cur_px / entry)
        conn.close()
        return balance + mark

    @staticmethod
    def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
        if not ts:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(ts, fmt)
            except ValueError:
                continue
        return None

    def count_open_positions(self) -> int:
        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'")
        n = int(c.fetchone()[0])
        conn.close()
        return n

    def _ensure_day_snapshot(self, day: str) -> float:
        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT equity_sol FROM equity_snapshots WHERE day = ?", (day,))
        row = c.fetchone()
        conn.close()
        if row:
            return float(row[0])
        eq = self.get_equity_now()
        conn = self._conn()
        conn.execute("INSERT OR IGNORE INTO equity_snapshots (day, equity_sol, taken_at) VALUES (?, ?, ?)",
                     (day, eq, fmt_utc(self.now())))
        conn.commit()
        conn.close()
        return eq

    @staticmethod
    def breaker_window_expired(trip_day: Optional[str], current_day: str) -> bool:
        return trip_day is None or trip_day != current_day

    def get_circuit_breaker_state(self, persist: bool = True) -> dict:
        now = self.now()
        day, day_start, next_midnight = local_day_bounds(now)
        reset_at = self._parse_ts(self._get_state("breaker_reset_at"))
        since = max(day_start, reset_at) if reset_at and reset_at > day_start else day_start
        since_s = fmt_utc(since)

        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT exit_reason, net_pnl_sol FROM trade_history WHERE closed_at >= ?", (since_s,))
        closed_today = c.fetchall()
        c.execute("SELECT COUNT(*) FROM trade_history WHERE opened_at >= ?", (since_s,))
        closed_entries = int(c.fetchone()[0])
        c.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN' AND opened_at >= ?", (since_s,))
        open_entries = int(c.fetchone()[0])
        conn.close()

        losing_stops = sum(
            1 for reason, net in closed_today
            if reason in LOSING_EXIT_REASONS and (net is not None and net < 0)
        )
        realized_today = sum((net or 0.0) for _, net in closed_today)
        trades_today = closed_entries + open_entries

        baseline = self._ensure_day_snapshot(day)
        equity_now = self.get_equity_now()
        daily_pnl = equity_now - baseline
        daily_pnl_pct = (daily_pnl / baseline * 100.0) if baseline > 0 else 0.0

        reasons = []
        if losing_stops >= self.max_daily_stop_losses:
            reasons.append(f"MAX_DAILY_STOP_LOSSES ({losing_stops}/{self.max_daily_stop_losses} losing stops today)")
        if daily_pnl_pct <= -abs(self.max_daily_drawdown_pct):
            reasons.append(f"MAX_DAILY_DRAWDOWN ({daily_pnl_pct:.2f}% <= -{abs(self.max_daily_drawdown_pct):.2f}%)")
        if trades_today >= self.max_daily_trades:
            reasons.append(f"MAX_DAILY_TRADES ({trades_today}/{self.max_daily_trades} entries today)")

        stored = self._get_state("breaker_trip") or {}
        stored_valid = (
            not self.breaker_window_expired(stored.get("day"), day)
            and (reset_at is None or (self._parse_ts(stored.get("at")) or datetime.min) >= reset_at)
        )
        if reasons and persist:
            merged = list(dict.fromkeys((stored.get("reasons", []) if stored_valid else []) + reasons))
            new_trip = not stored_valid
            self._set_state("breaker_trip", {"day": day, "reasons": merged, "at": stored.get("at") if stored_valid else fmt_utc(now)})
            if new_trip:
                self.journal("BLOCK", rule="CIRCUIT_BREAKER", reasons=["Circuit breaker TRIPPED"] + reasons,
                             extra={"dailyPnlSol": round(daily_pnl, 6), "dailyPnlPct": round(daily_pnl_pct, 3)})
                msg_cb = f"⛔ Circuit breaker TRIPPED: {'; '.join(reasons)} — no new entries until local midnight."
                add_agent_log("RISK MGR", msg_cb, "danger")
                if ALERT_THROTTLE.should_send(f"breaker:{day}"):
                    audit_event(AUDIT_LOG_PATH, "circuit_breaker_tripped", day=day, reasons=merged,
                                dailyPnlSol=round(daily_pnl, 6), dailyPnlPct=round(daily_pnl_pct, 3))
                    TELEGRAM.enqueue(f"🚨 <b>CIRCUIT BREAKER TRIPPED!</b>\n{'; '.join(reasons)}\nNovi ulazi su zaustavljeni do ponoći (ili /reset_breaker).")
            all_reasons = merged
        elif stored_valid:
            all_reasons = list(dict.fromkeys(stored.get("reasons", []) + reasons))
        else:
            all_reasons = reasons

        tripped = bool(all_reasons)
        if tripped and persist and self.pending_confirmations:
            self.cancel_all_pending("CIRCUIT_BREAKER")
        return {
            "tripped": tripped,
            "reasons": all_reasons,
            "day": day,
            "timezone": BOT_TIMEZONE_NAME,
            "losingStopsToday": losing_stops,
            "maxDailyStopLosses": self.max_daily_stop_losses,
            "tradesToday": trades_today,
            "maxDailyTrades": self.max_daily_trades,
            "realizedPnlTodaySol": realized_today,
            "dailyPnlSol": daily_pnl,
            "dailyPnlPct": daily_pnl_pct,
            "maxDailyDrawdownPct": self.max_daily_drawdown_pct,
            "equityBaselineSol": baseline,
            "equityNowSol": equity_now,
            "resetsAtUtc": fmt_utc(next_midnight),
            "resetsAt": fmt_local(next_midnight),
            "manualResetAt": fmt_local(reset_at) if reset_at else None,
        }

    def reset_circuit_breaker(self) -> dict:
        now = self.now()
        day, _, _ = local_day_bounds(now)
        eq = self.get_equity_now()
        conn = self._conn()
        c = conn.cursor()
        self._set_state("breaker_reset_at", fmt_utc(now), cur=c)
        c.execute("DELETE FROM bot_state WHERE key = 'breaker_trip'")
        c.execute("INSERT OR REPLACE INTO equity_snapshots (day, equity_sol, taken_at) VALUES (?, ?, ?)", (day, eq, fmt_utc(now)))
        conn.commit()
        conn.close()
        self.journal("BLOCK", rule="CIRCUIT_BREAKER_RESET", reasons="Manual circuit-breaker reset from Command Center (paper)")
        add_agent_log("RISK MGR", "🔄 Circuit breaker manually reset (paper). Counters restart now.", "warn")
        return self.get_circuit_breaker_state()

    def _last_closed_trade(self) -> Optional[tuple]:
        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT net_pnl_sol, exit_reason, symbol FROM trade_history ORDER BY closed_at DESC, id DESC LIMIT 1")
        row = c.fetchone()
        conn.close()
        return row

    def compute_position_size(self, conviction: str = "manual", breaker: Optional[dict] = None) -> tuple:
        base = float(self.trade_size_sol)
        mult = 1.0
        reasons = [f"base {base:.3f}"]
        last = self._last_closed_trade()
        if last is not None and (last[0] or 0.0) < 0:
            mult *= self.size_after_loss_mult
            reasons.append(f"after loss ({last[2]}) ×{self.size_after_loss_mult:g}")
        conv = (conviction or "manual").lower()
        conv_mult = {
            "high": 1.0,
            "ai": self.size_ai_only_mult,
            "heuristic": self.size_heuristic_mult,
            "manual": 1.0,
        }.get(conv, self.size_heuristic_mult)
        if conv_mult != 1.0:
            mult *= conv_mult
        reasons.append(f"{conv} conviction ×{conv_mult:g}")
        if breaker is None:
            breaker = self.get_circuit_breaker_state(persist=False)
        if (not breaker.get("tripped") and breaker.get("dailyPnlSol", 0.0) < 0
                and breaker.get("dailyPnlPct", 0.0) <= -abs(self.size_drawdown_trigger_pct)):
            mult *= self.size_drawdown_mult
            reasons.append(f"session down {breaker.get('dailyPnlPct', 0.0):.2f}% ×{self.size_drawdown_mult:g}")
        raw = base * mult
        max_pos = self.max_position_sol if self._max_position_explicit else max(self.max_position_sol, base)
        size = min(max(raw, self.min_position_sol), max_pos)
        if abs(size - raw) > 1e-12:
            reasons.append(f"clamped to [{self.min_position_sol:g}, {max_pos:g}]")
        size = round(size, 4)
        return size, reasons, mult

    def check_cluster_guard(self, exclude_pending: Optional[str] = None) -> tuple:
        now = self.now()
        conn = self._conn()
        c = conn.cursor()
        cutoff = fmt_utc(now - timedelta(seconds=self.cluster_cooldown_sec))
        placeholders = ",".join("?" for _ in LOSING_EXIT_REASONS)
        loss_row = None
        if self.cluster_cooldown_sec > 0:
            c.execute(
                f"SELECT symbol, exit_reason, closed_at, net_pnl_sol FROM trade_history "
                f"WHERE closed_at > ? AND exit_reason IN ({placeholders}) AND net_pnl_sol < 0 "
                f"ORDER BY closed_at DESC LIMIT 1",
                (cutoff, *LOSING_EXIT_REASONS),
            )
            loss_row = c.fetchone()
        c.execute("SELECT symbol, entry_price_usd, current_price_usd, opened_at FROM positions WHERE status = 'OPEN'")
        open_rows = c.fetchall()
        conn.close()

        if loss_row:
            closed_dt = self._parse_ts(loss_row[2])
            wait = self.cluster_cooldown_sec - int((now - closed_dt).total_seconds()) if closed_dt else self.cluster_cooldown_sec
            return False, "CLUSTER_LOSS_COOLDOWN", (
                f"Cluster guard: ${loss_row[0]} closed {loss_row[1]} ({loss_row[3]:+.3f} SOL) — "
                f"{max(wait, 0)}s left of {self.cluster_cooldown_sec}s cooldown"
            )
        for sym, entry, cur_px, opened_at in open_rows:
            if entry and cur_px:
                pnl = (cur_px / entry - 1.0) * 100.0
                if pnl <= -abs(self.cluster_open_dd_pct):
                    return False, "CLUSTER_OPEN_DD", (
                        f"Cluster guard: open ${sym} is {pnl:+.1f}% (limit -{abs(self.cluster_open_dd_pct):.1f}%)"
                    )
            opened_dt = self._parse_ts(opened_at)
            if opened_dt and self.cluster_unproven_sec > 0:
                age = (now - opened_dt).total_seconds()
                if age < self.cluster_unproven_sec:
                    return False, "CLUSTER_UNPROVEN", (
                        f"Cluster guard: ${sym} opened {int(age)}s ago, still unproven (<{self.cluster_unproven_sec}s)"
                    )
        if self.cluster_unproven_sec > 0:
            for pm, p in self.pending_confirmations.items():
                if pm == exclude_pending:
                    continue
                age = (now - p["created_at"]).total_seconds()
                if age < self.cluster_unproven_sec:
                    return False, "CLUSTER_UNPROVEN", (
                        f"Cluster guard: ${p['symbol']} entry is pending confirmation ({int(age)}s ago)"
                    )
        return True, "OK", "OK"

    def check_entry_gates(self, exclude_pending: Optional[str] = None, breaker: Optional[dict] = None) -> tuple:
        if breaker is None:
            breaker = self.get_circuit_breaker_state()
        if breaker.get("tripped"):
            return False, "CIRCUIT_BREAKER", "Circuit breaker active: " + "; ".join(breaker.get("reasons", []))

        now = self.now()
        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'")
        open_count = int(c.fetchone()[0])
        c.execute("SELECT MAX(opened_at) FROM positions")
        last_open = c.fetchone()[0]
        c.execute("SELECT MAX(opened_at) FROM trade_history")
        last_hist_open = c.fetchone()[0]
        c.execute(
            "SELECT MAX(closed_at) FROM trade_history WHERE exit_reason IN ('STOP_LOSS', 'FAST_INVALIDATION')"
        )
        last_sl = c.fetchone()[0]
        c.execute(
            "SELECT MAX(closed_at) FROM positions WHERE status = 'CLOSED' AND exit_reason IN ('STOP_LOSS', 'FAST_INVALIDATION')"
        )
        last_sl_pos = c.fetchone()[0]
        conn.close()

        pending_other = [p for m, p in self.pending_confirmations.items() if m != exclude_pending]
        if open_count + len(pending_other) >= self.max_open_positions:
            return False, "MAX_OPEN", (
                f"Max open positions reached ({open_count} open + {len(pending_other)} pending / {self.max_open_positions})"
            )

        last_entry_candidates = [d for d in (self._parse_ts(last_open), self._parse_ts(last_hist_open)) if d]
        last_entry_candidates += [p["created_at"] for p in pending_other]
        last_entry_dt = max(last_entry_candidates) if last_entry_candidates else None
        if last_entry_dt and (now - last_entry_dt).total_seconds() < self.min_seconds_between_entries:
            wait = self.min_seconds_between_entries - int((now - last_entry_dt).total_seconds())
            return False, "ENTRY_SPACING", f"Entry spacing: {wait}s left (min {self.min_seconds_between_entries}s between entries)"

        sl_candidates = [d for d in (self._parse_ts(last_sl), self._parse_ts(last_sl_pos)) if d]
        last_sl_dt = max(sl_candidates) if sl_candidates else None
        if last_sl_dt and (now - last_sl_dt).total_seconds() < self.stop_loss_cooldown_sec:
            wait = self.stop_loss_cooldown_sec - int((now - last_sl_dt).total_seconds())
            return False, "SL_COOLDOWN", f"Stop-loss cooldown: {wait}s left (of {self.stop_loss_cooldown_sec}s)"

        ok, rule, reason = self.check_cluster_guard(exclude_pending=exclude_pending)
        if not ok:
            return False, rule, reason
        return True, "OK", "OK"

    def can_open_new_position(self) -> tuple:
        ok, _, reason = self.check_entry_gates()
        return ok, reason

    def has_position(self, mint: str) -> bool:
        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT 1 FROM positions WHERE mint = ? AND status = 'OPEN'", (mint,))
        exists = c.fetchone() is not None
        conn.close()
        return exists

    def is_market_fresh(self, market: Optional[dict]) -> tuple:
        if not market:
            return False, "no quote (fetch failed)"
        try:
            px = float(market.get("price_usd") or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px <= 0:
            return False, "no valid price"
        fetched = market.get("fetched_at")
        if fetched is None:
            return False, "quote has no timestamp"
        age = self.now_ts() - float(fetched)
        # A backward clock step makes age negative; treat it as stale rather
        # than letting an arbitrarily old quote pass as fresh.
        if age < 0:
            return False, f"quote timestamp is {-age:.0f}s in the future (clock skew)"
        if age > self.stale_price_sec:
            return False, f"quote is {age:.0f}s old (> {self.stale_price_sec:.0f}s)"
        return True, "fresh"

    def _block(self, market: dict, rule: str, reason: str, suggested_size=None, log=True):
        self.last_entry_block = {"rule": rule, "reason": reason, "symbol": market.get("symbol"), "mint": market.get("mint"),
                                 "at": fmt_utc(self.now())}
        self.journal("BLOCK", mint=market.get("mint"), symbol=market.get("symbol"), rule=rule, reasons=reason,
                     suggested_size=suggested_size, mid_price=_safe_float(market.get("price_usd")))
        if log:
            add_agent_log("RISK MGR", f"⛔ Entry blocked for ${market.get('symbol', '?')} [{rule}]: {reason}", "warn")

    def request_entry(
        self, market: dict, security: Optional[dict] = None, verdict: Optional[TokenAnalysisDecision] = None,
        conviction: str = "manual", source: Optional[str] = None, skip_confirm: bool = False,
    ) -> dict:
        mint = market.get("mint")
        symbol = market.get("symbol", "?")
        if self.has_position(mint) or mint in self.pending_confirmations:
            self._block(market, "DUPLICATE", "Already holding or confirming this mint", log=False)
            return {"status": "BLOCKED", "rule": "DUPLICATE", "reason": "already open/pending"}
        fresh, why = self.is_market_fresh(market)
        if not fresh:
            self._block(market, "STALE_PRICE", f"Stale price at signal: {why}")
            return {"status": "BLOCKED", "rule": "STALE_PRICE", "reason": why}
        breaker = self.get_circuit_breaker_state()
        ok, rule, reason = self.check_entry_gates(breaker=breaker)
        size, size_reasons, _ = self.compute_position_size(conviction, breaker=breaker)
        if not ok:
            self._block(market, rule, reason, suggested_size=size)
            return {"status": "BLOCKED", "rule": rule, "reason": reason}

        if skip_confirm or self.entry_confirm_sec <= 0:
            opened = self.open_virtual_position(market, verdict, conviction=conviction, source=source,
                                                signal_price=_safe_float(market.get("price_usd")))
            if opened:
                return {"status": "OPENED", "rule": "OK", "reason": "opened"}
            blk = self.last_entry_block or {}
            return {"status": "BLOCKED", "rule": blk.get("rule", "UNKNOWN"), "reason": blk.get("reason", "")}

        now = self.now()
        signal_price = float(market["price_usd"])
        self.pending_confirmations[mint] = {
            "mint": mint,
            "symbol": symbol,
            "name": market.get("name", symbol),
            "signal_price": signal_price,
            "created_at": now,
            "due_at": now + timedelta(seconds=float(self.entry_confirm_sec)),
            "market": dict(market),
            "security": security,
            "verdict": verdict,
            "conviction": conviction,
            "source": source,
            "size_preview": size,
            "size_reasons": size_reasons,
        }
        self._persist_pending(self.pending_confirmations[mint])
        self.journal(
            "CONFIRM", mint=mint, symbol=symbol, rule="ENTRY_CONFIRM_PENDING",
            reasons=f"Waiting {self.entry_confirm_sec:g}s; cancel if price drops > {self.entry_confirm_max_drop_pct:g}% from signal",
            suggested_size=size, signal_price=signal_price, mid_price=signal_price,
            risk_score=getattr(verdict, "risk_score", None), hype_score=getattr(verdict, "hype_score", None),
            extra={"sizeReasons": size_reasons, "conviction": conviction},
        )
        add_agent_log("RISK MGR", f"⏳ ${symbol}: BUY signal → confirming for {self.entry_confirm_sec:g}s (signal ${signal_price:.10f}).", "info")
        return {"status": "PENDING", "rule": "ENTRY_CONFIRM_PENDING", "reason": f"due in {self.entry_confirm_sec:g}s"}

    def cancel_pending(self, mint: str, rule: str, reason: str, mid_price: Optional[float] = None):
        p = self.pending_confirmations.pop(mint, None)
        if not p:
            return
        self._delete_pending_row(mint)
        self.last_entry_block = {"rule": rule, "reason": reason, "symbol": p["symbol"], "mint": mint, "at": fmt_utc(self.now())}
        self.journal("CONFIRM_CANCEL", mint=mint, symbol=p["symbol"], rule=rule, reasons=reason,
                     suggested_size=p.get("size_preview"), signal_price=p["signal_price"], mid_price=mid_price)
        add_agent_log("RISK MGR", f"❎ CONFIRM_CANCEL ${p['symbol']} [{rule}]: {reason}", "warn")

    def cancel_all_pending(self, rule: str):
        for m in list(self.pending_confirmations.keys()):
            self.cancel_pending(m, rule, "Circuit breaker tripped — pending entries cancelled")

    def get_pending_confirmations(self) -> List[dict]:
        now = self.now()
        out = []
        for p in self.pending_confirmations.values():
            out.append({
                "mint": p["mint"], "symbol": p["symbol"], "signalPriceUsd": p["signal_price"],
                "createdAt": fmt_utc(p["created_at"]), "dueAt": fmt_utc(p["due_at"]),
                "dueAtMs": int(p["due_at"].replace(tzinfo=timezone.utc).timestamp() * 1000),
                "secondsLeft": max(0.0, (p["due_at"] - now).total_seconds()),
                "sizePreviewSol": p.get("size_preview"), "sizeReasons": p.get("size_reasons"),
                "conviction": p.get("conviction"), "maxDropPct": self.entry_confirm_max_drop_pct,
            })
        return out

    def process_confirmation(self, mint: str, fresh_market: Optional[dict], researcher=None) -> dict:
        p = self.pending_confirmations.get(mint)
        if not p:
            return {"status": "NONE"}
        if self.now() < p["due_at"]:
            return {"status": "WAITING", "secondsLeft": (p["due_at"] - self.now()).total_seconds()}

        mid = _safe_float((fresh_market or {}).get("price_usd"))
        breaker = self.get_circuit_breaker_state()
        if breaker.get("tripped"):
            self.cancel_pending(mint, "CIRCUIT_BREAKER", "Circuit breaker tripped during confirmation", mid)
            return {"status": "CANCELLED", "rule": "CIRCUIT_BREAKER"}
        fresh, why = self.is_market_fresh(fresh_market)
        if not fresh:
            self.cancel_pending(mint, "STALE_PRICE", f"Stale price at recheck: {why}", mid)
            return {"status": "CANCELLED", "rule": "STALE_PRICE"}
        change_pct = (mid / p["signal_price"] - 1.0) * 100.0
        if change_pct <= -abs(self.entry_confirm_max_drop_pct):
            self.cancel_pending(mint, "CONFIRM_DROP",
                                f"Price {change_pct:+.2f}% vs signal during {self.entry_confirm_sec:g}s wait (max -{abs(self.entry_confirm_max_drop_pct):g}%)", mid)
            return {"status": "CANCELLED", "rule": "CONFIRM_DROP", "changePct": change_pct}

        market = dict(fresh_market)
        for k in ("whale_signal", "whale_wallet", "whale_signal_info"):
            if p["market"].get(k) is not None and market.get(k) is None:
                market[k] = p["market"][k]
        if researcher is not None and p.get("security") is not None:
            passed, reason = researcher.pass_hard_filters(market, p["security"])
            if not passed:
                self.cancel_pending(mint, "HARD_FILTER", f"Hard filter failed at recheck: {reason}", mid)
                return {"status": "CANCELLED", "rule": "HARD_FILTER", "reason": reason}

        ok, rule, reason = self.check_entry_gates(exclude_pending=mint, breaker=breaker)
        if not ok:
            self.cancel_pending(mint, rule, reason, mid)
            return {"status": "CANCELLED", "rule": rule, "reason": reason}

        self.pending_confirmations.pop(mint, None)
        self.journal("CONFIRM", mint=mint, symbol=p["symbol"], rule="ENTRY_CONFIRMED",
                     reasons=f"Held {change_pct:+.2f}% vs signal after {self.entry_confirm_sec:g}s",
                     signal_price=p["signal_price"], mid_price=mid)
        opened = self.open_virtual_position(
            market, p.get("verdict"), conviction=p.get("conviction", "manual"), source=p.get("source"),
            signal_price=p["signal_price"], gates_checked=True,
        )
        if opened:
            self._delete_pending_row(mint)
            return {"status": "OPENED", "changePct": change_pct}
        self._delete_pending_row(mint)
        blk = self.last_entry_block or {}
        self.journal("CONFIRM_CANCEL", mint=mint, symbol=p["symbol"], rule=blk.get("rule", "OPEN_FAILED"),
                     reasons=blk.get("reason", "open failed"), signal_price=p["signal_price"], mid_price=mid)
        return {"status": "CANCELLED", "rule": blk.get("rule", "OPEN_FAILED")}

    def due_confirmations(self) -> List[str]:
        now = self.now()
        return [m for m, p in self.pending_confirmations.items() if now >= p["due_at"]]

    def open_virtual_position(
        self, market: dict, verdict: Optional[TokenAnalysisDecision] = None, conviction: str = "manual",
        source: Optional[str] = None, signal_price: Optional[float] = None, gates_checked: bool = False,
        size_override: Optional[float] = None,
    ) -> bool:
        price_str = market.get("price_usd")
        if not price_str:
            return False
        mid = float(price_str)
        if mid <= 0:
            return False

        mint = market["mint"]
        symbol = market.get("symbol", "?")
        if self.has_position(mint):
            self._block(market, "DUPLICATE", "Already holding this mint", log=False)
            return False

        fresh, why = self.is_market_fresh(market)
        if not fresh:
            self._block(market, "STALE_PRICE", f"No entry on stale price: {why}")
            return False

        breaker = self.get_circuit_breaker_state()
        ok, rule, reason = self.check_entry_gates(exclude_pending=mint, breaker=breaker)
        if not ok:
            self._block(market, rule, reason)
            return False

        size, size_reasons, _ = self.compute_position_size(conviction, breaker=breaker)
        if size_override is not None:
            size = float(size_override)
            size_reasons = size_reasons + [f"override {size:g}"]

        wallet = self.get_wallet_status()
        if wallet["balance_sol"] < size:
            self._block(market, "INSUFFICIENT_BALANCE", f"Balance {wallet['balance_sol']:.3f} < size {size:.3f}", suggested_size=size)
            return False

        liq = _safe_float(market.get("liquidity_usd"))
        cost_fill = self._cost_model("BUY").entry(mid, size, liq)
        if cost_fill.qty <= 0:
            self._block(
                market,
                "SIZE_TOO_SMALL",
                "size does not cover the flat transaction cost",
                suggested_size=size,
            )
            return False
        fill = cost_fill.price
        slip = cost_fill.total_cost_fraction
        slippage_sol = cost_fill.total_cost_sol
        sig_px = float(signal_price) if signal_price else mid
        whale_wallet = market.get("whale_wallet") if market.get("whale_signal") else None
        whale_label = market.get("whale_signal") or None
        size_reason = " · ".join(size_reasons)
        src_val = source or market.get("dex_id")

        conn = self._conn()
        c = conn.cursor()
        c.execute("UPDATE wallet SET balance_sol = balance_sol - ? WHERE id = 1", (size,))
        now = fmt_utc(self.now())
        c.execute("""
            INSERT OR REPLACE INTO positions
            (mint, symbol, name, entry_price_usd, current_price_usd, highest_price_usd,
             invested_sol, remaining_ratio, tp1_hit, realized_sol, status, opened_at, closed_at, exit_reason,
             signal_price_usd, entry_slippage_bps, slippage_sol, exit_fill_usd, liquidity_usd, last_price_at,
             size_reason, conviction, whale_wallet, whale_label, source, stop_price_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1.0, 0, 0.0, 'OPEN', ?, NULL, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL)
        """, (
            mint, symbol, market.get("name", symbol), fill, mid, mid, size, now,
            sig_px, slip * 10000.0, slippage_sol, liq, now, size_reason, conviction, whale_wallet, whale_label,
            src_val,
        ))
        self.journal(
            "OPEN", mint=mint, symbol=symbol, rule="ENTRY_FILLED",
            reasons=[f"size {size:.4f} SOL ({size_reason})", f"fill {slip * 10000:.0f} bps worse than mid"],
            suggested_size=size, actual_size=size, signal_price=sig_px, fill_price=fill, mid_price=mid,
            risk_score=getattr(verdict, "risk_score", None), hype_score=getattr(verdict, "hype_score", None),
            extra={"slippageBps": round(slip * 10000, 2), "slippageSol": round(slippage_sol, 6), "conviction": conviction,
                   "whale": whale_label, "liquidityUsd": liq, "source": src_val},
            cur=c,
        )
        conn.commit()
        conn.close()
        self.last_size_info = {"symbol": symbol, "sizeSol": size, "reasons": size_reasons, "at": now}
        msg = (f"💸 [PAPER BUY] ${symbol} {size:.3f} SOL | signal ${sig_px:.10f} → fill ${fill:.10f} "
               f"({slip * 10000:.0f} bps, {slippage_sol:.4f} SOL) | {size_reason}")
        print(f"   {msg}")
        add_agent_log("EXECUTION", msg, "success")
        TELEGRAM.enqueue(
            f"🟢 <b>OTVORENA POZICIJA: ${symbol}</b>\n"
            f"💰 Ulog: <b>{size:.3f} SOL</b> ({conviction})\n"
            f"🎯 Ulaz: ${fill:.8f} | Izvor: {src_val}\n"
            f"🔗 <a href='https://dexscreener.com/solana/{mint}'>Otvori na DexScreeneru</a>"
        )
        return True

    def close_position_manually(self, mint: str, exit_reason: str = "MANUAL_WEB_EXIT") -> bool:
        conn = self._conn()
        c = conn.cursor()
        c.execute("""
            SELECT symbol, entry_price_usd, current_price_usd, highest_price_usd, invested_sol, remaining_ratio, realized_sol
            FROM positions WHERE mint = ? AND status = 'OPEN'
        """, (mint,))
        row = c.fetchone()
        if not row:
            conn.close()
            return False
        symbol, entry_price, current_price, highest_price, invested_sol, rem_ratio, realized_sol = row
        price_multiplier = current_price / entry_price if entry_price > 0 else 1.0
        current_value_sol = (invested_sol * rem_ratio) * price_multiplier
        pnl_pct = (price_multiplier - 1.0) * 100.0
        self._close_full_position(
            c, mint, symbol, current_price, highest_price, invested_sol, rem_ratio,
            realized_sol, price_multiplier, current_value_sol, fmt_utc(self.now()), exit_reason, pnl_pct,
        )
        conn.commit()
        conn.close()
        return True

    def reset_portfolio_db(self):
        conn = self._conn()
        c = conn.cursor()
        c.execute("""
            UPDATE wallet SET balance_sol = ?, realized_pnl_sol = 0.0, total_trades = 0, winning_trades = 0 WHERE id = 1
        """, (self.initial_sol,))
        c.execute("DELETE FROM positions")
        c.execute("DELETE FROM trade_history")
        c.execute("DELETE FROM equity_snapshots")
        c.execute("DELETE FROM bot_state WHERE key IN ('breaker_trip', 'breaker_reset_at')")
        c.execute("DELETE FROM pending_confirmations")
        conn.commit()
        conn.close()
        self.pending_confirmations.clear()
        self.journal("BLOCK", rule="PORTFOLIO_RESET", reasons=f"Manual portfolio reset to {self.initial_sol:.2f} SOL")
        add_agent_log("RISK MGR", f"Portfelj u SQLite bazi resetiran na {self.initial_sol:.2f} SOL.", "warn")

    async def monitor_open_positions(self, session: aiohttp.ClientSession, researcher: ResearchAgent):
        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT mint FROM positions WHERE status = 'OPEN'")
        mints = [r[0] for r in c.fetchall()]
        conn.close()

        if not mints:
            return

        markets = await asyncio.gather(
            *(researcher.get_market_data(session, m) for m in mints), return_exceptions=True
        )
        for mint, market in zip(mints, markets):
            if isinstance(market, Exception):
                market = None
            self.process_position_tick(mint, market)

    def process_position_tick(self, mint: str, market: Optional[dict]) -> Optional[str]:
        conn = self._conn()
        c = conn.cursor()
        c.execute("""
            SELECT symbol, entry_price_usd, current_price_usd, highest_price_usd, invested_sol, remaining_ratio,
                   tp1_hit, realized_sol, opened_at, liquidity_usd, whale_wallet
            FROM positions WHERE mint = ? AND status = 'OPEN'
        """, (mint,))
        row = c.fetchone()
        conn.close()
        if not row:
            return None
        symbol, entry, last_px, highest, invested, rem, tp1_hit, realized, opened_at, stored_liq, whale_wallet = row

        fresh, why = self.is_market_fresh(market)
        flow = None
        if fresh:
            current = float(market["price_usd"])
            liq = _safe_float(market.get("liquidity_usd")) or stored_liq
            if market.get("flow_ok"):
                flow = {"buys": int(market.get("buys_m5") or 0), "sells": int(market.get("sells_m5") or 0)}
            self._stale_logged.discard(mint)
        else:
            current = last_px or entry
            liq = stored_liq
            if mint not in self._stale_logged:
                self._stale_logged.add(mint)
                add_agent_log("RISK MGR", f"⏸️ ${symbol}: stale price ({why}) — peak/trail frozen.", "warn")

        whale_evt = None
        ev = WHALE_SELL_EVENTS.get(mint)
        opened_dt = self._parse_ts(opened_at)
        if ev and opened_dt:
            ev_ts = float(ev.get("ts") or 0)
            ev_age = self.now_ts() - ev_ts
            # Only the whale this position followed counts, only after entry,
            # and only while the signal is still recent.
            same_whale = bool(whale_wallet) and str(ev.get("wallet") or "") == str(whale_wallet)
            if same_whale and ev_ts > opened_dt.replace(tzinfo=timezone.utc).timestamp() \
                    and 0 <= ev_age <= self.whale_event_ttl_sec:
                whale_evt = ev

        return self._apply_price_update_and_rules(
            mint, symbol, entry, current, highest, invested, rem, tp1_hit, realized,
            opened_at=opened_at, fresh=fresh, flow=flow, liquidity_usd=liq, whale_selling=whale_evt,
        )

    def simulate_pump_all_open(self, multiplier: float = 2.15):
        conn = self._conn()
        c = conn.cursor()
        c.execute("""
            SELECT mint, symbol, entry_price_usd, highest_price_usd, invested_sol, remaining_ratio, tp1_hit, realized_sol, opened_at
            FROM positions WHERE status = 'OPEN'
        """)
        open_positions = c.fetchall()
        conn.close()

        for pos in open_positions:
            mint, symbol, entry_price, highest_price, invested_sol, rem_ratio, tp1_hit, realized_sol, opened_at = pos
            pumped_price = entry_price * multiplier
            self.journal("EXIT", mint=mint, symbol=symbol, rule="SIMULATED_PUMP",
                         reasons=f"Dashboard test tick ×{multiplier:g} (not a market price)", mid_price=pumped_price)
            self._apply_price_update_and_rules(
                mint, symbol, entry_price, pumped_price, max(highest_price, pumped_price),
                invested_sol, rem_ratio, tp1_hit, realized_sol, opened_at=opened_at, fresh=True,
            )

    def _close_full_position(
        self, c, mint, symbol, current_price, highest_price, invested_sol, rem_ratio,
        realized_sol, price_multiplier, current_value_sol, now, exit_reason, pnl_pct, extra_note="",
        liquidity_usd=None,
    ):
        c.execute("""
            SELECT opened_at, signal_price_usd, entry_price_usd, slippage_sol, size_reason, conviction,
                   whale_wallet, whale_label, tp1_hit, liquidity_usd, source
            FROM positions WHERE mint = ?
        """, (mint,))
        meta = c.fetchone() or (None,) * 11
        opened_at, sig_px, entry_fill, prev_slip_sol, size_reason, conviction, whale_wallet, whale_label, tp1_flag, stored_liq, src_val = meta
        liq = liquidity_usd if liquidity_usd is not None else stored_liq
        # Every SELL leg pays exit slippage, the AMM fee and flat costs, priced
        # against the depth the entry already consumed.
        cost_fill = self._cost_model("SELL").exit_value(
            current_value_sol, current_price, liq, self._depth_used(invested_sol, liq)
        )
        exit_fill = cost_fill.price
        slip = cost_fill.total_cost_fraction
        proceeds = cost_fill.net_sol
        slip_sol_total = (prev_slip_sol or 0.0) + cost_fill.total_cost_sol

        total_returned = realized_sol + proceeds
        net_pnl_sol = total_returned - invested_sol
        is_win = 1 if net_pnl_sol > 0 else 0
        c.execute("""
            UPDATE positions
            SET current_price_usd = ?, highest_price_usd = ?, remaining_ratio = 0.0,
                realized_sol = ?, status = 'CLOSED', closed_at = ?, exit_reason = ?,
                exit_fill_usd = ?, slippage_sol = ?
            WHERE mint = ?
        """, (current_price, highest_price, total_returned, now, exit_reason, exit_fill, slip_sol_total, mint))
        c.execute("""
            UPDATE wallet
            SET balance_sol = balance_sol + ?, realized_pnl_sol = realized_pnl_sol + ?,
                total_trades = total_trades + 1, winning_trades = winning_trades + ?
            WHERE id = 1
        """, (proceeds, net_pnl_sol, is_win))
        c.execute("""
            INSERT OR REPLACE INTO trade_history
            (mint, symbol, opened_at, closed_at, invested_sol, returned_sol, net_pnl_sol, exit_reason, tp1_hit,
             signal_price_usd, entry_fill_usd, exit_fill_usd, slippage_sol, size_reason, conviction,
             whale_wallet, whale_label, source, backfilled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            mint, symbol, opened_at, now, invested_sol, total_returned, net_pnl_sol, exit_reason, int(tp1_flag or 0),
            sig_px, entry_fill, exit_fill, slip_sol_total, size_reason, conviction, whale_wallet, whale_label, src_val,
        ))
        if whale_wallet:
            self._update_wallet_quality(c, whale_wallet, whale_label, net_pnl_sol, mint, symbol)
        self.journal(
            "EXIT", mint=mint, symbol=symbol, rule=exit_reason,
            reasons=[f"{pnl_pct:+.1f}% vs entry fill", extra_note] if extra_note else [f"{pnl_pct:+.1f}% vs entry fill"],
            actual_size=invested_sol, signal_price=sig_px, fill_price=exit_fill, mid_price=current_price,
            extra={"netPnlSol": round(net_pnl_sol, 6), "returnedSol": round(total_returned, 6),
                   "exitSlippageBps": round(slip * 10000, 2), "slippageSolTotal": round(slip_sol_total, 6)},
            cur=c,
        )
        icon = exit_icon(exit_reason)
        msg = (
            f"{icon} [{exit_reason}] Zatvorena pozicija ${symbol} na {pnl_pct:+.1f}%. "
            f"Neto PnL: {net_pnl_sol:+.3f} SOL (exit fill {slip * 10000:.0f} bps ispod mid)."
            f"{(' ' + extra_note) if extra_note else ''}"
        )
        print(f"   {msg}")
        add_agent_log("RISK MGR", msg, "danger" if net_pnl_sol < 0 else "warn")
        TELEGRAM.enqueue(
            f"{icon} <b>ZATVORENA POZICIJA: ${symbol}</b>\n"
            f"Razlog: <code>{exit_reason}</code> ({pnl_pct:+.1f}%)\n"
            f"Neto PnL: <b>{net_pnl_sol:+.3f} SOL</b>"
        )

    def _apply_price_update_and_rules(
        self, mint, symbol, entry_price, current_price, highest_price, invested_sol, rem_ratio, tp1_hit, realized_sol,
        opened_at=None, fresh: bool = True, flow: Optional[dict] = None, liquidity_usd: Optional[float] = None,
        whale_selling: Optional[dict] = None,
    ) -> Optional[str]:
        if not entry_price or entry_price <= 0 or not current_price or current_price <= 0:
            return None
        tp1_hit = bool(tp1_hit)
        stored_highest = highest_price or entry_price
        highest_price = max(stored_highest, current_price) if fresh else stored_highest
        pnl_pct = ((current_price - entry_price) / entry_price) * 100.0
        peak_pnl_pct = ((highest_price - entry_price) / entry_price) * 100.0
        drawdown_from_peak_pct = ((highest_price - current_price) / highest_price) * 100.0 if highest_price > 0 else 0.0
        price_multiplier = current_price / entry_price
        current_value_sol = (invested_sol * rem_ratio) * price_multiplier
        opened_dt = self._parse_ts(opened_at)
        age_sec = (self.now() - opened_dt).total_seconds() if opened_dt else 10**9
        eff_stop, trail_stop, be_stop = self._effective_stop(entry_price, highest_price, tp1_hit)

        conn = self._conn()
        c = conn.cursor()
        now = fmt_utc(self.now())
        action = None

        def close(reason, note=""):
            self._close_full_position(
                c, mint, symbol, current_price, highest_price, invested_sol, rem_ratio,
                realized_sol, price_multiplier, current_value_sol, now, reason, pnl_pct,
                extra_note=note, liquidity_usd=liquidity_usd,
            )
            return reason

        policy = self.exit_policy()
        intent = policy.classify_tick(
            entry_price=entry_price,
            current_price=current_price,
            highest_price=highest_price,
            tp1_hit=tp1_hit,
            age_sec=age_sec,
            fresh=fresh,
            flow=flow,
            whale_selling=whale_selling,
        )
        reason = intent.reason

        if reason == WHALE_EXIT:
            action = close("WHALE_EXIT", f"Tracked whale '{whale_selling.get('label', '?')}' is selling this token.")

        elif reason == TAKE_PROFIT_1:
            sell_value_sol = current_value_sol * intent.fraction
            tp_cost = self._cost_model("SELL").exit_value(
                sell_value_sol, current_price, liquidity_usd, self._depth_used(invested_sol, liquidity_usd)
            )
            tp_fill = tp_cost.price
            slip = tp_cost.total_cost_fraction
            proceeds = tp_cost.net_sol
            new_rem_ratio = rem_ratio * (1.0 - intent.fraction)
            new_realized = realized_sol + proceeds
            new_stop, _, new_be = policy.effective_stop(entry_price, highest_price, True)

            c.execute("""
                UPDATE positions
                SET current_price_usd = ?, highest_price_usd = ?, remaining_ratio = ?, tp1_hit = 1, realized_sol = ?,
                    slippage_sol = COALESCE(slippage_sol, 0) + ?, last_price_at = ?, liquidity_usd = COALESCE(?, liquidity_usd),
                    stop_price_usd = ?
                WHERE mint = ?
            """, (current_price, highest_price, new_rem_ratio, new_realized, tp_cost.total_cost_sol, now,
                  liquidity_usd, new_stop, mint))

            c.execute("UPDATE wallet SET balance_sol = balance_sol + ? WHERE id = 1", (proceeds,))
            self.journal(
                "EXIT", mint=mint, symbol=symbol, rule="TAKE_PROFIT_1",
                reasons=[f"{pnl_pct:+.1f}% ≥ TP1 {self.tp1_pct:g}% → sold {intent.fraction * 100:g}%"],
                fill_price=tp_fill, mid_price=current_price,
                extra={"proceedsSol": round(proceeds, 6), "slippageBps": round(slip * 10000, 2)}, cur=c,
            )
            msg = (f"🎯 [TAKE-PROFIT 1] ${symbol} skočio {pnl_pct:+.1f}%! Prodano {intent.fraction * 100:g}% za "
                   f"{proceeds:.3f} SOL (Moonbag aktivan, stop ≥ breakeven).")
            print(f"   {msg}")
            add_agent_log("RISK MGR", msg, "success")
            TELEGRAM.enqueue(f"🎯 <b>TP1 OSTVAREN: ${symbol}</b> (+{pnl_pct:.1f}%)\nProdano {intent.fraction * 100:g}% za "
                             f"<b>{proceeds:.3f} SOL</b>. Moonbag aktivan!")
            action = "TAKE_PROFIT_1"

        elif reason == FAST_INVALIDATION:
            action = close("FAST_INVALIDATION", f"Fast invalidation inside the first {self.fast_invalidation_window_sec}s.")

        elif reason == STOP_LOSS:
            action = close("STOP_LOSS")

        elif reason == TIME_STOP:
            action = close("TIME_STOP", f"No pump after {int(age_sec)}s (peak {peak_pnl_pct:+.1f}% < {self.time_stop_min_profit_pct:g}%).")

        elif reason == FLOW_EXIT:
            action = close("FLOW_EXIT", f"Sell flow: {flow.get('sells')} sells vs {flow.get('buys')} buys (5m).")

        elif reason == TAKE_PROFIT:
            action = close("TAKE_PROFIT", f"Final target {self.final_take_profit_pct:g}% reached.")

        elif reason == TRAILING_STOP:
            trail_used = self.trailing_stop_pct_after_tp1 if tp1_hit else self.trailing_stop_pct
            action = close("TRAILING_STOP", f"{drawdown_from_peak_pct:.1f}% off peak (trail {trail_used:.1f}%).")

        elif reason == BREAKEVEN_STOP:
            action = close("BREAKEVEN_STOP", f"Moonbag/Step-up protected above entry (${eff_stop:.8f}).")

        elif reason == STEP_UP_STOP:
            action = close("STEP_UP_STOP", f"Step-up risk floor triggered (${eff_stop:.8f}).")

        else:
            if fresh:
                c.execute("""
                    UPDATE positions SET current_price_usd = ?, highest_price_usd = ?, last_price_at = ?,
                           liquidity_usd = COALESCE(?, liquidity_usd), stop_price_usd = ?
                    WHERE mint = ?
                """, (current_price, highest_price, now, liquidity_usd, eff_stop, mint))
            else:
                c.execute("UPDATE positions SET stop_price_usd = ? WHERE mint = ?", (eff_stop, mint))

        conn.commit()
        conn.close()
        return action

    def get_equity_series(self) -> dict:
        conn = self._conn()
        c = conn.cursor()
        c.execute("""
            SELECT symbol, opened_at, closed_at, net_pnl_sol, exit_reason, invested_sol, returned_sol
            FROM trade_history ORDER BY closed_at ASC, id ASC
        """)
        rows = c.fetchall()
        c.execute("SELECT realized_pnl_sol FROM wallet WHERE id = 1")
        wallet_realized = float(c.fetchone()[0])
        conn.close()

        points = []
        equity = float(self.initial_sol)
        if rows:
            first_open = self._parse_ts(rows[0][1]) or self._parse_ts(rows[0][2])
            points.append({"t": fmt_utc(first_open), "tLocal": fmt_local(first_open), "equity": round(equity, 6),
                           "label": "START", "kind": "start"})
        for sym, opened_at, closed_at, net, reason, inv, ret in rows:
            equity += float(net or 0.0)
            dt = self._parse_ts(closed_at)
            points.append({"t": closed_at, "tLocal": fmt_local(dt) if dt else closed_at, "equity": round(equity, 6),
                           "label": f"{sym} {reason}", "kind": "close", "pnl": round(float(net or 0.0), 6),
                           "icon": exit_icon(reason)})
        history_realized = equity - float(self.initial_sol)
        adjustment = wallet_realized - history_realized
        if abs(adjustment) > 1e-6:
            equity += adjustment
            points.append({"t": fmt_utc(self.now()), "tLocal": fmt_local(self.now()), "equity": round(equity, 6),
                           "label": "Unattributed (pre-v8 overwritten rows)", "kind": "adjust", "pnl": round(adjustment, 6)})
        now_eq = self.get_equity_now()
        points.append({"t": fmt_utc(self.now()), "tLocal": fmt_local(self.now()), "equity": round(now_eq, 6),
                       "label": "NOW (mark-to-market)", "kind": "now"})
        return {
            "initialSol": self.initial_sol,
            "equityNowSol": now_eq,
            "realizedPnlSol": wallet_realized,
            "points": points,
        }

    def get_risk_snapshot(self) -> dict:
        breaker = self.get_circuit_breaker_state()
        gate_ok, gate_rule, gate_reason = self.check_entry_gates(breaker=breaker)
        size_conv = {}
        for conv in ("high", "ai", "heuristic", "manual"):
            s, r, _ = self.compute_position_size(conv, breaker=breaker)
            size_conv[conv] = {"sizeSol": s, "reasons": r}
        return {
            "circuitBreaker": breaker,
            "entryGate": {"ok": gate_ok, "rule": gate_rule, "reason": gate_reason},
            "pendingConfirmations": self.get_pending_confirmations(),
            "sizing": {
                "nextSizeSol": size_conv["high"]["sizeSol"],
                "nextSizeReasons": size_conv["high"]["reasons"],
                "byConviction": size_conv,
                "lastSize": self.last_size_info,
            },
            "lastEntryBlock": self.last_entry_block,
            "walletQuality": self.list_wallet_quality(),
            "sourceAnalytics": self.get_source_analytics(),
        }

    def get_strategy_profile_info(self) -> dict:
        from bettercalltrader.strategies import profile_values
        return {
            "name": self.strategy_profile,
            "riskMode": "high" if self.high_risk_mode else "standard",
            "overridesEnvironment": self.high_risk_mode,
            "values": profile_values(self.strategy_profile),
            "effective": {
                "tradeSizeSol": self.trade_size_sol,
                "maxOpenPositions": self.max_open_positions,
                "stopLossPct": self.stop_loss_pct,
                "tp1Pct": self.tp1_pct,
                "trailingStopPct": self.trailing_stop_pct,
                "maxDailyStopLosses": self.max_daily_stop_losses,
                "maxDailyDrawdownPct": self.max_daily_drawdown_pct,
                "maxDailyTrades": self.max_daily_trades,
            },
        }

    def get_portfolio_risk(self) -> dict:
        from bettercalltrader.risk_analytics import herfindahl_index, summarize

        conn = self._conn()
        c = conn.cursor()
        c.execute("SELECT symbol, invested_sol, remaining_ratio, entry_price_usd, current_price_usd FROM positions WHERE status = 'OPEN'")
        rows = c.fetchall()
        by_symbol = {}
        total_open = 0.0
        for symbol, invested, rem, entry, cur in rows:
            value = float(invested or 0.0) * float(rem or 0.0)
            if entry and cur:
                value *= float(cur) / float(entry)
            key = symbol or "?"
            by_symbol[key] = by_symbol.get(key, 0.0) + value
            total_open += value
        c.execute("SELECT net_pnl_sol FROM trade_history ORDER BY closed_at ASC, id ASC")
        pnls = [r[0] or 0.0 for r in c.fetchall()]
        conn.close()

        metrics = summarize(pnls, float(self.initial_sol))
        concentration = {k: (v / total_open if total_open > 0 else 0.0) for k, v in by_symbol.items()}
        hhi = herfindahl_index(list(by_symbol.values()))
        top_symbol = max(concentration, key=concentration.get) if concentration else None
        top_pct = concentration.get(top_symbol, 0.0) if top_symbol else 0.0
        equity_now = self.get_equity_now()
        exposure_pct = (total_open / equity_now * 100.0) if equity_now > 0 else 0.0
        return {
            "openValueSol": total_open,
            "openExposurePct": exposure_pct,
            "concentrationBySymbol": concentration,
            "concentrationHhi": hhi,
            "topPosition": {"symbol": top_symbol, "weight": top_pct} if top_symbol else None,
            "maxHistoricalDrawdownPct": metrics["maxDrawdownPct"],
            "realizedTrades": metrics["trades"],
            "netRealizedPnlSol": metrics["netPnlSol"],
            "winRate": metrics["winRate"],
            "profitFactor": metrics["profitFactor"],
            "expectancySol": metrics["expectancySol"],
            "var95Sol": metrics["var95Sol"],
            "expectedShortfall95Sol": metrics["expectedShortfall95Sol"],
            "avgWinSol": metrics["avgWinSol"],
            "avgLossSol": metrics["avgLossSol"],
            "equityNowSol": equity_now,
        }

    def print_portfolio_summary(self):
        w = self.get_wallet_status()
        win_rate = (w["winning_trades"] / w["total_trades"] * 100) if w["total_trades"] > 0 else 0.0
        total_equity = w["balance_sol"] + w["locked_sol"]

        print("\n" + "-" * 70)
        print("💼 PAPER TRADING PORTFELJ (SQLite Stanje v12.0):")
        print(
            f"   💰 Slobodno: {w['balance_sol']:.3f} SOL | U pozicijama: {w['locked_sol']:.3f} SOL | "
            f"Ukupni kapital: {total_equity:.3f} SOL"
        )
        print(
            f"   📈 Realizirani PnL: {w['realized_pnl_sol']:+.3f} SOL | "
            f"Otvorene pozicije: {w['open_positions']} | "
            f"Završeni trejdovi: {w['total_trades']} (Win rate: {win_rate:.0f}%)"
        )
        print("-" * 70)


def _safe_float(v) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def determine_conviction(eval_mode: str, market: dict) -> str:
    has_whale = bool(market.get("whale_signal"))
    if eval_mode == "AI" and has_whale:
        return "high"
    if eval_mode == "AI":
        return "ai"
    return "heuristic"


def apply_whale_signal(market_data: dict, pf_event: Optional[dict], portfolio: PaperTradingManager, count_signal: bool = True) -> dict:
    if not (pf_event and pf_event.get("whale_label")):
        return market_data
    wallet = pf_event.get("whale_wallet")
    if wallet and count_signal:
        portfolio.increment_whale_signal(wallet)
    trusted, wq_reason = portfolio.is_wallet_trusted(wallet)
    if trusted:
        market_data["whale_signal"] = pf_event["whale_label"]
        market_data["whale_wallet"] = wallet
        market_data["whale_quality"] = wq_reason
    else:
        market_data["whale_signal"] = None
        market_data["whale_wallet"] = None
        market_data["whale_signal_info"] = f"{pf_event['whale_label']} — ignored for auto-BUY: {wq_reason}"
        portfolio.journal("WATCH", mint=market_data.get("mint"), symbol=market_data.get("symbol"), rule="WALLET_UNTRUSTED",
                          reasons=market_data["whale_signal_info"])
        add_agent_log("WHALE TRACKER", f"🐋 '{pf_event['whale_label']}' signal ignored for auto-BUY ({wq_reason}).", "warn")
    return market_data


async def evaluate_and_record_candidate(
    session: aiohttp.ClientSession,
    mint: str,
    source_tag: str,
    researcher: ResearchAgent,
    portfolio: PaperTradingManager,
    executor: SolanaExecutionAgent,
    pf_event: Optional[dict] = None
):
    set_runtime_phase("EVALUATING_TOKEN", mint)
    print("\n----------------------------------------------------------------------")
    print(f"🧪 [{source_tag}] Analiziram token: {mint}")

    market_data, security_data = await asyncio.gather(
        researcher.get_market_data(session, mint),
        researcher.get_security_data(session, mint)
    )

    if not market_data and pf_event:
        market_data = await researcher.build_pumpfun_fallback_market_data(session, pf_event)

    if not market_data:
        print("   ⏭️ Preskačem: Nema aktivnih parova/likvidnosti na DexScreeneru.")
        return None

    apply_whale_signal(market_data, pf_event, portfolio)

    top10_raw = security_data.get("top10_holder_pct")
    top10_txt = f"{float(top10_raw):.1f}%" if top10_raw is not None else "N/A"
    max1_raw = security_data.get("max_single_holder_pct")
    max1_txt = f"{float(max1_raw):.1f}%" if max1_raw is not None else "N/A"
    bundle_flag = "⚠️ BUNDLE DETECTED" if security_data.get("is_bundled") else "✅ Čista distribucija"
    whale_tag = f" | 🐋 WHALE: {market_data['whale_signal']}" if market_data.get("whale_signal") else ""

    print(f"   🪙 Token: {market_data['name']} (${market_data['symbol']}) | Izvor: {market_data['dex_id']}{whale_tag}")
    print(f"   💧 Likvidnost: ${market_data['liquidity_usd']:,.2f} | 📊 FDV: ${market_data['fdv_usd']:,.2f}")
    print(f"   👥 Top 10 Holdera: {top10_txt} (Max 1: {max1_txt}) | {bundle_flag}")
    print(f"   🔄 Transakcije (5m): {market_data['buys_m5']} Buys / {market_data['sells_m5']} Sells")

    passed, reason = researcher.pass_hard_filters(market_data, security_data)
    if not passed:
        print(f"   🛑 [HARD FILTER REJECT]: {reason}")
        reject_verdict = TokenAnalysisDecision(
            decision="SKIP",
            risk_score=18,
            hype_score=30,
            summary_hr=f"Automatski odbačen (Hard Filter): {reason}",
            red_flags=[reason],
            green_flags=[]
        )
        portfolio.save_scan_result(market_data, security_data, reject_verdict, source_tag)
        portfolio.journal("SKIP", mint=mint, symbol=market_data.get("symbol"), rule="HARD_FILTER", reasons=reason,
                          mid_price=_safe_float(market_data.get("price_usd")), risk_score=18, hype_score=30)
        add_agent_log("RESEARCH AI", f"🔴 [SKIP] ${market_data['symbol']} — {reason}", "danger")
        return reject_verdict

    print(f"   ✅ [HARD FILTER PASS]: {reason}. Šaljem AI Agentu ({researcher.model_name}) na duboku analizu...")
    verdict = await researcher.ai_evaluate_token(market_data, security_data)
    eval_mode = getattr(researcher, "last_eval_mode", "HEURISTIC")

    if market_data.get("whale_signal"):
        whale_note = f"🐋 Smart Money Copy-Signal: '{market_data['whale_signal']}' kupio ovaj token"
        if whale_note not in verdict.green_flags:
            verdict.green_flags.insert(0, whale_note)
        verdict.hype_score = min(99, verdict.hype_score + 15)

    portfolio.save_scan_result(market_data, security_data, verdict, source_tag)

    badge = "🟢 BUY" if verdict.decision == "BUY" else ("🟡 WATCH" if verdict.decision == "WATCH" else "🔴 SKIP")
    print(f"\n   🎯 ODLUKA AGENTA: {badge} ({eval_mode})")
    print(f"   🛡️ Risk Score (Sigurnost): {verdict.risk_score}/100 | 🚀 Hype Score: {verdict.hype_score}/100")
    print(f"   📝 Obrazloženje: {verdict.summary_hr}")

    log_lvl = "success" if verdict.decision == "BUY" else ("warn" if verdict.decision == "WATCH" else "danger")
    add_agent_log(
        "RESEARCH AI",
        f"{badge} ${market_data['symbol']} | Top10: {top10_txt} | Risk: {verdict.risk_score} | Hype: {verdict.hype_score}",
        log_lvl
    )
    portfolio.journal(
        verdict.decision if verdict.decision in ("BUY", "WATCH", "SKIP") else "WATCH",
        mint=mint, symbol=market_data.get("symbol"), rule=f"{eval_mode}_VERDICT",
        reasons=verdict.summary_hr[:400], mid_price=_safe_float(market_data.get("price_usd")),
        risk_score=verdict.risk_score, hype_score=verdict.hype_score,
        extra={"whale": market_data.get("whale_signal"), "source": source_tag},
    )

    if verdict.decision == "BUY":
        conviction = determine_conviction(eval_mode, market_data)
        result = portfolio.request_entry(market_data, security_data, verdict, conviction=conviction, source=source_tag)
        if result["status"] == "OPENED":
            await _dry_run_after_open(session, executor, portfolio, mint, market_data)

    return verdict


async def _dry_run_after_open(session, executor: SolanaExecutionAgent, portfolio: PaperTradingManager, mint: str, market: dict):
    size = None
    for p in portfolio.get_all_positions():
        if p["mint"] == mint and p["status"] == "OPEN":
            size = p["investedSol"]
            break
    exec_result = await executor.execute_buy(
        session=session,
        mint=mint,
        amount_sol=size or portfolio.trade_size_sol,
        dex_id=market.get("dex_id", "raydium")
    )
    print(f"   🛰️ [Execution Status]: {exec_result}")


async def process_due_confirmations(
    session: aiohttp.ClientSession,
    researcher: ResearchAgent,
    portfolio: PaperTradingManager,
    executor: SolanaExecutionAgent,
):
    for mint in portfolio.due_confirmations():
        fresh = await researcher.get_market_data(session, mint)
        res = portfolio.process_confirmation(mint, fresh, researcher)
        if res.get("status") == "OPENED":
            await _dry_run_after_open(session, executor, portfolio, mint, fresh or {})


# =====================================================================
# 5. LOKALNI BRIDGE SERVER ZA COMMAND CENTER (Port 8080)
# =====================================================================

CONFIG_FIELDS = [
    ("tradeSizeSol", "portfolio", "trade_size_sol", float),
    ("minLiquidityUsd", "researcher", "min_liquidity_usd", float),
    ("maxTop10Pct", "researcher", "max_top10_holder_pct", float),
    ("tp1Pct", "portfolio", "tp1_pct", float),
    ("stopLossPct", "portfolio", "stop_loss_pct", float),
    ("trailingStopPct", "portfolio", "trailing_stop_pct", float),
    ("trailingStopPctAfterTp1", "portfolio", "trailing_stop_pct_after_tp1", float),
    ("maxOpenPositions", "portfolio", "max_open_positions", int),
    ("minSecondsBetweenEntries", "portfolio", "min_seconds_between_entries", int),
    ("stopLossCooldownSec", "portfolio", "stop_loss_cooldown_sec", int),
    ("fastInvalidationPct", "portfolio", "fast_invalidation_pct", float),
    ("fastInvalidationWindowSec", "portfolio", "fast_invalidation_window_sec", int),
    ("positionMonitorSec", "portfolio", "position_monitor_sec", float),
    ("rejectM5DumpPct", "researcher", "reject_m5_dump_pct", float),
    ("maxChaseH1Pct", "researcher", "max_chase_h1_pct", float),
    ("flowExitEnabled", "portfolio", "flow_exit_enabled", bool),
    ("flowExitMinSells", "portfolio", "flow_exit_min_sells", int),
    ("flowExitSellRatio", "portfolio", "flow_exit_sell_ratio", float),
    ("whaleExitEnabled", "portfolio", "whale_exit_enabled", bool),
    ("maxDailyStopLosses", "portfolio", "max_daily_stop_losses", int),
    ("maxDailyDrawdownPct", "portfolio", "max_daily_drawdown_pct", float),
    ("maxDailyTrades", "portfolio", "max_daily_trades", int),
    ("entryConfirmSec", "portfolio", "entry_confirm_sec", float),
    ("entryConfirmMaxDropPct", "portfolio", "entry_confirm_max_drop_pct", float),
    ("paperSlippageBps", "portfolio", "paper_slippage_bps", float),
    ("paperExitSlippageBps", "portfolio", "paper_exit_slippage_bps", float),
    ("paperImpactCoeff", "portfolio", "paper_impact_coeff", float),
    ("paperImpactExponent", "portfolio", "paper_impact_exponent", float),
    ("paperAmmFeeBps", "portfolio", "paper_amm_fee_bps", float),
    ("paperPriorityFeeSol", "portfolio", "paper_priority_fee_sol", float),
    ("paperFailedTxProb", "portfolio", "paper_failed_tx_prob", float),
    ("sizeAfterLossMult", "portfolio", "size_after_loss_mult", float),
    ("sizeHeuristicMult", "portfolio", "size_heuristic_mult", float),
    ("sizeAiOnlyMult", "portfolio", "size_ai_only_mult", float),
    ("sizeDrawdownMult", "portfolio", "size_drawdown_mult", float),
    ("sizeDrawdownTriggerPct", "portfolio", "size_drawdown_trigger_pct", float),
    ("minPositionSol", "portfolio", "min_position_sol", float),
    ("maxPositionSol", "portfolio", "max_position_sol", float),
    ("clusterCooldownSec", "portfolio", "cluster_cooldown_sec", int),
    ("clusterOpenDdPct", "portfolio", "cluster_open_dd_pct", float),
    ("clusterUnprovenSec", "portfolio", "cluster_unproven_sec", int),
    ("walletQualityWindow", "portfolio", "wallet_quality_window", int),
    ("walletQualityMinSamples", "portfolio", "wallet_quality_min_samples", int),
    ("walletQualityMinWinRate", "portfolio", "wallet_quality_min_win_rate", float),
    ("stalePriceSec", "portfolio", "stale_price_sec", float),
    ("moveStopToBreakevenAfterTp1", "portfolio", "move_stop_to_breakeven_after_tp1", bool),
    ("breakevenBufferPct", "portfolio", "breakeven_buffer_pct", float),
    ("tp1Fraction", "portfolio", "tp1_fraction", float),
    ("finalTakeProfitPct", "portfolio", "final_take_profit_pct", float),
    ("trailingArmGainPct", "portfolio", "trailing_arm_gain_pct", float),
    # v9.0
    ("timeStopEnabled", "portfolio", "time_stop_enabled", bool),
    ("maxHoldNoPumpSec", "portfolio", "max_hold_no_pump_sec", int),
    ("timeStopMinProfitPct", "portfolio", "time_stop_min_profit_pct", float),
    ("step1TriggerPct", "portfolio", "step1_trigger_pct", float),
    ("step1FloorPct", "portfolio", "step1_floor_pct", float),
    ("step2TriggerPct", "portfolio", "step2_trigger_pct", float),
    ("step2FloorPct", "portfolio", "step2_floor_pct", float),
]


def build_config_payload(portfolio, researcher, executor) -> dict:
    owners = {"portfolio": portfolio, "researcher": researcher, "executor": executor}
    cfg = {key: getattr(owners[owner], attr) for key, owner, attr, _ in CONFIG_FIELDS}
    cfg["slippagePct"] = executor.slippage_bps / 100.0
    cfg["jitoTipSol"] = executor.priority_fee_sol
    cfg["strategyProfile"] = portfolio.strategy_profile
    cfg["riskMode"] = "high" if portfolio.high_risk_mode else "standard"
    cfg["liveTradingEnabled"] = executor.live_trading_enabled
    cfg["telegramEnabled"] = TELEGRAM.enabled
    cfg["timezone"] = BOT_TIMEZONE_NAME
    cfg["version"] = "v12.0"
    cfg["configHash"] = portfolio.config_hash
    cfg["costModel"] = portfolio._cost_model("BUY").as_dict()
    cfg["exitCostModel"] = portfolio._cost_model("SELL").as_dict()
    cfg["exitPolicy"] = portfolio.exit_policy().as_dict()
    cfg["llm"] = researcher.llm.describe()
    return cfg


def apply_config_payload(data: dict, portfolio, researcher, executor) -> List[str]:
    owners = {"portfolio": portfolio, "researcher": researcher, "executor": executor}
    changed = []
    for key, owner, attr, typ in CONFIG_FIELDS:
        if key not in data or data[key] is None or data[key] == "":
            continue
        raw = data[key]
        try:
            if typ is bool:
                val = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
            elif typ is int:
                val = int(float(raw))
            else:
                val = float(raw)
        except (TypeError, ValueError):
            continue
        if getattr(owners[owner], attr) != val:
            setattr(owners[owner], attr, val)
            changed.append(key)
            if key == "maxPositionSol":
                portfolio._max_position_explicit = True
    if "slippagePct" in data:
        try:
            executor.slippage_bps = int(float(data["slippagePct"]) * 100)
        except (TypeError, ValueError):
            pass
    if "jitoTipSol" in data:
        try:
            executor.priority_fee_sol = float(data["jitoTipSol"])
        except (TypeError, ValueError):
            pass
    # Applied last so a profile switch wins over the numeric fields posted in
    # the same request (the dashboard sends the whole form on every change).
    requested_profile = data.get("strategyProfile")
    if isinstance(requested_profile, str) and requested_profile.strip():
        from bettercalltrader.strategies import normalize_profile

        target = normalize_profile(requested_profile)
        if target != portfolio.strategy_profile:
            portfolio.apply_strategy_profile(target, force=True)
            changed.append("strategyProfile")
    if changed:
        portfolio.refresh_config_hash()
    return changed


def _is_protected_path(path: str) -> bool:
    return path.startswith("/api/") or path in ("/metrics", "/ready")


def dashboard_bind_error(host: str, token: str, allow_insecure: bool) -> Optional[str]:
    """Return a message when the dashboard would be exposed without auth."""
    if host in LOOPBACK_BIND_HOSTS or token or allow_insecure:
        return None
    return (
        f"refusing to start: COMMAND_CENTER_HOST={host!r} exposes the trading API on the network "
        "while DASHBOARD_TOKEN is empty. Set DASHBOARD_TOKEN, bind 127.0.0.1, or set "
        "ALLOW_INSECURE_DASHBOARD=true only if you really mean it."
    )


@web.middleware
async def metrics_middleware(request: web.Request, handler):
    """Record request counts and latency before any auth decision is made."""
    started = time.perf_counter()
    status = 500
    try:
        response = await handler(request)
        status = response.status
        return response
    finally:
        elapsed = time.perf_counter() - started
        path = request.path
        HTTP_METRICS.inc(
            "bettercalltrader_http_requests_total",
            {"method": request.method, "path": path, "status": str(status)},
        )
        HTTP_METRICS.observe(
            "bettercalltrader_http_request_duration_seconds",
            elapsed,
            {"method": request.method, "path": path},
        )


@web.middleware
async def cors_middleware(request: web.Request, handler):
    origin = request.headers.get("Origin")
    allowed = CORS_ALLOW_ALL or is_origin_allowed(origin, ALLOWED_ORIGINS)

    if request.method == "OPTIONS":
        if origin and not allowed:
            return web.Response(status=403, text="origin not allowed")
        resp = web.Response(status=204)
    else:
        resp = await handler(request)

    if origin and allowed:
        resp.headers["Access-Control-Allow-Origin"] = "*" if CORS_ALLOW_ALL else origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Auth-Token"
        resp.headers["Access-Control-Max-Age"] = "600"

    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if resp.content_type == "text/html":
        resp.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    return resp


@web.middleware
async def security_middleware(request: web.Request, handler):
    """Rate limit, optional token auth, and CSRF protection for mutating routes."""
    if request.method == "OPTIONS" or not _is_protected_path(request.path):
        return await handler(request)

    # Rate limit first: otherwise an unauthenticated caller can brute-force the
    # token without ever being throttled.
    ip = client_ip(dict(request.headers), request.remote, trust_forwarded=TRUST_PROXY_HEADERS)
    allowed, retry_after = API_RATE_LIMITER.allow(ip)
    if not allowed:
        response = web.json_response(
            {"ok": False, "error": "rate_limited", "retryAfterSec": round(retry_after, 1)},
            status=429,
        )
        response.headers["Retry-After"] = str(int(retry_after) + 1)
        return response

    token = request.headers.get("X-Auth-Token") or request.query.get("token")
    if not is_authorized(token, DASHBOARD_TOKEN):
        return web.json_response({"ok": False, "error": "unauthorized", "hint": "Send X-Auth-Token or ?token="}, status=401)

    # Defense in depth: reject cross-site state changes even when auth is off.
    if request.method in MUTATING_METHODS and not is_mutation_allowed(
        dict(request.headers), ALLOWED_ORIGINS, CORS_ALLOW_ALL
    ):
        return web.json_response(
            {"ok": False, "error": "cross_origin_blocked", "hint": "Send X-Auth-Token from a local client."},
            status=403,
        )

    return await handler(request)


async def start_command_center_server(
    portfolio: PaperTradingManager,
    researcher: ResearchAgent,
    discovery: DiscoveryAgent,
    executor: SolanaExecutionAgent,
    session: aiohttp.ClientSession,
    port: int = 8080
):
    # SystemExit is not caught by the caller's ``except Exception``, so an
    # insecure bind stops the process instead of silently degrading to
    # terminal-only trading with the API still unprotected.
    bind_error = dashboard_bind_error(
        COMMAND_CENTER_HOST, DASHBOARD_TOKEN, env_bool("ALLOW_INSECURE_DASHBOARD", False)
    )
    if bind_error:
        print(f"🛑 [SECURITY] {bind_error}")
        raise SystemExit(bind_error)

    app = web.Application(
        middlewares=[metrics_middleware, cors_middleware, security_middleware]
    )

    def page_response(path: Path) -> web.FileResponse:
        """Serve an HTML file with an explicit content type.

        ``FileResponse`` only guesses ``Content-Type`` during ``prepare()``,
        which happens after the middleware chain — so without pinning it here
        the CSP header would never be attached to a page.
        """
        return web.FileResponse(path, headers={"Content-Type": "text/html; charset=utf-8"})

    async def handle_index(request: web.Request):
        html_path = HTML_DIR / "command_center.html"
        if not html_path.exists():
            html_path = Path.cwd() / "html" / "command_center.html"
        if html_path.exists():
            return page_response(html_path)
        return web.Response(text="Datoteka html/command_center.html nije pronađena!", status=404)

    def make_page_handler(filename: str):
        """Serve a console page from html/ (or ./html under the cwd)."""
        async def _handle_page(request: web.Request):
            html_path = HTML_DIR / filename
            if not html_path.exists():
                html_path = Path.cwd() / "html" / filename
            if html_path.exists():
                return page_response(html_path)
            return web.Response(text=f"html/{filename} not found", status=404)
        return _handle_page

    async def handle_api_state(request: web.Request):
        risk = portfolio.get_risk_snapshot()
        return web.json_response({
            "status": "ok",
            "version": "v12.0",
            "security": {
                "authEnabled": bool(DASHBOARD_TOKEN),
                "rateLimitPerMin": API_RATE_LIMIT_PER_MIN,
            },
            "runtime": RUNTIME_STATUS,
            "ai_active": bool(researcher.client),
            "ai_model": researcher.model_name,
            "telegram_active": TELEGRAM.enabled,
            "live_trading_enabled": executor.live_trading_enabled,
            "wallet_address": executor.get_public_key_str(),
            "wallet": portfolio.get_wallet_status(),
            "positions": portfolio.get_all_positions(),
            "recent_scans": portfolio.get_recent_scans(limit=40),
            "tracked_wallets": portfolio.get_tracked_wallets(),
            "logs": LIVE_AGENT_LOGS[:80],
            "circuitBreaker": risk["circuitBreaker"],
            "entryGate": risk["entryGate"],
            "pendingConfirmations": risk["pendingConfirmations"],
            "sizing": risk["sizing"],
            "lastEntryBlock": risk["lastEntryBlock"],
            "walletQuality": risk["walletQuality"],
            "sourceAnalytics": risk["sourceAnalytics"],
            "strategyProfile": portfolio.get_strategy_profile_info(),
            "priceCache": PRICE_CACHE.stats(),
            "serverTimeUtc": fmt_utc(portfolio.now()),
            "serverTimeMs": int(portfolio.now_ts() * 1000),
            "config": build_config_payload(portfolio, researcher, executor),
        })

    async def handle_api_journal(request: web.Request):
        try:
            limit = max(1, min(500, int(request.query.get("limit", "100"))))
        except ValueError:
            limit = 100
        return web.json_response({"ok": True, "journal": portfolio.get_journal(limit)})

    async def handle_api_equity(request: web.Request):
        return web.json_response({"ok": True, **portfolio.get_equity_series()})

    async def handle_api_metrics(request: web.Request):
        try:
            from bettercalltrader.metrics import collect_basic_metrics
            metrics = collect_basic_metrics(portfolio.db_path)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        return web.json_response({"ok": True, "metrics": metrics, "schemaVersion": "10.0.0"})

    async def handle_api_portfolio_risk(request: web.Request):
        return web.json_response({"ok": True, "portfolioRisk": portfolio.get_portfolio_risk()})

    async def handle_ready(request: web.Request):
        checks = {"database": False, "ai": bool(researcher.client), "telegram": TELEGRAM.enabled}
        try:
            conn = portfolio._conn()
            try:
                conn.execute("SELECT 1").fetchone()
                checks["database"] = True
            finally:
                conn.close()
        except Exception as e:
            checks["dbError"] = str(e)
        checks["liveTradingEnabled"] = bool(executor.live_trading_enabled)
        checks["authEnabled"] = bool(DASHBOARD_TOKEN)
        ok = checks["database"]
        return web.json_response({"ok": ok, "checks": checks}, status=200 if ok else 503)

    async def handle_metrics(request: web.Request):
        try:
            from bettercalltrader.metrics import collect_basic_metrics
            db_metrics = collect_basic_metrics(portfolio.db_path)
            HTTP_METRICS.set("bettercalltrader_db_up", 1)
        except Exception:
            db_metrics = {}
            HTTP_METRICS.set("bettercalltrader_db_up", 0)

        HTTP_METRICS.set("bettercalltrader_uptime_seconds", time.monotonic() - START_MONOTONIC)
        HTTP_METRICS.set("bettercalltrader_trades_total", db_metrics.get("totalTrades", 0))
        HTTP_METRICS.set("bettercalltrader_win_rate", db_metrics.get("winRate", 0))
        HTTP_METRICS.set("bettercalltrader_net_pnl_sol", db_metrics.get("netPnlSol", 0))
        HTTP_METRICS.set("bettercalltrader_open_positions", db_metrics.get("openPositions", 0))
        try:
            wallet = portfolio.get_wallet_status()
            HTTP_METRICS.set("bettercalltrader_wallet_balance_sol", wallet.get("balance_sol", 0))
        except Exception:
            HTTP_METRICS.set("bettercalltrader_wallet_balance_sol", 0)
        try:
            breaker = portfolio.get_circuit_breaker_state()
            HTTP_METRICS.set("bettercalltrader_circuit_breaker_active", 1 if breaker.get("tripped") else 0)
        except Exception:
            HTTP_METRICS.set("bettercalltrader_circuit_breaker_active", 0)
        HTTP_METRICS.set(
            "bettercalltrader_info",
            1,
            {
                "version": "12.0",
                "mode": "LIVE" if executor.live_trading_enabled else "PAPER",
            },
        )
        return web.Response(
            text=HTTP_METRICS.render(),
            headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
        )

    async def handle_api_audit(request: web.Request):
        try:
            limit = max(1, min(500, int(request.query.get("limit", "100"))))
        except ValueError:
            limit = 100
        return web.json_response({"ok": True, "events": read_audit(AUDIT_LOG_PATH, limit)})

    async def handle_api_ml_status(request: web.Request):
        from bettercalltrader.ml import model_status
        return web.json_response({"ok": True, "model": model_status(ML_MODEL_PATH)})

    async def handle_api_ml_predict(request: web.Request):
        from bettercalltrader.ml import load_model, model_status, predict_mint
        mint = (request.query.get("mint") or "").strip()
        if not mint:
            return web.json_response({"ok": False, "error": "mint query parameter required"}, status=400)
        model = load_model(ML_MODEL_PATH)
        if not model:
            status = model_status(ML_MODEL_PATH)
            return web.json_response(
                {
                    "ok": False,
                    "error": "model is missing or stale; retrain it",
                    "state": status["state"],
                    "reasons": status["reasons"],
                },
                status=409,
            )
        probability = predict_mint(portfolio.db_path, mint, model)
        if probability is None:
            return web.json_response({"ok": False, "error": "no scored journal entry for mint"}, status=404)
        return web.json_response({
            "ok": True,
            "mint": mint,
            "probability": probability,
            "advisory": "BUY_BIAS" if probability >= 0.6 else ("SKIP_BIAS" if probability <= 0.4 else "NEUTRAL"),
        })

    async def handle_api_ml_train(request: web.Request):
        from bettercalltrader.ml import model_status, save_model, train_model
        model = train_model(portfolio.db_path)
        if not model.get("ok"):
            return web.json_response({"ok": False, "reason": model.get("reason")}, status=409)
        save_model(model, ML_MODEL_PATH)
        return web.json_response({"ok": True, "model": model_status(ML_MODEL_PATH)})

    async def handle_api_backtest(request: web.Request):
        from bettercalltrader.backtest import build_report
        try:
            report = build_report(portfolio.db_path, initial_sol=portfolio.initial_sol)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        report["trades"] = report["trades"][-100:]
        report["equityCurve"] = report["equityCurve"][-500:]
        return web.json_response({"ok": True, "report": report})

    async def handle_analytics(request: web.Request):
        html_path = HTML_DIR / "analytics.html"
        if not html_path.exists():
            html_path = Path.cwd() / "html" / "analytics.html"
        if html_path.exists():
            return page_response(html_path)
        return web.Response(text="html/analytics.html not found", status=404)

    async def handle_api_wallet_quality(request: web.Request):
        return web.json_response({"ok": True, "walletQuality": portfolio.list_wallet_quality()})

    async def handle_api_breaker(request: web.Request):
        return web.json_response({"ok": True, "circuitBreaker": portfolio.get_circuit_breaker_state()})

    async def handle_api_breaker_reset(request: web.Request):
        state = portfolio.reset_circuit_breaker()
        return web.json_response({"ok": True, "circuitBreaker": state})

    async def handle_api_confirm_cancel(request: web.Request):
        data = await request.json()
        mint = (data.get("mint") or "").strip()
        if mint in portfolio.pending_confirmations:
            portfolio.cancel_pending(mint, "MANUAL_CANCEL", "Cancelled from Command Center")
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "No pending confirmation for this mint"})

    async def handle_api_scan_now(request: web.Request):
        add_agent_log("DISCOVERY", "⚡ Ručno pokrenuto instant skeniranje iz Web Command Centera!", "info")
        mints = await discovery.fetch_latest_solana_candidates(session, limit=3)
        scanned_count = 0
        for m in mints:
            await evaluate_and_record_candidate(session, m, "WEB_INSTANT_SCAN", researcher, portfolio, executor)
            scanned_count += 1
        set_runtime_phase("IDLE_WAITING")
        return web.json_response({"ok": True, "scanned": scanned_count})

    async def handle_api_wallets_add(request: web.Request):
        data = await request.json()
        address = data.get("address", "").strip()
        label = data.get("label", "").strip()
        ok = portfolio.add_tracked_wallet(address, label)
        return web.json_response({"ok": ok, "tracked_wallets": portfolio.get_tracked_wallets()})

    async def handle_api_wallets_remove(request: web.Request):
        data = await request.json()
        address = data.get("address", "").strip()
        ok = portfolio.remove_tracked_wallet(address)
        return web.json_response({"ok": ok, "tracked_wallets": portfolio.get_tracked_wallets()})

    async def handle_api_simulate_whale_buy(request: web.Request):
        wallets = portfolio.get_tracked_wallets()
        chosen_whale = wallets[0] if wallets else {"address": "suqh5sHtr8HyJ7q8scBimULPkPpA557prMG47xCHQfK", "label": "Cupsey (High-Freq Alpha)"}
        mints = await discovery.fetch_latest_solana_candidates(session, limit=2)
        target_mint = mints[0] if mints else "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

        add_agent_log(
            "WHALE TRACKER",
            f"🐋 [WHALE ALERT] '{chosen_whale['label']}' upravo kupio {target_mint[:8]}... (Prioritetna forenzika!)",
            "success"
        )
        fake_pf_event = {
            "mint": target_mint,
            "source": "WHALE_COPY_SIGNAL",
            "whale_label": chosen_whale["label"],
            "whale_wallet": chosen_whale["address"],
            "whale_sol_amount": 3.5
        }
        await evaluate_and_record_candidate(
            session, target_mint, "WHALE_COPY_SIGNAL", researcher, portfolio, executor, pf_event=fake_pf_event
        )
        set_runtime_phase("IDLE_WAITING")
        return web.json_response({"ok": True, "mint": target_mint, "whale": chosen_whale["label"]})

    async def handle_api_buy(request: web.Request):
        data = await request.json()
        mint = data.get("mint") or (data.get("market") or {}).get("mint")
        if not mint:
            return web.json_response({"ok": False, "error": "Nedostaje mint"}, status=400)
        market = await researcher.get_market_data(session, mint)
        if not market:
            portfolio.journal("BLOCK", mint=mint, symbol=(data.get("market") or {}).get("symbol"), rule="STALE_PRICE",
                              reasons="Manual buy: no fresh DexScreener quote (fail-closed)")
            return web.json_response({"ok": False, "error": "No fresh price quote — manual buy refused (fail-closed)"}, status=400)
        result = portfolio.request_entry(market, None, None, conviction="manual", source="MANUAL_WEB", skip_confirm=True)
        if result["status"] == "OPENED":
            await _dry_run_after_open(session, executor, portfolio, mint, market)
        return web.json_response({"ok": result["status"] == "OPENED", "result": result})

    async def handle_api_sell(request: web.Request):
        data = await request.json()
        mint = data.get("mint")
        closed = portfolio.close_position_manually(mint)
        return web.json_response({"ok": closed})

    async def handle_api_analyze(request: web.Request):
        data = await request.json()
        mint = data.get("mint", "").strip()
        if not mint:
            return web.json_response({"ok": False, "error": "Nedostaje mint adresa"}, status=400)

        add_agent_log("RESEARCH AI", f"Duboka On-Chain + Gemini analiza za: {mint[:8]}...", "ai")
        market_data, security_data = await asyncio.gather(
            researcher.get_market_data(session, mint),
            researcher.get_security_data(session, mint)
        )
        if not market_data:
            return web.json_response({"ok": False, "error": "Token nije pronađen na DexScreeneru"}, status=404)

        verdict = await researcher.ai_evaluate_token(market_data, security_data)
        portfolio.save_scan_result(market_data, security_data, verdict, "WEB_MANUAL")
        top10_raw = security_data.get("top10_holder_pct")
        top10_txt = f"{float(top10_raw):.1f}%" if top10_raw is not None else "N/A"
        add_agent_log(
            "RESEARCH AI",
            f"✨ [GEMINI ODLUKA] ${market_data['symbol']}: {verdict.decision} (Top10: {top10_txt})",
            "ai"
        )
        return web.json_response({
            "ok": True,
            "market": market_data,
            "security": security_data,
            "verdict": json.loads(verdict.model_dump_json())
        })

    async def handle_api_simulate_pump(request: web.Request):
        portfolio.simulate_pump_all_open(multiplier=2.15)
        return web.json_response({"ok": True})

    async def handle_api_reset(request: web.Request):
        portfolio.reset_portfolio_db()
        return web.json_response({"ok": True})

    async def handle_api_config(request: web.Request):
        data = await request.json()
        requested = data.get("strategyProfile")
        if isinstance(requested, str) and requested.strip():
            from bettercalltrader.strategies import PROFILES, is_known_profile

            if not is_known_profile(requested):
                return web.json_response(
                    {
                        "ok": False,
                        "error": f"unknown strategy profile {requested!r}",
                        "profiles": sorted(PROFILES),
                    },
                    status=400,
                )
        changed = apply_config_payload(data, portfolio, researcher, executor)
        add_agent_log("RISK MGR", f"Settings updated ({', '.join(changed) if changed else 'no changes'})", "info")
        if "strategyProfile" in changed:
            add_agent_log(
                "RISK MGR",
                f"Profil strategije promijenjen na '{portfolio.strategy_profile}' "
                f"({portfolio.trade_size_sol:g} SOL/trejd · stop {portfolio.stop_loss_pct:g}% · "
                f"TP1 {portfolio.tp1_pct:g}% · max {portfolio.max_open_positions} pozicija)",
                "warn" if portfolio.high_risk_mode else "info",
            )
        return web.json_response({"ok": True, "changed": changed, "config": build_config_payload(portfolio, researcher, executor)})

    async def handle_api_health(request: web.Request):
        return web.json_response({
            "ok": True,
            "version": "v12.0",
            "serverTimeUtc": fmt_utc(portfolio.now()),
            "serverTimeMs": int(portfolio.now_ts() * 1000),
            "telegramEnabled": TELEGRAM.enabled,
            "liveTradingEnabled": executor.live_trading_enabled,
            "openPositions": portfolio.count_open_positions(),
        })

    app.router.add_get("/", handle_index)
    for console_page in ("risk", "wallets", "backtest", "ml", "system", "settings"):
        app.router.add_get(f"/{console_page}", make_page_handler(f"{console_page}.html"))
    webui_dir = WEBUI_DIR
    if not webui_dir.exists():
        webui_dir = Path.cwd() / "html" / "webui"
    if webui_dir.exists():
        app.router.add_static("/webui/", webui_dir)
    app.router.add_get("/api/health", handle_api_health)
    app.router.add_get("/api/state", handle_api_state)
    app.router.add_get("/api/journal", handle_api_journal)
    app.router.add_get("/api/equity", handle_api_equity)
    app.router.add_get("/api/metrics", handle_api_metrics)
    app.router.add_get("/metrics", handle_metrics)
    app.router.add_get("/ready", handle_ready)
    app.router.add_get("/api/portfolio_risk", handle_api_portfolio_risk)
    app.router.add_get("/api/audit", handle_api_audit)
    app.router.add_get("/api/ml/status", handle_api_ml_status)
    app.router.add_get("/api/ml/predict", handle_api_ml_predict)
    app.router.add_post("/api/ml/train", handle_api_ml_train)
    app.router.add_get("/api/backtest", handle_api_backtest)
    app.router.add_get("/analytics", handle_analytics)
    app.router.add_get("/api/wallet_quality", handle_api_wallet_quality)
    app.router.add_get("/api/circuit_breaker", handle_api_breaker)
    app.router.add_post("/api/breaker/reset", handle_api_breaker_reset)
    app.router.add_post("/api/confirmations/cancel", handle_api_confirm_cancel)
    app.router.add_post("/api/scan_now", handle_api_scan_now)
    app.router.add_post("/api/wallets/add", handle_api_wallets_add)
    app.router.add_post("/api/wallets/remove", handle_api_wallets_remove)
    app.router.add_post("/api/simulate_whale_buy", handle_api_simulate_whale_buy)
    app.router.add_post("/api/buy", handle_api_buy)
    app.router.add_post("/api/sell", handle_api_sell)
    app.router.add_post("/api/analyze", handle_api_analyze)
    app.router.add_post("/api/simulate_pump", handle_api_simulate_pump)
    app.router.add_post("/api/reset", handle_api_reset)
    app.router.add_post("/api/config", handle_api_config)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, COMMAND_CENTER_HOST, port)
    await site.start()
    url = f"http://127.0.0.1:{port}"
    print(f"🌐 [Command Center Bridge] Lokalni API & Web server aktivan na: {url}")
    add_agent_log("DISCOVERY", f"Spojen lokalni SQLite Command Center Bridge v12.0 na {url}", "success")
    return runner


# =====================================================================
# 6. GLAVNI ORKESTRATOR (v9.0)
# =====================================================================

async def _position_monitor_loop(
    portfolio: PaperTradingManager,
    researcher: ResearchAgent,
    session: aiohttp.ClientSession,
):
    while True:
        try:
            await portfolio.monitor_open_positions(session, researcher)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            add_agent_log("RISK MGR", f"Monitor greška: {e}", "warn")
        await asyncio.sleep(max(1.0, float(portfolio.position_monitor_sec)))


async def _confirmation_loop(
    portfolio: PaperTradingManager,
    researcher: ResearchAgent,
    executor: SolanaExecutionAgent,
    session: aiohttp.ClientSession,
):
    while True:
        try:
            if portfolio.pending_confirmations:
                await process_due_confirmations(session, researcher, portfolio, executor)
            await TELEGRAM.flush_and_poll(session, portfolio, researcher, executor)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            add_agent_log("RISK MGR", f"Confirm/Telegram loop error: {e}", "warn")
        await asyncio.sleep(2.0)


async def run_research_pipeline(
    cycles: Optional[int] = None,
    interval_seconds: int = 20,
    enable_pumpfun_ws: bool = True,
    live_trading_enabled: Optional[bool] = None,
    server_port: int = 8080
):
    """Run the bot until it is stopped.

    ``cycles=None`` (the default) means run indefinitely: the pipeline keeps
    scanning until ``Ctrl+C`` / ``SIGTERM``.
    Pass a positive integer to cap the run at that many cycles.
    """
    print("=" * 70)
    print("🚀 SOLANA MEMECOIN MULTI-AGENT BOT (v12.0 — Mint/Freeze Fix · Time-Stop · Step-Up · Telegram)")
    print("=" * 70)

    researcher = ResearchAgent()
    llm = researcher.llm
    if llm.available:
        where = "LOCAL" if llm.is_local else "REMOTE"
        chain = " → ".join(llm.describe()["chain"]) or llm.label
        print(f"🧠 [AI Status] {where} AI aktivan: {llm.label} (lanac: {chain}).")
        add_agent_log("RESEARCH AI", f"AI inicijaliziran: {llm.label} ({where}).", "ai")
    else:
        print(
            "ℹ️ [AI Status] Nijedan LLM provider nije konfiguriran — radim u heurističkom modu. "
            "Postavi GEMINI_API_KEY ili LLM_BASE_URL (+ LLM_MODEL) za AI forenziku."
        )
        add_agent_log("RESEARCH AI", "Nema LLM providera — koristim brzi heuristički mod.", "warn")

    discovery = DiscoveryAgent()
    pumpfun_stream = PumpFunStreamAgent(max_dev_buy_sol=15.0, min_initial_sol_in_curve=30.2)
    executor = SolanaExecutionAgent(live_trading_enabled=live_trading_enabled)
    portfolio = PaperTradingManager(db_path=os.environ.get("PAPER_DB_PATH") or "paper_portfolio.db")

    mode_str = "🔥 LIVE ON-CHAIN TRADING" if executor.live_trading_enabled else "🛡️ PAPER TRADING + JUPITER DRY-RUN"
    print(f"⚙️  Mod izvršavanja: {mode_str} | Telegram: {'ON ✅' if TELEGRAM.enabled else 'OFF (dodaj u .env)'}")
    print(f"🧾 Config hash: {portfolio.config_hash} (profile: {portfolio.strategy_profile})")
    if portfolio.high_risk_mode:
        print(
            "🔥 [RISK MODE] HIGH RISK / HIGH REWARD — "
            f"{portfolio.trade_size_sol:g} SOL/trade · {portfolio.max_open_positions} max open · "
            f"stop {portfolio.stop_loss_pct:g}% · TP1 {portfolio.tp1_pct:g}% "
            f"(sell {portfolio.tp1_fraction:.0%}) · trailing {portfolio.trailing_stop_pct:g}% → "
            f"{portfolio.trailing_stop_pct_after_tp1:g}% after TP1 · "
            f"max daily drawdown {portfolio.max_daily_drawdown_pct:g}%"
        )
        print(
            "   ⚠️  Profil nadjačava .env vrijednosti za sizing i izlaze. "
            "Ulazni filteri (likvidnost, holderi, mint/freeze) ostaju nepromijenjeni."
        )
        add_agent_log(
            "RISK MGR",
            f"HIGH RISK mod aktivan: {portfolio.trade_size_sol:g} SOL/trade, "
            f"{portfolio.max_open_positions} pozicija, stop {portfolio.stop_loss_pct:g}%, "
            f"TP1 {portfolio.tp1_pct:g}% — profil nadjačava .env.",
            "warn",
        )
    portfolio.print_portfolio_summary()

    if TELEGRAM.enabled:
        TELEGRAM.enqueue("🚀 <b>Solana Agentic Bot v9.0 pokrenut!</b>\nPošalji /help za listu komandi.")
        if portfolio.high_risk_mode:
            TELEGRAM.enqueue(
                "🔥 <b>HIGH RISK / HIGH REWARD mod aktivan</b>\n"
                f"{portfolio.trade_size_sol:g} SOL/trade · {portfolio.max_open_positions} max otvorenih · "
                f"stop {portfolio.stop_loss_pct:g}% · TP1 {portfolio.tp1_pct:g}% · "
                f"max dnevni drawdown {portfolio.max_daily_drawdown_pct:g}%"
            )

    async with aiohttp.ClientSession() as session:
        runner = None
        monitor_task = None
        confirm_task = None
        shutdown = ShutdownController().install()
        try:
            runner = await start_command_center_server(
                portfolio, researcher, discovery, executor, session, port=server_port
            )
            try:
                webbrowser.open(f"http://127.0.0.1:{server_port}")
            except Exception:
                pass
        except Exception as e:
            print(f"⚠️ [Command Center Bridge] Port {server_port} je zauzet ({e}), nastavljam u terminal modu.")

        try:
            monitor_task = asyncio.create_task(_position_monitor_loop(portfolio, researcher, session))
            confirm_task = asyncio.create_task(_confirmation_loop(portfolio, researcher, executor, session))
            cycle_iter = itertools.count(1) if cycles is None else range(1, cycles + 1)
            for cycle in cycle_iter:
                if shutdown.requested:
                    add_agent_log("SYSTEM", f"Shutdown requested ({shutdown.reason}); stopping before cycle {cycle}.", "warn")
                    break
                RUNTIME_STATUS["cycle"] = cycle
                if cycles is None:
                    print(f"\n🔄 Pokrećem ciklus {cycle} (Web Dashboard: http://127.0.0.1:{server_port})...")
                else:
                    print(f"\n🔄 Pokrećem ciklus {cycle}/{cycles} (Web Dashboard: http://127.0.0.1:{server_port})...")

                dex_mints = await discovery.fetch_latest_solana_candidates(session, limit=3)
                for mint in dex_mints:
                    if shutdown.requested:
                        break
                    await evaluate_and_record_candidate(
                        session, mint, "DEXSCREENER", researcher, portfolio, executor
                    )
                    await asyncio.sleep(0.8)

                if enable_pumpfun_ws and not shutdown.requested:
                    tracked_map = portfolio.get_tracked_wallets_map()
                    pf_events = await pumpfun_stream.listen_for_events(
                        session, tracked_wallets_map=tracked_map, duration_seconds=8, max_candidates=2
                    )
                    for ev in pf_events:
                        if shutdown.requested:
                            break
                        src = ev.get("source", "PUMPFUN_STREAM")
                        await evaluate_and_record_candidate(
                            session, ev["mint"], src, researcher, portfolio, executor, pf_event=ev
                        )
                        await asyncio.sleep(0.8)

                portfolio.print_portfolio_summary()
                set_runtime_phase("IDLE_WAITING", None)

                if shutdown.requested:
                    break
                if cycles is None or cycle < cycles:
                    try:
                        await asyncio.wait_for(shutdown.event.wait(), timeout=interval_seconds)
                    except asyncio.TimeoutError:
                        pass
        finally:
            set_runtime_phase("SHUTTING_DOWN", None)
            for task in (monitor_task, confirm_task):
                if task:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
            if runner:
                await runner.cleanup()
            shutdown.uninstall()
            audit_event(
                AUDIT_LOG_PATH,
                "shutdown",
                reason=shutdown.reason or "completed",
                cycle=RUNTIME_STATUS.get("cycle"),
            )
            add_agent_log("SYSTEM", f"Shutdown complete ({shutdown.reason or 'completed'}).", "info")
            print("\n[Shutdown] Tasks cancelled, HTTP runner closed, audit event written.")


if __name__ == "__main__":
    try:
        asyncio.run(
            run_research_pipeline(
                cycles=None,
                interval_seconds=20,
                server_port=COMMAND_CENTER_PORT,
            )
        )
    except KeyboardInterrupt:
        print("\n[Shutdown] Interrupted before the event loop was ready.")
