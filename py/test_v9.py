import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
for _path in (ROOT, PROJECT_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import research_bot as rb

# The self-tests assert the historical balanced numbers. Pin the profile so a
# local .env (or a Command Center choice) cannot change what they measure; the
# profile tests override and restore this value themselves.
os.environ["STRATEGY_PROFILE"] = "balanced"


def make_temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    return path


def cleanup_db(path):
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def test_env_helpers():
    os.environ["TEST_FLOAT"] = "1.25"
    os.environ["TEST_INT"] = "7"
    os.environ["TEST_BOOL"] = "true"
    assert rb.env_float("TEST_FLOAT", 0) == 1.25
    assert rb.env_int("TEST_INT", 0) == 7
    assert rb.env_bool("TEST_BOOL", False) is True
    assert rb.env_float("MISSING_FLOAT", 3.5) == 3.5
    print("  env helpers: OK")


def test_paper_fill():
    mid = 1.0
    buy_fill, buy_slip = rb.paper_fill(mid, "BUY", 0.5, 100000.0, 50, 1.0, 185.0)
    sell_fill, sell_slip = rb.paper_fill(mid, "SELL", 0.5, 100000.0, 50, 1.0, 185.0)
    assert buy_fill > mid
    assert sell_fill < mid
    assert 0 <= buy_slip <= 0.5
    assert 0 <= sell_slip <= 0.5
    # Cap check
    _, capped = rb.paper_fill(mid, "BUY", 1000.0, 1.0, 50, 1.0, 185.0)
    assert capped <= 0.5
    print("  paper_fill: OK")


def test_exit_icon():
    assert rb.exit_icon("STOP_LOSS") == "🛑"
    assert rb.exit_icon("TAKE_PROFIT_1") == "🎯"
    assert rb.exit_icon("UNKNOWN") == "📤"
    print("  exit_icon: OK")


def test_portfolio_basic():
    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        assert abs(portfolio.get_wallet_status()["balance_sol"] - 10.0) < 1e-9

        now_ts = portfolio.now_ts()
        market = {
            "mint": "TestMint11111111111111111111111111111111111",
            "symbol": "TEST",
            "name": "Test Token",
            "price_usd": "1.0",
            "liquidity_usd": 100000.0,
            "dex_id": "raydium",
            "fetched_at": now_ts,
        }

        opened = portfolio.open_virtual_position(market, conviction="manual", source="TEST")
        assert opened is True, "position should open"
        positions = portfolio.get_all_positions()
        open_positions = [p for p in positions if p["status"] == "OPEN"]
        assert len(open_positions) == 1
        assert open_positions[0]["symbol"] == "TEST"
        assert portfolio.get_wallet_status()["balance_sol"] < 10.0

        mint = market["mint"]
        closed = portfolio.close_position_manually(mint, exit_reason="MANUAL_WEB_EXIT")
        assert closed is True
        assert portfolio.get_wallet_status()["open_positions"] == 0
        assert len(portfolio.get_journal(10)) > 0
        assert len(portfolio.get_equity_series()["points"]) >= 1
        print("  portfolio basic open/close/journal/equity: OK")
    finally:
        cleanup_db(db_path)


def test_risk_snapshot_and_health_shape():
    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=5.0, trade_size_sol=0.25)
        researcher = rb.ResearchAgent()
        executor = rb.SolanaExecutionAgent(live_trading_enabled=False)
        cfg = rb.build_config_payload(portfolio, researcher, executor)
        assert cfg["version"] == "v13.0"
        assert cfg["liveTradingEnabled"] is False
        risk = portfolio.get_risk_snapshot()
        assert "circuitBreaker" in risk
        assert "entryGate" in risk
        assert "pendingConfirmations" in risk
        assert "sizing" in risk
        cb = portfolio.get_circuit_breaker_state()
        assert "tripped" in cb
        assert "day" in cb
        print("  risk snapshot/config/circuit breaker: OK")
    finally:
        cleanup_db(db_path)


def test_confirmation_cancel():
    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        portfolio.entry_confirm_sec = 20.0
        now_ts = portfolio.now_ts()
        market = {
            "mint": "ConfirmMint11111111111111111111111111111111",
            "symbol": "CONF",
            "name": "Confirm Token",
            "price_usd": "1.0",
            "liquidity_usd": 100000.0,
            "dex_id": "raydium",
            "fetched_at": now_ts,
        }
        result = portfolio.request_entry(market, conviction="manual", source="TEST")
        assert result["status"] == "PENDING", result
        assert market["mint"] in portfolio.pending_confirmations
        portfolio.cancel_pending(market["mint"], "MANUAL_CANCEL", "test cancel")
        assert market["mint"] not in portfolio.pending_confirmations
        print("  entry confirmation pending/cancel: OK")
    finally:
        cleanup_db(db_path)


def test_v12_pending_persistence():
    """Pending entries survive a restart, expire when stale, and still confirm."""
    from datetime import timedelta

    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        portfolio.entry_confirm_sec = 20.0
        mint = "PersistMint1111111111111111111111111111111"
        market = {
            "mint": mint,
            "symbol": "PERSIST",
            "name": "Persist Token",
            "price_usd": "1.0",
            "liquidity_usd": 100000.0,
            "dex_id": "raydium",
            "fetched_at": portfolio.now_ts(),
        }
        assert portfolio.request_entry(market, conviction="manual", source="TEST")["status"] == "PENDING"

        conn = portfolio._conn()
        stored = conn.execute("SELECT COUNT(*) FROM pending_confirmations").fetchone()[0]
        conn.close()
        assert stored == 1, "pending entry should be written to SQLite"

        restored = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        assert mint in restored.pending_confirmations, "pending entry should survive a restart"
        pending = restored.get_pending_confirmations()
        assert len(pending) == 1 and pending[0]["mint"] == mint
        assert abs(pending[0]["signalPriceUsd"] - 1.0) < 1e-12

        # A restored but due entry can still be confirmed with a fresh quote.
        restored.cluster_cooldown_sec = 0
        restored.stop_loss_cooldown_sec = 0
        restored.min_seconds_between_entries = 0
        restored.max_daily_stop_losses = 100
        restored.max_daily_trades = 100
        restored.cluster_unproven_sec = 0
        entry = restored.pending_confirmations[mint]
        entry["due_at"] = restored.now() - timedelta(seconds=1)
        restored._persist_pending(entry)
        fresh = dict(market, price_usd="1.01", fetched_at=restored.now_ts())
        result = restored.process_confirmation(mint, fresh, None)
        assert result["status"] == "OPENED", result
        assert (restored.now() - entry["due_at"]).total_seconds() >= 0

        conn = restored._conn()
        left = conn.execute("SELECT COUNT(*) FROM pending_confirmations").fetchone()[0]
        conn.close()
        assert left == 0, "confirmed entries must be removed from disk"

        # An entry that aged past the recovery window is dropped, not resumed.
        stale_mint = "PersistOld111111111111111111111111111111111"
        stale_market = dict(market, mint=stale_mint, price_usd="2.0", fetched_at=restored.now_ts())
        restored.entry_confirm_sec = 20.0
        restored.entry_confirm_max_drop_pct = 100.0
        assert restored.request_entry(stale_market, conviction="manual", source="TEST")["status"] == "PENDING"
        stale_entry = restored.pending_confirmations[stale_mint]
        stale_entry["created_at"] = restored.now() - timedelta(seconds=1_000)
        restored._persist_pending(stale_entry)

        recovered = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        assert stale_mint not in recovered.pending_confirmations
        assert any(row["rule"] == "CONFIRM_EXPIRED" for row in recovered.get_journal(20))
        print("  v12 pending confirmation persistence/expiry: OK")
    finally:
        cleanup_db(db_path)


def test_v12_pumpfun_curve_quote():
    """A curve-only token gets a quote for confirmation and manual buys."""
    import asyncio

    import aiohttp
    from aiohttp import web

    async def run():
        async def dex(request):
            return web.json_response({"pairs": None})

        async def coin(request):
            return web.json_response({
                "mint": request.match_info["mint"] if "mint" in request.match_info else "CurveMint",
                "name": "Curve Token",
                "symbol": "CURVE",
                "decimals": 6,
                "total_supply": 1_000_000_000_000_000,
                "virtual_sol_reserves": 45_000_000_000,
                "virtual_token_reserves": 800_000_000_000_000,
                "usd_market_cap": 9_000.0,
                "complete": False,
                "twitter": "https://x.com/curve",
            })

        app = web.Application()
        app.router.add_get("/dex/{mint}", dex)
        app.router.add_get("/coins/{mint}", coin)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        mint = "CurveMint111111111111111111111111111111111"
        try:
            researcher = rb.ResearchAgent()
            researcher.DEXSCREENER_TOKEN_URL = f"http://127.0.0.1:{port}/dex/{{mint}}"
            researcher.PUMPFUN_COIN_URL = f"http://127.0.0.1:{port}/coins/{{mint}}"
            researcher.curve_cache_sec = 0.0
            researcher.curve_miss_cache_sec = 0.0
            rb.PRICE_CACHE.invalidate(mint)
            async with aiohttp.ClientSession() as session:
                market = await researcher.get_market_data(session, mint)
                assert market is not None, "curve quote should replace the missing DexScreener pair"
                assert market["dex_id"] == "PUMPFUN_CURVE"
                assert market["curve"] is True
                assert abs(float(market["price_usd"]) - 9_000.0 / 1_000_000_000) < 1e-15
                expected_liq = 45.0 * researcher.curve_sol_price_usd
                assert abs(market["liquidity_usd"] - expected_liq) < 1e-9
                assert market["bonding_curve_pct"] > 0
                assert market["socials"] and market["socials"][0]["type"] == "twitter"

                # WS fallback quotes are remembered for the next recheck.
                event_mint = "CurveEvent11111111111111111111111111111111"
                rb.PRICE_CACHE.invalidate(event_mint)
                event = {
                    "mint": event_mint, "source": "PUMPFUN_LIVE", "name": "Event", "symbol": "EV",
                    "v_sol_in_curve": 45.0, "real_sol_in_curve": 15.0, "bonding_curve_pct": 17.6,
                    "mcap_sol": 30.0,
                }
                fallback = await researcher.build_pumpfun_fallback_market_data(session, event, 185.0)
                assert event_mint in researcher._curve_quotes
                assert researcher._curve_quotes[event_mint][1]["mint"] == event_mint
                assert fallback["curve"] is True
        finally:
            await runner.cleanup()

        # The curve quote is fresh enough for the paper entry flow.
        db_path = make_temp_db()
        try:
            portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
            portfolio.entry_confirm_sec = 20.0
            portfolio.stale_price_sec = 300
            result = portfolio.request_entry(market, conviction="manual", source="TEST")
            assert result["status"] == "PENDING", result
            portfolio.cancel_pending(mint, "MANUAL_CANCEL", "test done")
        finally:
            cleanup_db(db_path)

    asyncio.run(run())
    print("  v12 Pump.fun curve quote: OK")


def test_exit_rules():
    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        portfolio.cluster_cooldown_sec = 0
        portfolio.stop_loss_cooldown_sec = 0
        portfolio.min_seconds_between_entries = 0
        portfolio.max_daily_stop_losses = 100
        portfolio.max_daily_trades = 100
        now_ts = portfolio.now_ts()
        market = {
            "mint": "ExitMint11111111111111111111111111111111111",
            "symbol": "EXIT",
            "name": "Exit Token",
            "price_usd": "1.0",
            "liquidity_usd": 100000.0,
            "dex_id": "raydium",
            "fetched_at": now_ts,
        }
        assert portfolio.open_virtual_position(market, conviction="manual", source="TEST") is True
        mint = market["mint"]

        fast_market = dict(market, price_usd="0.85", fetched_at=portfolio.now_ts())
        action = portfolio.process_position_tick(mint, fast_market)
        assert action == "FAST_INVALIDATION", action
        print("  fast invalidation exit: OK")

        portfolio.fast_invalidation_pct = -100.0

        # Reopen for stop-loss test
        market2 = dict(market, mint="StopMint1111111111111111111111111111111111", symbol="STOP")
        assert portfolio.open_virtual_position(market2, conviction="manual", source="TEST") is True
        stop_market = dict(market2, price_usd="0.70", fetched_at=portfolio.now_ts())
        action = portfolio.process_position_tick(market2["mint"], stop_market)
        assert action == "STOP_LOSS", action
        print("  stop loss exit: OK")

        # Reopen for TP1 test
        market3 = dict(market, mint="TpMint111111111111111111111111111111111111", symbol="TP")
        assert portfolio.open_virtual_position(market3, conviction="manual", source="TEST") is True
        tp_market = dict(market3, price_usd="2.1", fetched_at=portfolio.now_ts())
        action = portfolio.process_position_tick(market3["mint"], tp_market)
        assert action == "TAKE_PROFIT_1", action
        print("  take profit 1 exit: OK")
    finally:
        cleanup_db(db_path)


def test_flow_exit():
    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        portfolio.cluster_cooldown_sec = 0
        portfolio.stop_loss_cooldown_sec = 0
        portfolio.min_seconds_between_entries = 0
        portfolio.max_daily_stop_losses = 100
        portfolio.max_daily_trades = 100
        now_ts = portfolio.now_ts()
        market = {
            "mint": "FlowMint11111111111111111111111111111111111",
            "symbol": "FLOW",
            "name": "Flow Token",
            "price_usd": "1.0",
            "liquidity_usd": 100000.0,
            "dex_id": "raydium",
            "fetched_at": now_ts,
            "flow_ok": True,
            "buys_m5": 1,
            "sells_m5": 5,
        }
        assert portfolio.open_virtual_position(market, conviction="manual", source="TEST") is True
        tick_market = dict(market, fetched_at=portfolio.now_ts(), flow_ok=True, buys_m5=1, sells_m5=5, price_usd="1.0")
        action = portfolio.process_position_tick(market["mint"], tick_market)
        assert action == "FLOW_EXIT", action
        print("  flow exit: OK")
    finally:
        cleanup_db(db_path)


def test_whale_exit():
    from datetime import timedelta

    db_path = make_temp_db()
    mint = "WhaleMint11111111111111111111111111111111"
    wallet = "Wallet1111111111111111111111111111111111"
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        portfolio.cluster_cooldown_sec = 0
        portfolio.stop_loss_cooldown_sec = 0
        portfolio.min_seconds_between_entries = 0
        portfolio.max_daily_stop_losses = 100
        portfolio.max_daily_trades = 100
        portfolio.whale_exit_enabled = True
        portfolio.whale_event_ttl_sec = 1800.0
        now_ts = portfolio.now_ts()
        market = {
            "mint": mint,
            "symbol": "WHALE",
            "name": "Whale Token",
            "price_usd": "1.0",
            "liquidity_usd": 100000.0,
            "dex_id": "raydium",
            "fetched_at": now_ts,
            "whale_signal": "SmartWhale",
            "whale_wallet": wallet,
        }
        assert portfolio.open_virtual_position(market, conviction="manual", source="TEST") is True
        # Push the entry a few seconds back so "after entry" is unambiguous.
        conn = portfolio._conn()
        conn.execute(
            "UPDATE positions SET opened_at = ? WHERE mint = ?",
            (rb.fmt_utc(portfolio.now() - timedelta(seconds=5)), mint),
        )
        conn.commit()
        conn.close()
        tick_market = dict(market, fetched_at=portfolio.now_ts(), price_usd="1.0")

        # A different tracked whale selling this mint must not close the position.
        rb.WHALE_SELL_EVENTS.pop(mint, None)
        rb.record_whale_sell(mint, "OtherWallet999", "OtherWhale")
        assert portfolio.process_position_tick(mint, tick_market) is None
        print("  whale exit ignores other wallets: OK")

        # A stale sell event must not close it either.
        rb.record_whale_sell(
            mint, wallet, "SmartWhale",
            ts=portfolio.now_ts() - portfolio.whale_event_ttl_sec - 60,
        )
        assert portfolio.process_position_tick(mint, tick_market) is None
        print("  whale exit ignores stale events: OK")

        # Only the whale this position followed, selling after entry, triggers it.
        # Use the portfolio clock for the event timestamp so the comparison does
        # not depend on the host clock source (time.time vs datetime).
        rb.record_whale_sell(mint, wallet, "SmartWhale", ts=portfolio.now_ts())
        tick_market = dict(tick_market, fetched_at=portfolio.now_ts())
        action = portfolio.process_position_tick(mint, tick_market)
        assert action == "WHALE_EXIT", action
        print("  whale exit: OK")
    finally:
        rb.WHALE_SELL_EVENTS.pop(mint, None)
        cleanup_db(db_path)


def test_time_stop():
    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        portfolio.cluster_cooldown_sec = 0
        portfolio.stop_loss_cooldown_sec = 0
        portfolio.min_seconds_between_entries = 0
        portfolio.max_daily_stop_losses = 100
        portfolio.max_daily_trades = 100
        portfolio.time_stop_enabled = True
        portfolio.max_hold_no_pump_sec = 0
        portfolio.time_stop_min_profit_pct = 1000.0
        now_ts = portfolio.now_ts()
        market = {
            "mint": "TimeMint11111111111111111111111111111111111",
            "symbol": "TIME",
            "name": "Time Token",
            "price_usd": "1.0",
            "liquidity_usd": 100000.0,
            "dex_id": "raydium",
            "fetched_at": now_ts,
        }
        assert portfolio.open_virtual_position(market, conviction="manual", source="TEST") is True
        tick_market = dict(market, fetched_at=portfolio.now_ts(), price_usd="1.0")
        action = portfolio.process_position_tick(market["mint"], tick_market)
        assert action == "TIME_STOP", action
        print("  time stop: OK")
    finally:
        cleanup_db(db_path)


def test_circuit_breaker_trips():
    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        portfolio.cluster_cooldown_sec = 0
        portfolio.stop_loss_cooldown_sec = 0
        portfolio.min_seconds_between_entries = 0
        portfolio.max_daily_stop_losses = 1
        portfolio.max_daily_trades = 100
        portfolio.fast_invalidation_window_sec = 0
        now_ts = portfolio.now_ts()
        market = {
            "mint": "BreakerMint1111111111111111111111111111111",
            "symbol": "BRK",
            "name": "Breaker Token",
            "price_usd": "1.0",
            "liquidity_usd": 100000.0,
            "dex_id": "raydium",
            "fetched_at": now_ts,
        }
        assert portfolio.open_virtual_position(market, conviction="manual", source="TEST") is True
        tick_market = dict(market, fetched_at=portfolio.now_ts(), price_usd="0.70")
        action = portfolio.process_position_tick(market["mint"], tick_market)
        assert action == "STOP_LOSS", action
        cb = portfolio.get_circuit_breaker_state()
        assert cb["tripped"] is True, cb
        print("  circuit breaker trips: OK")
    finally:
        cleanup_db(db_path)


def test_v10_schema_and_metrics():
    import asyncio
    import sqlite3
    from bettercalltrader.metrics import collect_basic_metrics
    from bettercalltrader.migrations import apply_migrations, CURRENT_SCHEMA_VERSION

    db_path = make_temp_db()
    log_path = db_path + ".log"
    audit_path = db_path + ".audit.jsonl"
    original_audit_path = rb.AUDIT_LOG_PATH
    rb.AUDIT_LOG_PATH = audit_path
    logger = None
    try:
        version = apply_migrations(db_path)
        assert version == CURRENT_SCHEMA_VERSION

        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE trade_history (net_pnl_sol REAL)")
        conn.execute("CREATE TABLE positions (status TEXT)")
        conn.executemany("INSERT INTO trade_history (net_pnl_sol) VALUES (?)", [(0.5,), (-0.2,), (0.1,)])
        conn.execute("INSERT INTO positions (status) VALUES ('OPEN')")
        conn.execute("INSERT INTO positions (status) VALUES ('CLOSED')")
        conn.commit()
        conn.close()

        metrics = collect_basic_metrics(db_path)
        assert metrics["totalTrades"] == 3.0
        assert metrics["winningTrades"] == 2.0
        assert abs(metrics["winRate"] - (2.0 / 3.0)) < 1e-9
        assert abs(metrics["netPnlSol"] - 0.4) < 1e-9
        assert metrics["openPositions"] == 1.0

        from bettercalltrader.observability import configure_file_logger
        logger = configure_file_logger(log_path)
        logger.info("v10 observability test")
        assert Path(log_path).exists() and Path(log_path).stat().st_size > 0

        agent = rb.SolanaExecutionAgent(live_trading_enabled=True, private_key_b58="")
        blocked = asyncio.run(agent.execute_buy(None, "TESTMINT", 0.01))
        assert blocked["status"] == "BLOCKED" and "LIVE_TRADING_CONFIRMED" in blocked["reason"]

        agent.live_trading_confirmed = True
        too_big = asyncio.run(agent.execute_buy(None, "TESTMINT", agent.max_live_trade_sol + 1.0))
        assert too_big["status"] == "BLOCKED" and "MAX_LIVE_TRADE_SOL" in too_big["reason"]

        risk_db = make_temp_db()
        try:
            pm = rb.PaperTradingManager(db_path=risk_db, initial_sol=10.0)
            risk = pm.get_portfolio_risk()
            assert risk["openValueSol"] == 0.0
            assert risk["maxHistoricalDrawdownPct"] == 0.0
            assert risk["realizedTrades"] == 0
        finally:
            cleanup_db(risk_db)
        print("  v10 schema/metrics/observability/guards: OK")
    finally:
        rb.AUDIT_LOG_PATH = original_audit_path
        cleanup_db(db_path)
        if logger is not None:
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
        for path in (log_path, audit_path):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def test_v10_risk_and_backtest():
    import sqlite3
    from bettercalltrader.risk_analytics import herfindahl_index, summarize
    from bettercalltrader.backtest import build_report

    stats = summarize([0.5, -0.2, 1.0, -0.4], initial_equity=10.0)
    assert stats["trades"] == 4
    assert abs(stats["netPnlSol"] - 0.9) < 1e-9
    assert abs(stats["winRate"] - 0.5) < 1e-9
    assert abs(stats["profitFactor"] - 2.5) < 1e-9
    assert abs(stats["var95Sol"] - 0.4) < 1e-9
    assert abs(stats["expectedShortfall95Sol"] - 0.4) < 1e-9
    assert abs(stats["maxDrawdownPct"] - (0.4 / 11.3 * 100.0)) < 1e-6
    assert abs(herfindahl_index([50.0, 50.0]) - 0.5) < 1e-9
    assert summarize([])["profitFactor"] == 0.0
    assert summarize([0.5, 0.2])["profitFactor"] is None

    db_path = make_temp_db()
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT, symbol TEXT, opened_at TEXT, closed_at TEXT,
                invested_sol REAL, returned_sol REAL, net_pnl_sol REAL,
                exit_reason TEXT, tp1_hit INTEGER, slippage_sol REAL,
                conviction TEXT, source TEXT
            )
            """
        )
        rows = [
            ("m1", "AAA", "2026-01-01 10:00:00", "2026-01-01 11:00:00", 0.5, 0.7, 0.2, "TAKE-PROFIT 1", 1, 0.01, "high", "DEX"),
            ("m2", "BBB", "2026-01-02 10:00:00", "2026-01-02 11:00:00", 0.5, 0.3, -0.2, "STOP_LOSS", 0, 0.02, "ai", "PUMPFUN"),
            ("m3", "CCC", "2026-01-03 10:00:00", "2026-01-03 11:00:00", 0.5, 1.5, 1.0, "TAKE-PROFIT 2", 1, 0.01, "high", "DEX"),
        ]
        conn.executemany(
            "INSERT INTO trade_history (mint, symbol, opened_at, closed_at, invested_sol, returned_sol, net_pnl_sol, exit_reason, tp1_hit, slippage_sol, conviction, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()

        report = build_report(db_path, initial_sol=10.0)
        assert report["summary"]["trades"] == 3
        assert abs(report["summary"]["netPnlSol"] - 1.0) < 1e-9
        assert report["byExitReason"]["STOP_LOSS"]["trades"] == 1
        assert report["bySource"]["DEX"]["trades"] == 2
        assert len(report["equityCurve"]) == 3
        assert report["bestTrade"]["symbol"] == "CCC"
        assert report["worstTrade"]["symbol"] == "BBB"
        assert report["dailyPnl"]["2026-01-03"] == 1.0
    finally:
        cleanup_db(db_path)

    empty_path = make_temp_db()
    try:
        empty = build_report(empty_path)
        assert empty["summary"]["trades"] == 0
    finally:
        cleanup_db(empty_path)
    print("  v10 risk analytics/backtest: OK")


def test_v10_security():
    from bettercalltrader.security import RateLimiter, client_ip, is_authorized, is_mutation_allowed

    assert is_authorized(None, "") is True
    assert is_authorized("abc", "abc") is True
    assert is_authorized("abc", "xyz") is False
    assert is_authorized(None, "xyz") is False

    # X-Forwarded-For is spoofable unless a trusted proxy is declared.
    assert client_ip({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}, "9.9.9.9") == "9.9.9.9"
    assert client_ip({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}, "9.9.9.9", trust_forwarded=True) == "1.2.3.4"
    assert client_ip({}, "9.9.9.9") == "9.9.9.9"

    # CSRF: non-browser clients (no Origin) and same-origin fetches pass;
    # genuine cross-site mutations are rejected unless explicitly allowed.
    allowed = {"http://localhost:8080"}
    assert is_mutation_allowed({}, allowed) is True
    assert is_mutation_allowed({"Sec-Fetch-Site": "same-origin"}, allowed) is True
    assert is_mutation_allowed({"Sec-Fetch-Site": "none"}, allowed) is True
    assert is_mutation_allowed({"Origin": "http://localhost:8080"}, allowed) is True
    assert is_mutation_allowed({"Origin": "http://127.0.0.1:9000"}, allowed) is True
    assert is_mutation_allowed({"Origin": "http://evil.example"}, allowed) is False
    assert is_mutation_allowed(
        {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}, allowed
    ) is False
    assert is_mutation_allowed({"Origin": "http://evil.example"}, allowed, allow_all=True) is True

    limiter = RateLimiter(max_requests=3, window_seconds=60.0)
    assert limiter.allow("ip", now=0.0) == (True, 0.0)
    assert limiter.allow("ip", now=1.0) == (True, 0.0)
    assert limiter.allow("ip", now=2.0) == (True, 0.0)
    allowed, retry_after = limiter.allow("ip", now=3.0)
    assert allowed is False and retry_after > 0
    assert limiter.allow("other", now=3.0) == (True, 0.0)
    assert limiter.allow("ip", now=61.5) == (True, 0.0)
    assert limiter.remaining("ip", now=61.5) == 1
    limiter.reset()
    assert limiter.remaining("ip", now=61.5) == 3
    print("  v10 security auth/rate-limit: OK")


def test_v11_phase0_hardening():
    """Phase 0: fail-closed bind, honest exits, stale-quote and boolean guards."""
    from bettercalltrader.validate import summarize, validate_env

    # Bind guard: loopback or a token is required for a network bind.
    assert rb.dashboard_bind_error("127.0.0.1", "", False) is None
    assert rb.dashboard_bind_error("localhost", "", False) is None
    assert rb.dashboard_bind_error("::1", "", False) is None
    assert rb.dashboard_bind_error("0.0.0.0", "long-enough-token", False) is None
    assert rb.dashboard_bind_error("0.0.0.0", "", False) is not None
    assert rb.dashboard_bind_error("0.0.0.0", "", True) is None
    print("  dashboard bind guard: OK")

    from bettercalltrader.doctor import check_exposure, check_env_permissions
    assert check_exposure("0.0.0.0", "").status == "fail"
    assert check_exposure("0.0.0.0", "", allow_insecure=True).status == "warn"
    assert check_exposure("127.0.0.1", "").status == "warn"
    assert check_exposure("0.0.0.0", "long-enough-token").status == "ok"
    with tempfile.TemporaryDirectory() as tmp:
        perm_path = Path(tmp) / "perm.env"
        perm_path.write_text("X=1\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(perm_path, 0o644)
            assert check_env_permissions(perm_path).status == "warn"
            os.chmod(perm_path, 0o600)
            assert check_env_permissions(perm_path).status == "ok"
        assert check_env_permissions(Path(tmp) / "missing.env").status == "info"
    print("  doctor exposure gate: OK")

    # A quote timestamp in the future is clock skew, not a fresh quote.
    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        fresh, why = portfolio.is_market_fresh(
            {"price_usd": "1.0", "fetched_at": portfolio.now_ts() + 60}
        )
        assert fresh is False and "future" in why, (fresh, why)
        print("  negative quote age rejected: OK")

        # Every sell leg pays exit slippage. With entry slippage 0 and 800 bps
        # on exit, a flat TIME_STOP must return 92% of the position value.
        portfolio.cluster_cooldown_sec = 0
        portfolio.stop_loss_cooldown_sec = 0
        portfolio.min_seconds_between_entries = 0
        portfolio.max_daily_stop_losses = 100
        portfolio.max_daily_trades = 100
        portfolio.paper_slippage_bps = 0.0
        portfolio.paper_exit_slippage_bps = 800.0
        portfolio.paper_impact_coeff = 0.0
        # Isolate the exit-slippage assertion from the rest of the CostModel.
        portfolio.paper_amm_fee_bps = 0.0
        portfolio.paper_priority_fee_sol = 0.0
        portfolio.paper_failed_tx_prob = 0.0
        portfolio.time_stop_enabled = True
        portfolio.max_hold_no_pump_sec = 0
        portfolio.time_stop_min_profit_pct = 1_000_000.0
        now_ts = portfolio.now_ts()
        time_mint = "TimeSlipMint11111111111111111111111111111"
        time_market = {
            "mint": time_mint,
            "symbol": "TSLIP",
            "name": "Time Slip",
            "price_usd": "1.0",
            "liquidity_usd": 100000.0,
            "dex_id": "raydium",
            "fetched_at": now_ts,
        }
        assert portfolio.open_virtual_position(time_market, conviction="manual", source="TEST") is True
        action = portfolio.process_position_tick(time_mint, dict(time_market, fetched_at=portfolio.now_ts()))
        assert action == "TIME_STOP", action
        conn = portfolio._conn()
        row = conn.execute(
            "SELECT returned_sol, invested_sol, exit_reason FROM trade_history WHERE mint = ?",
            (time_mint,),
        ).fetchone()
        conn.close()
        returned, invested, reason = row
        assert reason == "TIME_STOP"
        assert abs(returned - invested * 0.92) < 1e-9, (returned, invested)
        print("  exit slippage on TIME_STOP: OK")

        # TP1 must charge the same exit slippage on the half sold.
        tp_mint = "TpSlipMint1111111111111111111111111111111"
        tp_market = dict(time_market, mint=tp_mint, symbol="TPSLIP", price_usd="1.0")
        assert portfolio.open_virtual_position(tp_market, conviction="manual", source="TEST") is True
        action = portfolio.process_position_tick(
            tp_mint, dict(tp_market, price_usd="1.8", fetched_at=portfolio.now_ts())
        )
        assert action == "TAKE_PROFIT_1", action
        conn = portfolio._conn()
        invested_tp, realized = conn.execute(
            "SELECT invested_sol, realized_sol FROM positions WHERE mint = ?", (tp_mint,)
        ).fetchone()
        conn.close()
        expected = invested_tp * 0.5 * 1.8 * 0.92  # half the position at +80%, minus 800 bps
        assert abs(realized - expected) < 1e-9, (realized, expected)
        print("  exit slippage on TP1: OK")
    finally:
        cleanup_db(db_path)

    # A typo in a boolean sell flag is a hard config failure, not silent False.
    fails, _, _ = summarize(validate_env({"FLOW_EXIT_ENABLED": "ture", "WHALE_EXIT_ENABLED": "flase"}))
    assert fails >= 2, fails
    fails, _, _ = summarize(validate_env({"FLOW_EXIT_ENABLED": "off", "WHALE_EXIT_ENABLED": "0"}))
    assert fails == 0, fails
    print("  boolean typo validation: OK")


def test_v10_observability():
    import json
    import logging
    from bettercalltrader.observability import (
        AlertThrottle, JsonFormatter, audit_event, configure_json_logging, log_event, read_audit,
    )

    record = logging.LogRecord("x", logging.INFO, __file__, 1, "test_event", None, None)
    record.fields = {"mint": "ABC", "amountSol": 0.5}
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "test_event"
    assert payload["mint"] == "ABC"

    path = str(ROOT / "_audit_test.jsonl")
    try:
        assert audit_event(path, "unit_test", value=1)["event"] == "unit_test"
        assert audit_event(path, "unit_test_2", value=2) is not None
        events = read_audit(path, limit=10)
        assert len(events) == 2
        assert events[0]["event"] == "unit_test_2"
        assert events[1]["value"] == 1
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("not json\n")
        assert len(read_audit(path, limit=10)) == 2
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    throttle = AlertThrottle(cooldown_sec=60.0)
    assert throttle.should_send("breaker", now=0.0) is True
    assert throttle.should_send("breaker", now=30.0) is False
    assert throttle.should_send("breaker", now=61.0) is True
    assert throttle.should_send("ws", now=61.0) is True
    throttle.reset()
    assert throttle.should_send("breaker", now=61.0) is True

    json_path = ROOT / "_json_log_test.jsonl"
    try:
        logger = configure_json_logging(str(json_path))
        log_event(logger, "json_event", agent="TEST", severity="info")
        for handler in list(logger.handlers):
            handler.flush()
        line = json.loads(json_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert line["event"] == "json_event" and line["agent"] == "TEST"
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        try:
            os.remove(json_path)
        except OSError:
            pass
    print("  v10 observability audit/alerts: OK")


def test_v10_strategy_profiles():
    from bettercalltrader.strategies import (
        is_known_profile,
        is_override_profile,
        normalize_profile,
        profile_values,
        resolve_profile,
    )

    assert normalize_profile("AGGRESSIVE") == "aggressive"
    assert normalize_profile("nonsense") == "balanced"
    assert profile_values("conservative")["STOP_LOSS_PCT"] == -18.0
    assert resolve_profile({}, "aggressive")["stop_loss_pct"] == -35.0
    assert "stop_loss_pct" not in resolve_profile({"STOP_LOSS_PCT": "-10"}, "aggressive")
    assert is_known_profile("high-risk") and not is_known_profile("yolo")

    # high_risk is an override profile: it wins over explicit env values, while
    # the standard profiles keep the "env always wins" contract.
    assert normalize_profile("HIGH-RISK") == "high_risk"
    assert normalize_profile("degen") == "high_risk"
    assert normalize_profile("highrisk") == "high_risk"
    assert is_override_profile("high_risk") and not is_override_profile("aggressive")
    assert profile_values("high_risk")["TRADE_SIZE_SOL"] == 1.5
    assert "trade_size_sol" not in resolve_profile({"TRADE_SIZE_SOL": "0.5"}, "high_risk")
    assert resolve_profile({"TRADE_SIZE_SOL": "0.5"}, "high_risk", force=True)["trade_size_sol"] == 1.5
    assert resolve_profile({"TRADE_SIZE_SOL": "0.5"}, "high_risk", force=True)["tp1_fraction"] == 0.35

    keys = [
        "STRATEGY_PROFILE", "STOP_LOSS_PCT", "TRADE_SIZE_SOL", "MAX_POSITION_SOL",
        "MAX_OPEN_POSITIONS", "TP1_PCT", "MAX_DAILY_TRADES",
    ]
    saved = {key: os.environ.get(key) for key in keys}
    db_path = make_temp_db()
    try:
        for key in keys:
            os.environ.pop(key, None)
        balanced = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0)
        assert balanced.strategy_profile == "balanced"
        assert balanced.stop_loss_pct == -25.0
        assert balanced.trade_size_sol == 0.5

        os.environ["STRATEGY_PROFILE"] = "aggressive"
        aggressive = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0)
        assert aggressive.stop_loss_pct == -35.0
        assert aggressive.trade_size_sol == 0.75
        assert aggressive.max_position_sol == 0.75
        assert aggressive.get_strategy_profile_info()["name"] == "aggressive"

        os.environ["STOP_LOSS_PCT"] = "-12"
        explicit = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0)
        assert explicit.stop_loss_pct == -12.0

        # high_risk forces its own sizing/exits over the pinned env values.
        os.environ["STRATEGY_PROFILE"] = "high_risk"
        os.environ["TRADE_SIZE_SOL"] = "0.5"
        os.environ["STOP_LOSS_PCT"] = "-12"
        high = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0)
        assert high.high_risk_mode
        assert high.trade_size_sol == 1.5
        assert high.stop_loss_pct == -45.0
        assert high.max_position_sol == 1.5
        assert high.max_open_positions == 4
        assert high.tp1_fraction == 0.35
        info = high.get_strategy_profile_info()
        assert info["name"] == "high_risk"
        assert info["riskMode"] == "high"
        assert info["overridesEnvironment"] is True
        # An explicit constructor argument still beats the profile.
        pinned = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.2)
        assert pinned.trade_size_sol == 0.2

        # Runtime switch from the Command Center: switching back restores the
        # pre-profile baseline instead of leaving the forced 1.5 SOL behind.
        os.environ.pop("TRADE_SIZE_SOL", None)
        os.environ.pop("STOP_LOSS_PCT", None)
        back = high.apply_strategy_profile("balanced")
        assert back["name"] == "balanced" and back["riskMode"] == "standard"
        assert high.trade_size_sol == 0.5
        assert high.stop_loss_pct == -25.0
        assert high.max_position_sol == 0.5
        assert high.tp1_fraction == 0.5

        # The /api/config payload path uses the same switch.
        changed = rb.apply_config_payload({"strategyProfile": "high_risk"}, high, None, None)
        assert "strategyProfile" in changed
        assert high.high_risk_mode and high.trade_size_sol == 1.5
        changed = rb.apply_config_payload({"strategyProfile": "degen"}, high, None, None)
        assert "strategyProfile" not in changed  # alias resolves to the active profile

        # A Command Center switch is explicit: it applies the profile numbers
        # even when .env pins those keys.
        os.environ["TRADE_SIZE_SOL"] = "0.5"
        os.environ["STOP_LOSS_PCT"] = "-25"
        rb.apply_config_payload({"strategyProfile": "aggressive"}, high, None, None)
        assert high.trade_size_sol == 0.75 and high.stop_loss_pct == -35.0
        rb.apply_config_payload({"strategyProfile": "high_risk"}, high, None, None)
        assert high._get_state("strategy_profile") == "high_risk"

        # The stored Command Center choice wins over .env on the next start.
        os.environ["STRATEGY_PROFILE"] = "balanced"
        restored = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0)
        assert restored.strategy_profile == "high_risk"
        assert restored.trade_size_sol == 1.5
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cleanup_db(db_path)
    print("  v10 strategy profiles: OK")


def test_v10_ml_advisory():
    import sqlite3
    from bettercalltrader.ml import (
        build_training_rows, load_model, model_summary, predict_mint,
        predict_probability, save_model, train_model,
    )

    db_path = make_temp_db()
    model_path = db_path + ".model.json"
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, closed_at TEXT,
                net_pnl_sol REAL, invested_sol REAL, conviction TEXT, source TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE decision_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, risk_score INTEGER,
                hype_score INTEGER, suggested_size_sol REAL, extra_json TEXT
            )"""
        )
        for i in range(30):
            mint = f"mint{i}"
            risk = 25 + (i % 10) * 7
            hype = 25 + ((i * 3) % 10) * 7
            win = risk >= 60 and hype >= 60
            conn.execute(
                "INSERT INTO trade_history (mint, closed_at, net_pnl_sol, invested_sol, conviction, source) VALUES (?,?,?,?,?,?)",
                (mint, "2026-01-01 12:00:00", 0.25 if win else -0.2, 0.5, "high", "DEX"),
            )
            conn.execute(
                "INSERT INTO decision_journal (mint, risk_score, hype_score, suggested_size_sol, extra_json) VALUES (?,?,?,?,?)",
                (mint, risk, hype, 0.5, '{"conviction":"high","source":"DEX"}'),
            )
        conn.commit()
        conn.close()

        rows = build_training_rows(db_path)
        assert len(rows) == 30
        model = train_model(db_path)
        assert model["ok"] is True
        assert model["samples"] == 30
        assert model["accuracy"] >= 0.7
        assert any(abs(w) > 0.05 for w in model["weights"])

        high = build_training_rows(db_path)
        high_features = max(high, key=lambda r: r["features"][0] + r["features"][1])["features"]
        low_features = min(high, key=lambda r: r["features"][0] + r["features"][1])["features"]
        assert predict_probability(model, high_features) > predict_probability(model, low_features)

        save_model(model, model_path)
        loaded = load_model(model_path)
        assert loaded is not None and model_summary(loaded)["trained"] is True
        assert predict_mint(db_path, "mint0", loaded) is not None
        assert predict_mint(db_path, "missing", loaded) is None
        empty_path = make_temp_db()
        try:
            assert train_model(empty_path)["ok"] is False
        finally:
            cleanup_db(empty_path)
    finally:
        for path in (model_path,):
            try:
                os.remove(path)
            except OSError:
                pass
        cleanup_db(db_path)
    print("  v10 ML advisory model: OK")


def test_v10_price_cache():
    from bettercalltrader.price_feed import PriceCache

    cache = PriceCache(max_entries=2, default_ttl_sec=3.0)
    assert cache.get("AAA", now=0.0) is None
    market = {"mint": "AAA", "price_usd": "1.0"}
    cache.update("AAA", market, now=0.0)
    market["price_usd"] = "999"
    hit = cache.get("AAA", now=1.0)
    assert hit["price_usd"] == "1.0" and hit["cache_hit"] is True
    assert cache.get("AAA", now=5.0) is None
    cache.update("AAA", {"price_usd": "1"}, now=10.0)
    cache.update("BBB", {"price_usd": "2"}, now=10.0)
    cache.update("CCC", {"price_usd": "3"}, now=10.0)
    assert cache.get("AAA", now=10.5) is None
    assert cache.get("CCC", now=10.5)["price_usd"] == "3"
    cache.invalidate("CCC")
    assert cache.get("CCC", now=10.5) is None
    stats = cache.stats()
    assert stats["entries"] == 1 and stats["misses"] >= 3
    cache.clear()
    assert cache.stats()["entries"] == 0
    print("  v10 price cache: OK")


def test_v11_prometheus():
    from bettercalltrader.prometheus import MetricsRegistry

    registry = MetricsRegistry(buckets=(0.1, 1.0))
    registry.inc("requests_total", {"method": "GET", "path": "/api/state"})
    registry.inc("requests_total", {"method": "GET", "path": "/api/state"})
    registry.set("balance_sol", 9.5, {"wallet": "paper"})
    registry.observe("latency_seconds", 0.05)
    registry.observe("latency_seconds", 0.5)
    registry.observe("latency_seconds", 2.0)
    text = registry.render()

    assert "# TYPE requests_total counter" in text
    assert 'requests_total{method="GET",path="/api/state"} 2.0' in text
    assert 'balance_sol{wallet="paper"} 9.5' in text
    assert "# TYPE latency_seconds histogram" in text
    assert 'latency_seconds_bucket{le="0.1"} 1' in text
    assert 'latency_seconds_bucket{le="1.0"} 2' in text
    assert 'latency_seconds_bucket{le="+Inf"} 3' in text
    assert 'latency_seconds_count 3' in text
    assert 'latency_seconds_sum 2.55' in text

    escaped = MetricsRegistry()
    escaped.set("gauge", 1, {"label": 'a"b\\c'})
    assert 'label="a\\"b\\\\c"' in escaped.render()
    print("  v11 prometheus: OK")


def test_v11_origin_policy():
    from bettercalltrader.security import is_origin_allowed, parse_allowed_origins

    allowed = parse_allowed_origins("https://example.com, https://app.example.com")
    assert allowed == {"https://example.com", "https://app.example.com"}
    assert is_origin_allowed("https://example.com", allowed) is True
    assert is_origin_allowed("https://evil.example.com", allowed) is False
    assert is_origin_allowed("http://127.0.0.1:8080", set()) is True
    assert is_origin_allowed("http://localhost:18080", set()) is True
    assert is_origin_allowed("null", set()) is False
    assert is_origin_allowed("https://example.com", {"*"}) is True
    assert is_origin_allowed(None, allowed) is False
    assert rb._is_protected_path("/api/state") is True
    assert rb._is_protected_path("/metrics") is True
    assert rb._is_protected_path("/ready") is True
    assert rb._is_protected_path("/analytics") is False
    print("  v11 origin policy: OK")


def test_v11_shutdown_controller():
    import asyncio
    from bettercalltrader.shutdown import ShutdownController

    async def scenario():
        controller = ShutdownController()
        assert controller.requested is False
        controller.request("SIGTERM")
        assert controller.requested is True and controller.reason == "SIGTERM"
        controller.request("SIGINT")
        assert controller.reason == "SIGTERM"
        controller.uninstall()

    asyncio.run(scenario())
    print("  v11 shutdown controller: OK")


def _synthetic_candles(closes):
    from bettercalltrader.ohlcv import Candle

    candles = []
    previous = closes[0]
    for index, close in enumerate(closes):
        open_price = previous
        high = max(open_price, close) * 1.01
        low = min(open_price, close) * 0.99
        candles.append(
            Candle(
                ts=float(index * 60),
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=1000.0 + index,
            )
        )
        previous = close
    return candles


class _RoundTripStrategy:
    name = "round_trip"

    def generate(self, history):
        if len(history) == 2:
            return "BUY"
        if len(history) == 8:
            return "SELL"
        return None


class _FirstBuyStrategy:
    name = "first_buy"

    def __init__(self):
        self.done = False

    def generate(self, history):
        if not self.done and len(history) >= 2:
            self.done = True
            return "BUY"
        return None


def test_v11_ohlcv():
    import tempfile
    from pathlib import Path
    from bettercalltrader.ohlcv import (
        BacktestConfig,
        BreakoutStrategy,
        Candle,
        EmaCrossStrategy,
        candle_from_mapping,
        load_candles_csv,
        load_candles_json,
        make_strategy,
        normalize_candles,
        run_backtest,
        walk_forward,
    )

    rising = [1.0 + i * 0.02 for i in range(20)]
    candles = _synthetic_candles(rising)

    zero_cost = BacktestConfig(
        initial_sol=10.0,
        trade_size_sol=0.5,
        fee_bps=0.0,
        slippage_bps=0.0,
        latency_candles=1,
        stop_loss_pct=0.0,
        take_profit_pct=0.0,
        trailing_stop_pct=0.0,
    )
    result = run_backtest(candles, _RoundTripStrategy(), zero_cost)
    assert len(result.trades) == 1, result.trades
    assert result.trades[0]["exitReason"] == "SIGNAL"
    assert result.trades[0]["pnlSol"] > 0
    assert result.summary["trades"] == 1
    assert result.summary["finalEquitySol"] > 10.0
    assert result.summary["exposurePct"] > 0

    costly = BacktestConfig(
        initial_sol=10.0,
        trade_size_sol=0.5,
        fee_bps=100.0,
        slippage_bps=300.0,
        latency_candles=1,
        stop_loss_pct=0.0,
        take_profit_pct=0.0,
        trailing_stop_pct=0.0,
    )
    costly_result = run_backtest(candles, _RoundTripStrategy(), costly)
    assert costly_result.trades[0]["pnlSol"] < result.trades[0]["pnlSol"]
    assert costly_result.summary["feesPaidSol"] > 0

    stop_config = BacktestConfig(
        initial_sol=10.0,
        trade_size_sol=0.5,
        fee_bps=0.0,
        slippage_bps=0.0,
        latency_candles=1,
        stop_loss_pct=-10.0,
        take_profit_pct=0.0,
        trailing_stop_pct=0.0,
    )
    stop_candles = _synthetic_candles([1.0] * 10 + [0.85, 0.7])
    stop_result = run_backtest(stop_candles, _FirstBuyStrategy(), stop_config)
    assert stop_result.trades[0]["exitReason"] == "STOP_LOSS"
    assert stop_result.trades[0]["pnlSol"] < 0

    trend = _synthetic_candles([1.0] * 10 + [1.0 + i * 0.03 for i in range(1, 31)] + [1.9 - i * 0.03 for i in range(1, 31)])
    momentum = run_backtest(trend, EmaCrossStrategy(fast=3, slow=8), zero_cost)
    assert momentum.trades, "momentum strategy should trade on a clear trend"
    assert momentum.summary["maxDrawdownPct"] >= 0

    breakout = BreakoutStrategy(lookback=5, threshold_pct=0.1)
    assert make_strategy("breakout", lookback=5).name == "breakout"
    assert make_strategy("ema", fast=3, slow=8).name == "momentum"
    assert breakout.generate(trend[:10]) in (None, "BUY", "SELL")

    # Input handling
    mapped = candle_from_mapping(
        {"time": "2026-01-01T00:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}
    )
    assert mapped.ts > 0 and mapped.high == 2.0
    try:
        candle_from_mapping({"ts": 1, "open": 1, "high": 0.5, "low": 1, "close": 1})
        raise AssertionError("invalid candle should fail")
    except ValueError:
        pass

    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "candles.csv"
        csv_path.write_text(
            "ts,open,high,low,close,volume\n"
            "2026-01-01T00:00:00Z,1.0,1.1,0.9,1.05,100\n"
            "2026-01-01T00:01:00Z,1.05,1.2,1.0,1.15,120\n",
            encoding="utf-8",
        )
        loaded = load_candles_csv(str(csv_path))
        assert len(loaded) == 2 and loaded[1].close == 1.15

        json_path = Path(tmp) / "candles.json"
        json_path.write_text(
            '{"candles": [{"t": 1, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 4}]}',
            encoding="utf-8",
        )
        try:
            load_candles_json(str(json_path))
            raise AssertionError("unknown keys should fail")
        except ValueError:
            pass

        good_json = Path(tmp) / "good.json"
        good_json.write_text(
            '[[60, 1.0, 1.2, 0.9, 1.1, 10], [120, 1.1, 1.3, 1.0, 1.2, 11]]',
            encoding="utf-8",
        )
        assert len(load_candles_json(str(good_json))) == 2

    duplicate = normalize_candles([Candle(1, 1, 1, 1, 1), Candle(1, 2, 2, 2, 2)])
    assert len(duplicate) == 1 and duplicate[0].close == 2

    wf = walk_forward(
        _synthetic_candles([1.0 + i * 0.01 for i in range(100)] + [2.0 - i * 0.01 for i in range(100)]),
        lambda: EmaCrossStrategy(fast=3, slow=8),
        zero_cost,
        segments=2,
        train_ratio=0.5,
    )
    assert wf["aggregate"]["segments"] == 2
    assert len(wf["segments"]) == 2
    print("  v11 OHLCV backtest: OK")


def test_v11_config_validation():
    from bettercalltrader.validate import summarize, validate_env

    safe = {
        "LIVE_TRADING_ENABLED": "false",
        "INITIAL_PAPER_SOL": "10",
        "TRADE_SIZE_SOL": "0.5",
        "STOP_LOSS_PCT": "-25",
        "TP1_PCT": "80",
        "MAX_OPEN_POSITIONS": "2",
        "MAX_DAILY_DRAWDOWN_PCT": "10",
        "STRATEGY_PROFILE": "balanced",
        "DASHBOARD_TOKEN": "unit-test-token-123",
    }
    fails, warns, _ = summarize(validate_env(safe))
    assert fails == 0, validate_env(safe)
    assert warns == 0, validate_env(safe)

    live_unconfirmed = dict(safe, LIVE_TRADING_ENABLED="true")
    fails, _, _ = summarize(validate_env(live_unconfirmed))
    assert fails >= 2  # not confirmed + missing private key

    live_ok = dict(
        live_unconfirmed,
        LIVE_TRADING_CONFIRMED="true",
        SOLANA_PRIVATE_KEY="fake-test-key",
        MAX_LIVE_TRADE_SOL="0.05",
    )
    fails, warns, _ = summarize(validate_env(live_ok))
    assert fails == 0
    assert any("live trading is ON" in issue.message for issue in validate_env(live_ok))

    bad_numbers = dict(
        safe,
        STOP_LOSS_PCT="5",
        TRADE_SIZE_SOL="50",
        MAX_OPEN_POSITIONS="0",
        API_RATE_LIMIT_PER_MIN="nope",
    )
    fails, _, _ = summarize(validate_env(bad_numbers))
    assert fails >= 4

    optional_warnings = dict(safe, DASHBOARD_TOKEN="", CORS_ALLOW_ALL="true", STRATEGY_PROFILE="yolo")
    fails, warns, _ = summarize(validate_env(optional_warnings))
    assert fails == 0 and warns >= 3

    high_risk = dict(safe, STRATEGY_PROFILE="high_risk")
    high_risk_issues = validate_env(high_risk)
    fails, warns, _ = summarize(high_risk_issues)
    assert fails == 0
    assert any("high_risk mode" in issue.message for issue in high_risk_issues if issue.level == "warn")
    # A recognised alias must not be reported as an unknown profile.
    alias_issues = validate_env(dict(safe, STRATEGY_PROFILE="degen"))
    assert not any("unknown profile" in issue.message for issue in alias_issues)

    unknown_flag = dict(safe, LIVE_TRADING_ENABLED="maybe")
    fails, _, _ = summarize(validate_env(unknown_flag))
    assert fails == 1

    curve_bad = dict(safe, PUMPFUN_CURVE_ENABLED="maybe")
    fails, _, _ = summarize(validate_env(curve_bad))
    assert fails == 1

    recovery_bad = dict(safe, ENTRY_CONFIRM_RECOVERY_MAX_SEC="-1", PUMPFUN_CURVE_CACHE_SEC="-1")
    fails, _, _ = summarize(validate_env(recovery_bad))
    assert fails >= 2
    print("  v11 config validation: OK")


def test_v11_doctor():
    import tempfile
    from pathlib import Path
    from bettercalltrader.doctor import load_env_file, run_checks
    from bettercalltrader.envfile import describe_ignored_lines, parse_env_file

    env = {
        "LIVE_TRADING_ENABLED": "false",
        "INITIAL_PAPER_SOL": "10",
        "TRADE_SIZE_SOL": "0.5",
        "STOP_LOSS_PCT": "-25",
        "DASHBOARD_TOKEN": "smoke-token-1234",
        "COMMAND_CENTER_HOST": "127.0.0.1",
    }
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        checks = run_checks(
            env,
            db_path=tmp_path / "doctor.db",
            audit_path=tmp_path / "doctor_audit.jsonl",
            check_socket=False,
        )
        names = {check.name: check for check in checks}
        assert names["python"].status == "ok"
        assert names["database"].status == "ok"
        assert names["audit_log"].status == "ok"
        assert names["config"].status == "ok"
        assert names["dashboard_auth"].status == "ok"
        assert not any(check.status == "fail" for check in checks), checks

        assignments = tmp_path / ".env"
        assignments.write_text(
            "# comment\nDASHBOARD_TOKEN=abc123\nEMPTY=\nQUOTED='x y'\n", encoding="utf-8"
        )
        loaded = load_env_file(assignments)
        assert loaded["DASHBOARD_TOKEN"] == "abc123"
        assert loaded.get("QUOTED") == "x y"

        # Documentation separators are ignored quietly, but reported once by
        # doctor with line numbers instead of one logger line per row.
        malformed = tmp_path / "malformed.env"
        malformed.write_text(
            "=====\n1. HEADING\nDASHBOARD_TOKEN=abc123\n", encoding="utf-8"
        )
        parsed = parse_env_file(malformed)
        assert parsed.values == {"DASHBOARD_TOKEN": "abc123"}
        assert parsed.ignored_lines == [1, 2]
        assert describe_ignored_lines(parsed) == "line(s) 1, 2"
        assert load_env_file(malformed)["DASHBOARD_TOKEN"] == "abc123"

        reported = run_checks(
            {},
            env_file=malformed,
            db_path=tmp_path / "parse.db",
            audit_path=tmp_path / "parse_audit.jsonl",
            check_socket=False,
        )
        reported_names = {check.name: check for check in reported}
        assert reported_names["env_parse"].status == "warn"
        assert "line(s) 1, 2" in reported_names["env_parse"].detail

        missing = run_checks(
            {},
            env_file=tmp_path / "missing.env",
            db_path=tmp_path / "other.db",
            audit_path=tmp_path / "audit2.jsonl",
            check_socket=False,
        )
        assert any(check.name == "env_file" and check.status == "warn" for check in missing)
    print("  v11 doctor: OK")


def test_v11_extracted_helpers():
    from datetime import datetime

    from bettercalltrader import fills, timeutils

    assert rb.paper_fill is fills.paper_fill
    assert rb.exit_icon is fills.exit_icon
    assert rb.utc_now is timeutils.utc_now
    assert rb.fmt_utc is timeutils.fmt_utc
    assert rb.LOSING_EXIT_REASONS == ("STOP_LOSS", "FAST_INVALIDATION", "FLOW_EXIT", "WHALE_EXIT", "TIME_STOP")

    now = timeutils.utc_now()
    assert now.tzinfo is None
    assert timeutils.fmt_utc(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02 03:04:05"

    # Winter (CET) and summer (CEST) local days; both are deterministic whether
    # zoneinfo data is present or the CET/CEST fallback is used.
    day, start, end = timeutils.local_day_bounds(datetime(2026, 1, 15, 12, 0, 0))
    assert day == "2026-01-15"
    assert timeutils.fmt_utc(start) == "2026-01-14 23:00:00"
    assert timeutils.fmt_utc(end) == "2026-01-15 23:00:00"

    day, start, _ = timeutils.local_day_bounds(datetime(2026, 7, 1, 12, 0, 0))
    assert day == "2026-07-01"
    assert timeutils.fmt_utc(start) == "2026-06-30 22:00:00"

    local = timeutils.fmt_local(datetime(2026, 1, 15, 12, 0, 0))
    assert local and local.startswith("2026-01-15 13:00:00")
    assert timeutils.fmt_local(None) is None

    fill, slip = fills.paper_fill(1.0, "BUY", 0.5, 100_000.0, 50.0, 1.0, 185.0)
    assert fill > 1.0 and slip > 0
    try:
        fills.paper_fill(1.0, "BUY", 1.0, None, 0.0, 0.0)
    except Exception:
        raise AssertionError("paper_fill should tolerate missing liquidity")
    print("  v11 extracted helpers: OK")


def test_v11_cost_model():
    from bettercalltrader.costs import COST_MODEL_VERSION, CostModel, replay_round_trip
    from bettercalltrader.defaults import COST_DEFAULTS, STRATEGY_DEFAULTS, config_hash

    zero = CostModel(base_bps=0, amm_fee_bps=0, impact_coeff=0, priority_fee_sol=0, failed_tx_prob=0)
    buy = zero.entry(2.0, 1.0)
    assert buy.qty == 0.5 and buy.price == 2.0 and buy.total_cost_sol == 0.0
    sell = zero.exit_value(1.0, 2.0)
    assert sell.qty == 0.5 and sell.net_sol == 1.0 and sell.price == 2.0

    spread = CostModel(base_bps=100, amm_fee_bps=0, impact_coeff=0, priority_fee_sol=0, failed_tx_prob=0)
    buy = spread.entry(1.0, 1.0)
    assert abs(buy.slippage_fraction - 0.01) < 1e-12
    assert abs(buy.price - 1.01) < 1e-12
    assert abs(buy.total_cost_fraction - 0.01) < 1e-12
    sell = spread.exit_value(1.0, 1.0)
    assert abs(sell.net_sol - 0.99) < 1e-12 and abs(sell.price - 0.99) < 1e-12

    amm = CostModel(base_bps=0, amm_fee_bps=100, impact_coeff=0, priority_fee_sol=0, failed_tx_prob=0)
    buy = amm.entry(1.0, 1.0)
    assert abs(buy.price - 1.01) < 1e-12
    assert abs(buy.amm_fee_sol - (1.0 - 1.0 / 1.01)) < 1e-12
    sell = amm.exit_value(1.0, 1.0)
    assert abs(sell.net_sol - 0.99) < 1e-12

    flat = CostModel(base_bps=0, amm_fee_bps=0, impact_coeff=0, priority_fee_sol=0.01, failed_tx_prob=0)
    buy = flat.entry(1.0, 1.0)
    assert abs(buy.qty - 0.99) < 1e-12
    assert abs(buy.price - (1.0 / 0.99)) < 1e-12
    sell = flat.exit_value(1.0, 1.0)
    assert abs(sell.net_sol - 0.99) < 1e-12

    flaky = CostModel(base_bps=0, amm_fee_bps=0, impact_coeff=0, priority_fee_sol=0.01, failed_tx_prob=0.5)
    buy = flaky.entry(1.0, 1.0)
    assert abs(buy.qty - 0.985) < 1e-12  # 0.01 priority + 0.005 expected retry

    impact = CostModel(base_bps=0, amm_fee_bps=0, impact_coeff=1, priority_fee_sol=0, failed_tx_prob=0)
    assert impact.slippage_fraction(10.0, 10_000.0) > 0
    assert abs(impact.slippage_fraction(1e9, 1.0) - 0.5) < 1e-12

    for kwargs in ({"base_bps": -1}, {"failed_tx_prob": 1.5}, {"max_slippage": 0.0}):
        try:
            CostModel(**kwargs)
            raise AssertionError(f"CostModel should reject {kwargs}")
        except ValueError:
            pass

    round_trip = replay_round_trip(spread, spread, 1.0, 1.5, 2.0)
    assert abs(round_trip["pnlSol"] - (round_trip["returnedSol"] - 2.0)) < 1e-12
    assert round_trip["entryCostSol"] > 0 and round_trip["exitCostSol"] > 0
    assert round_trip["exitPrice"] < 1.5

    assert COST_MODEL_VERSION == "2"
    assert spread.as_dict()["version"] == COST_MODEL_VERSION
    assert COST_DEFAULTS["amm_fee_bps"] == 25.0
    assert config_hash({"a": 80.0}) == config_hash({"a": 80})
    assert config_hash({"a": 80.0}) != config_hash({"a": 81.0})
    print("  v11 shared cost model: OK")


def _ohlc_candles(rows):
    from bettercalltrader.ohlcv import Candle

    return [
        Candle(ts=float(index * 60), open=open_, high=high, low=low, close=close, volume=1.0)
        for index, (open_, high, low, close) in enumerate(rows)
    ]


def test_v11_backtest_exits():
    from bettercalltrader.costs import COST_MODEL_VERSION
    from bettercalltrader.ohlcv import BacktestConfig, run_backtest

    def free_config(**overrides):
        values = dict(
            initial_sol=10.0,
            trade_size_sol=0.5,
            fee_bps=0.0,
            slippage_bps=0.0,
            exit_slippage_bps=0.0,
            impact_coeff=0.0,
            liquidity_usd=0.0,
            priority_fee_sol=0.0,
            failed_tx_prob=0.0,
            latency_candles=1,
            stop_loss_pct=0.0,
            take_profit_pct=0.0,
            trailing_stop_pct=0.0,
        )
        values.update(overrides)
        return BacktestConfig(**values)

    # Entry candle 2 has a low below the stop, but the position was opened at
    # its open and must not be stopped by its own bar. Candle 4 gaps through.
    gap_stop = _ohlc_candles([
        (1.0, 1.0, 1.0, 1.0),
        (1.0, 1.0, 1.0, 1.0),
        (1.0, 1.0, 0.80, 1.0),
        (1.0, 1.0, 1.0, 1.0),
        (0.8, 0.85, 0.75, 0.8),
    ])
    result = run_backtest(gap_stop, _FirstBuyStrategy(), free_config(stop_loss_pct=-10.0))
    assert len(result.trades) == 1, result.trades
    trade = result.trades[0]
    assert trade["entryIndex"] == 2 and trade["exitIndex"] == 4
    assert trade["exitReason"] == "STOP_LOSS"
    assert trade["exitMidPrice"] == 0.8
    assert abs(trade["pnlSol"] - (0.5 * 0.8 - 0.5)) < 1e-12
    assert trade["costSol"] == 0.0
    assert result.summary["configHash"] == free_config(stop_loss_pct=-10.0).fingerprint(_FirstBuyStrategy())
    assert result.summary["costModelVersion"] == COST_MODEL_VERSION

    # Take profit fills at max(open, tp) when the candle gaps above it.
    gap_tp = _ohlc_candles([
        (1.0, 1.0, 1.0, 1.0),
        (1.0, 1.0, 1.0, 1.0),
        (1.0, 1.05, 1.0, 1.0),
        (1.0, 1.0, 1.0, 1.0),
        (2.0, 2.2, 1.9, 2.0),
    ])
    result = run_backtest(
        gap_tp, _FirstBuyStrategy(), free_config(take_profit_pct=50.0, tp1_fraction=1.0)
    )
    trade = result.trades[0]
    assert trade["exitReason"] == "TAKE_PROFIT_1"
    assert trade["exitMidPrice"] == 2.0
    assert abs(trade["pnlSol"] - (0.5 * 2.0 - 0.5)) < 1e-12
    assert trade["ambiguousBar"] is False

    # A candle that makes a new high must not arm and trigger its own trailing
    # stop; the next candle does.
    trailing = _ohlc_candles([
        (1.0, 1.0, 1.0, 1.0),
        (1.0, 1.0, 1.0, 1.0),
        (1.0, 1.0, 0.8, 1.0),
        (1.0, 2.0, 0.95, 2.0),
        (2.0, 2.0, 0.85, 1.0),
    ])
    result = run_backtest(trailing, _FirstBuyStrategy(), free_config(trailing_stop_pct=10.0))
    trade = result.trades[0]
    assert trade["exitIndex"] == 4 and trade["exitReason"] == "TRAILING_STOP"
    assert trade["exitMidPrice"] == 1.8

    # Stop and take profit inside one candle: take the conservative stop and
    # flag the ambiguity.
    ambiguous = _ohlc_candles([
        (1.0, 1.0, 1.0, 1.0),
        (1.0, 1.0, 1.0, 1.0),
        (1.0, 1.0, 1.0, 1.0),
        (1.0, 1.3, 0.85, 1.0),
    ])
    result = run_backtest(
        ambiguous, _FirstBuyStrategy(), free_config(stop_loss_pct=-10.0, take_profit_pct=20.0)
    )
    trade = result.trades[0]
    assert trade["exitReason"] == "STOP_LOSS"
    assert trade["exitMidPrice"] == 0.9
    assert trade["ambiguousBar"] is True
    assert result.summary["ambiguousBars"] == 1
    print("  v11 backtest exit fixes: OK")


def test_v11_cost_parity():
    from bettercalltrader.costs import replay_round_trip
    from bettercalltrader.ohlcv import BacktestConfig, run_backtest

    cfg = BacktestConfig(
        initial_sol=10.0,
        trade_size_sol=0.5,
        fee_bps=25.0,
        slippage_bps=50.0,
        exit_slippage_bps=80.0,
        impact_coeff=1.0,
        liquidity_usd=50_000.0,
        priority_fee_sol=0.0015,
        failed_tx_prob=0.02,
        latency_candles=1,
        stop_loss_pct=0.0,
        take_profit_pct=0.0,
        trailing_stop_pct=0.0,
    )
    candles = _synthetic_candles([1.0 + index * 0.02 for index in range(20)])
    result = run_backtest(candles, _RoundTripStrategy(), cfg)
    assert len(result.trades) == 1
    trade = result.trades[0]
    expected = replay_round_trip(
        cfg.entry_model(),
        cfg.exit_model(),
        candles[trade["entryIndex"]].open,
        trade["exitMidPrice"],
        cfg.trade_size_sol,
        cfg.liquidity(),
    )
    for key in ("entryPrice", "exitPrice", "investedSol", "returnedSol", "pnlSol"):
        assert abs(float(trade[key]) - float(expected[key])) < 1e-12, (key, trade[key], expected[key])
    assert abs(trade["costSol"] - (expected["entryCostSol"] + expected["exitCostSol"])) < 1e-12

    # The paper book must price the very same round trip identically.
    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        portfolio.cluster_cooldown_sec = 0
        portfolio.stop_loss_cooldown_sec = 0
        portfolio.min_seconds_between_entries = 0
        portfolio.max_daily_stop_losses = 100
        portfolio.max_daily_trades = 100
        portfolio.paper_slippage_bps = cfg.slippage_bps
        portfolio.paper_exit_slippage_bps = cfg.exit_slippage_bps
        portfolio.paper_impact_coeff = cfg.impact_coeff
        portfolio.paper_amm_fee_bps = cfg.fee_bps
        portfolio.paper_priority_fee_sol = cfg.priority_fee_sol
        portfolio.paper_failed_tx_prob = cfg.failed_tx_prob
        portfolio.paper_sol_price_usd = cfg.sol_price_usd

        mint = "ParityMint11111111111111111111111111111111"
        market = {
            "mint": mint,
            "symbol": "PARITY",
            "name": "Parity",
            "price_usd": "1.0",
            "liquidity_usd": cfg.liquidity_usd,
            "dex_id": "raydium",
            "fetched_at": portfolio.now_ts(),
        }
        assert portfolio.open_virtual_position(
            market, conviction="manual", source="TEST", size_override=cfg.trade_size_sol
        ) is True
        exit_mid = 1.5
        conn = portfolio._conn()
        conn.execute("UPDATE positions SET current_price_usd = ? WHERE mint = ?", (exit_mid, mint))
        conn.commit()
        conn.close()
        assert portfolio.close_position_manually(mint) is True
        conn = portfolio._conn()
        row = conn.execute(
            "SELECT invested_sol, returned_sol, entry_fill_usd, exit_fill_usd FROM trade_history WHERE mint = ?",
            (mint,),
        ).fetchone()
        conn.close()
        invested, returned, entry_fill, exit_fill = row
        paper_expected = replay_round_trip(
            portfolio._cost_model("BUY"),
            portfolio._cost_model("SELL"),
            1.0,
            exit_mid,
            cfg.trade_size_sol,
            cfg.liquidity(),
        )
        assert abs(invested - cfg.trade_size_sol) < 1e-12
        assert abs(returned - paper_expected["returnedSol"]) < 1e-9
        assert abs(entry_fill - paper_expected["entryPrice"]) < 1e-12
        assert abs(exit_fill - paper_expected["exitPrice"]) < 1e-12
    finally:
        cleanup_db(db_path)
    print("  v11 paper/OHLCV cost parity: OK")


def test_v11_config_fingerprint():
    from bettercalltrader.defaults import COST_DEFAULTS, STRATEGY_DEFAULTS
    from bettercalltrader.ohlcv import BacktestConfig
    from bettercalltrader.strategies import PROFILES
    from bettercalltrader.validate import validate_env

    assert PROFILES["balanced"]["STOP_LOSS_PCT"] == STRATEGY_DEFAULTS["STOP_LOSS_PCT"]
    assert PROFILES["balanced"]["TP1_PCT"] == STRATEGY_DEFAULTS["TP1_PCT"]
    assert COST_DEFAULTS["amm_fee_bps"] == 25.0
    assert BacktestConfig().fingerprint() == BacktestConfig().fingerprint()
    assert BacktestConfig().fingerprint() != BacktestConfig(fee_bps=100.0).fingerprint()
    from bettercalltrader.ohlcv import BreakoutStrategy, EmaCrossStrategy

    first = BacktestConfig().fingerprint(EmaCrossStrategy(fast=3, slow=8))
    assert first == BacktestConfig().fingerprint(EmaCrossStrategy(fast=3, slow=8))
    assert first != BacktestConfig().fingerprint(BreakoutStrategy(lookback=5))
    assert first != BacktestConfig().fingerprint(EmaCrossStrategy(fast=4, slow=8))

    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        before = portfolio.config_hash
        assert len(before) == 12
        assert portfolio._cost_model("BUY").base_bps == portfolio.paper_slippage_bps
        assert portfolio._cost_model("SELL").base_bps == portfolio.paper_exit_slippage_bps
        portfolio.stop_loss_pct = -10.0
        assert portfolio.refresh_config_hash() != before
    finally:
        cleanup_db(db_path)

    issues = validate_env({"PAPER_FAILED_TX_PROB": "1.5"})
    assert any(issue.key == "PAPER_FAILED_TX_PROB" and issue.level == "fail" for issue in issues)
    issues = validate_env({"PAPER_AMM_FEE_BPS": "-1"})
    assert any(issue.key == "PAPER_AMM_FEE_BPS" and issue.level == "fail" for issue in issues)
    issues = validate_env({"PAPER_PRIORITY_FEE_SOL": "-0.1"})
    assert any(issue.key == "PAPER_PRIORITY_FEE_SOL" and issue.level == "fail" for issue in issues)
    issues = validate_env({"PAPER_IMPACT_EXPONENT": "0"})
    assert any(issue.key == "PAPER_IMPACT_EXPONENT" and issue.level == "fail" for issue in issues)
    issues = validate_env({"TP1_FRACTION": "1.5"})
    assert any(issue.key == "TP1_FRACTION" and issue.level == "fail" for issue in issues)
    issues = validate_env({"LLM_PROVIDER": "wat"})
    assert any(issue.key == "LLM_PROVIDER" and issue.level == "fail" for issue in issues)
    issues = validate_env({"LLM_BASE_URL": "ftp://example.com"})
    assert any(issue.key == "LLM_BASE_URL" and issue.level == "fail" for issue in issues)
    print("  v11 config fingerprint and cost validation: OK")


def test_v11_partial_tp1():
    from bettercalltrader.ohlcv import BacktestConfig, run_backtest

    cfg = BacktestConfig(
        initial_sol=10.0,
        trade_size_sol=0.5,
        fee_bps=0.0,
        slippage_bps=0.0,
        exit_slippage_bps=0.0,
        impact_coeff=0.0,
        priority_fee_sol=0.0,
        failed_tx_prob=0.0,
        latency_candles=1,
        stop_loss_pct=-25.0,
        take_profit_pct=20.0,
        trailing_stop_pct=0.0,
        tp1_fraction=0.5,
    )
    candles = _ohlc_candles([
        (1.0, 1.0, 1.0, 1.0),
        (1.0, 1.0, 1.0, 1.0),
        (1.0, 1.05, 0.95, 1.0),  # entry at the open
        (1.0, 1.30, 1.00, 1.25),  # TP1: sell half at 1.2
        (0.70, 0.72, 0.60, 0.70),  # remainder stops out at the open
    ])
    result = run_backtest(candles, _FirstBuyStrategy(), cfg)
    assert len(result.trades) == 1, result.trades
    trade = result.trades[0]
    assert trade["tp1Hit"] is True
    assert trade["exitIndex"] == 4 and trade["legs"] == 2
    assert trade["exitReason"] == "TRAILING_STOP"
    assert abs(trade["tp1Price"] - 1.2) < 1e-12
    assert abs(trade["tp1ProceedsSol"] - 0.3) < 1e-12
    # Half sold at 1.2, half at the gapped stop (0.7), on a 0.5 SOL budget.
    assert abs(trade["returnedSol"] - (0.25 * 1.2 + 0.25 * 0.7)) < 1e-12
    assert abs(trade["pnlSol"] - -0.025) < 1e-12
    assert result.summary["tp1Exits"] == 1
    print("  v11 partial TP1: OK")


def test_v11_depth_and_participation():
    from bettercalltrader.costs import CostModel, depth_used_fraction, replay_round_trip
    from bettercalltrader.ohlcv import BacktestConfig, Candle, run_backtest

    model = CostModel(
        base_bps=0.0,
        amm_fee_bps=0.0,
        impact_coeff=1.0,
        priority_fee_sol=0.0,
        failed_tx_prob=0.0,
        sol_price_usd=185.0,
        max_slippage=0.9,
    )
    used = depth_used_fraction(5.0, 10_000.0, 185.0)
    assert 0.0 < used < 0.95
    shallow = replay_round_trip(model, model, 1.0, 1.0, 5.0, 10_000.0, depth_decay=False)
    deep = replay_round_trip(model, model, 1.0, 1.0, 5.0, 10_000.0, depth_decay=True)
    assert deep["depthUsedFraction"] > 0
    assert deep["exitCostSol"] > shallow["exitCostSol"]
    assert deep["returnedSol"] < shallow["returnedSol"]

    # A steeper exponent prices the same depth more aggressively.
    linear = model.impact_fraction(5.0, 10_000.0)
    quad = CostModel(
        base_bps=0.0, amm_fee_bps=0.0, impact_coeff=1.0, priority_fee_sol=0.0,
        failed_tx_prob=0.0, sol_price_usd=185.0, impact_exponent=0.5,
    ).impact_fraction(5.0, 10_000.0)
    assert quad > linear and linear > 0

    # A thin candle cannot fill the full requested size.
    candles = [
        Candle(ts=float(index * 60), open=1.0, high=1.01, low=0.99, close=1.0, volume=100.0)
        for index in range(8)
    ]
    cfg = BacktestConfig(
        initial_sol=10.0,
        trade_size_sol=0.5,
        fee_bps=0.0,
        slippage_bps=0.0,
        exit_slippage_bps=0.0,
        impact_coeff=0.0,
        priority_fee_sol=0.0,
        failed_tx_prob=0.0,
        latency_candles=1,
        stop_loss_pct=0.0,
        take_profit_pct=0.0,
        trailing_stop_pct=0.0,
        max_participation_pct=0.1,
    )
    result = run_backtest(candles, _FirstBuyStrategy(), cfg)
    trade = result.trades[0]
    capacity = (100.0 * 0.1) / 185.0
    assert trade["requestedSol"] == 0.5
    assert abs(trade["investedSol"] - capacity) < 1e-12
    assert 0.0 < trade["filledFraction"] < 1.0
    assert result.summary["partialEntries"] == 1
    assert result.summary["requestedEntries"] == 1
    print("  v11 depth decay and participation cap: OK")


def test_v11_exit_policy_parity():
    from bettercalltrader.exits import (
        BREAKEVEN_STOP, FAST_INVALIDATION, FLOW_EXIT, STOP_LOSS,
        TAKE_PROFIT_1, TIME_STOP, TRAILING_STOP, WHALE_EXIT, ExitPolicy,
    )

    policy = ExitPolicy()
    # TP1 is partial by default.
    intent = policy.classify_tick(
        entry_price=1.0, current_price=1.9, highest_price=1.9, tp1_hit=False, age_sec=600
    )
    assert intent.reason == TAKE_PROFIT_1 and intent.fraction == 0.5

    # Same tick through the bar resolver: reason and fraction must agree.
    bar = policy.intrabar(
        entry_price=1.0, peak_price=1.0, tp1_hit=False,
        open_price=1.0, high=1.0, low=1.0, close=1.9, age_sec=600,
    )
    assert bar is None or bar.reason == TAKE_PROFIT_1

    assert policy.classify_tick(
        entry_price=1.0, current_price=0.7, highest_price=1.0, tp1_hit=False, age_sec=600
    ).reason == STOP_LOSS
    assert policy.classify_tick(
        entry_price=1.0, current_price=0.7, highest_price=1.0, tp1_hit=False, age_sec=10
    ).reason == FAST_INVALIDATION
    assert policy.classify_tick(
        entry_price=1.0, current_price=1.02, highest_price=1.0, tp1_hit=False, age_sec=10,
        whale_selling={"label": "w"},
    ).reason == WHALE_EXIT
    assert policy.classify_tick(
        entry_price=1.0, current_price=1.0, highest_price=1.0, tp1_hit=False, age_sec=10,
        flow={"sells": 5, "buys": 1},
    ).reason == FLOW_EXIT
    assert policy.classify_tick(
        entry_price=1.0, current_price=1.0, highest_price=1.0, tp1_hit=False, age_sec=1000
    ).reason == TIME_STOP

    # Trailing: a new high arms it, then a pullback through the trail exits.
    trail = ExitPolicy(trailing_arm_gain_pct=10.0, trailing_stop_pct=20.0)
    assert trail.classify_tick(
        entry_price=1.0, current_price=1.15, highest_price=1.5, tp1_hit=False, age_sec=600
    ).reason == TRAILING_STOP
    bar = trail.intrabar(
        entry_price=1.0, peak_price=1.5, tp1_hit=False,
        open_price=1.4, high=1.4, low=1.2, close=1.3, age_sec=600,
    )
    assert bar is not None and bar.reason == TRAILING_STOP and abs(bar.price - 1.2) < 1e-12

    # Breakeven floor after TP1, in both paths.
    moon = ExitPolicy(move_stop_to_breakeven_after_tp1=True, breakeven_buffer_pct=1.0)
    assert moon.classify_tick(
        entry_price=1.0, current_price=1.0, highest_price=1.2, tp1_hit=True, age_sec=600
    ).reason == BREAKEVEN_STOP
    bar = moon.intrabar(
        entry_price=1.0, peak_price=1.2, tp1_hit=True,
        open_price=1.0, high=1.0, low=0.99, close=1.0, age_sec=600,
    )
    assert bar is not None and bar.reason == BREAKEVEN_STOP

    # intrabar excludes the plain stop when it is disabled.
    assert ExitPolicy(stop_loss_pct=0.0, tp1_pct=0.0).intrabar(
        entry_price=1.0, peak_price=1.0, tp1_hit=False,
        open_price=0.5, high=1.0, low=0.5, close=0.5, age_sec=600,
    ) is None
    print("  v11 shared exit policy parity: OK")


def test_v11_llm_layer():
    import asyncio

    from aiohttp import web

    from bettercalltrader.llm import LLMClient, LLMConfig, LLMError, extract_json_object, probe

    gemini = LLMConfig.from_env({"GEMINI_API_KEY": "g"})
    assert gemini.providers()[0].kind == "gemini"
    local = LLMConfig.from_env({"LLM_BASE_URL": "http://127.0.0.1:9/v1", "LLM_MODEL": "tiny"})
    assert local.providers()[0].kind == "openai"
    assert local.providers()[0].model == "tiny"
    assert LLMConfig.from_env({"LLM_PROVIDER": "none", "GEMINI_API_KEY": "g"}).providers() == []
    assert LLMConfig.from_env(
        {"LLM_PROVIDER": "local", "LLM_BASE_URL": "http://127.0.0.1:9/v1", "LLM_MODEL": "x"}
    ).provider == "openai"
    chain = LLMConfig.from_env(
        {
            "LLM_PROVIDER": "openai",
            "LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "LLM_MODEL": "x",
            "GEMINI_API_KEY": "g",
        }
    ).providers()
    assert [spec.kind for spec in chain] == ["openai", "gemini"]

    # LLM_MODELS adds ordered local fallbacks; LLM_MODEL stays first.
    multi = LLMConfig.from_env(
        {
            "LLM_PROVIDER": "openai",
            "LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "LLM_MODEL": "primary",
            "LLM_MODELS": " fallback-a , fallback-b, primary ",
        }
    )
    assert multi.local_models == ("primary", "fallback-a", "fallback-b")
    assert [spec.model for spec in multi.providers()] == ["primary", "fallback-a", "fallback-b"]
    list_only = LLMConfig.from_env({"LLM_BASE_URL": "http://127.0.0.1:9/v1", "LLM_MODELS": "x,y"})
    assert [spec.model for spec in list_only.providers()] == ["x", "y"]

    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('Here you go: {"b": 2} — done') == {"b": 2}
    try:
        extract_json_object("definitely not json")
        raise AssertionError("non-JSON should raise")
    except LLMError:
        pass

    async def run():
        lm_mode = {"lmstudio": False}

        async def chat(request):
            body = await request.json()
            assert body["model"] == "tiny"
            return web.json_response(
                {"choices": [{"message": {"content": '```json\n{"decision": "BUY"}\n```'}}]}
            )

        async def models(request):
            return web.json_response({"data": [{"id": "tiny"}]})

        async def lmstudio_models(request):
            if not lm_mode["lmstudio"]:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response(
                {
                    "models": [
                        {"key": "qwen2.5-coder-7b-instruct", "type": "llm", "loaded_instances": [{"id": "x"}],
                         "params_string": "7B", "publisher": "Qwen"},
                        {"key": "text-embedding", "type": "embedding", "loaded_instances": []},
                    ]
                }
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", chat)
        app.router.add_get("/v1/models", models)
        app.router.add_get("/api/v1/models", lmstudio_models)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        base = f"http://127.0.0.1:{port}/v1"
        try:
            from pydantic import BaseModel

            from bettercalltrader.llm import fetch_models

            class Decision(BaseModel):
                decision: str

            cfg = LLMConfig(provider="openai", model="tiny", local_model="tiny", base_url=base)
            client = LLMClient(cfg)
            assert client.available and client.is_local and client.label == "local:tiny"
            decision = await client.complete_json("pick one", Decision)
            assert decision.decision == "BUY"
            assert client.last_provider == "local:tiny"
            assert client.describe()["chain"] == ["local:tiny"]
            assert (await client.complete_text("ping")).startswith("```json")
            # probe() is synchronous urllib, so it must run off the server loop.
            assert (await asyncio.to_thread(probe, cfg, 2.0))["state"] == "ok"

            # Discovery prefers LM Studio REST, then falls back to OpenAI /models.
            openai_list = await fetch_models(base, timeout=2.0)
            assert openai_list["reachable"] and openai_list["source"] == "openai"
            assert [model["id"] for model in openai_list["models"]] == ["tiny"]
            lm_mode["lmstudio"] = True
            lm_list = await fetch_models(base, timeout=2.0)
            assert lm_list["source"] == "lmstudio"
            assert lm_list["models"][0]["id"] == "qwen2.5-coder-7b-instruct"
            assert lm_list["models"][0]["loaded"] is True
            assert lm_list["models"][1]["type"] == "embedding"
            # An unreachable server is a normal state, not an exception.
            down = await fetch_models("http://127.0.0.1:9/v1", timeout=0.4)
            assert down["reachable"] is False and down["models"] == []
        finally:
            await runner.cleanup()

    asyncio.run(run())

    # Command Center model selection: local first, Gemini first, auto, persistence.
    class FakeResearcher:
        pass

    researcher = FakeResearcher()
    researcher.llm = LLMClient(
        LLMConfig(
            provider="auto",
            model="primary",
            local_model="primary",
            local_models=("primary", "fallback-a"),
            base_url="http://127.0.0.1:9/v1",
            gemini_api_key="g",
        )
    )
    researcher.api_key = "g"
    researcher.model_name = researcher.llm.model_name
    researcher.client = researcher.llm
    researcher.llm_base_config = researcher.llm.config
    rb.apply_llm_selection(researcher, "local", "fallback-a")
    assert researcher.llm.config.provider == "openai"
    assert researcher.model_name == "fallback-a"
    assert [spec.model for spec in researcher.llm.config.providers() if spec.kind == "openai"] == [
        "fallback-a", "primary"
    ]
    rb.apply_llm_selection(researcher, "gemini")
    assert researcher.llm.config.provider == "gemini"
    assert researcher.llm.label.startswith("gemini:")
    rb.apply_llm_selection(researcher, "auto")
    assert researcher.llm.config.provider == "auto"
    # Switching back to auto restores the pristine .env order, not the pinned one.
    assert [spec.model for spec in researcher.llm.config.providers() if spec.kind == "openai"] == [
        "primary", "fallback-a"
    ]
    try:
        rb.apply_llm_selection(researcher, "bogus")
        raise AssertionError("unknown provider should raise")
    except ValueError:
        pass
    bare = FakeResearcher()
    bare.llm = LLMClient(LLMConfig(provider="auto", gemini_api_key="g"))
    bare.api_key = "g"
    bare.model_name = bare.llm.model_name
    bare.client = bare.llm
    try:
        rb.apply_llm_selection(bare, "local", "")
        raise AssertionError("missing local model should raise")
    except ValueError:
        pass

    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0)
        portfolio._set_state("llm_provider", "local")
        portfolio._set_state("llm_model", "fallback-a")
        rb.restore_llm_selection(portfolio, researcher)
        assert researcher.llm.config.local_model == "fallback-a"
        assert researcher.llm.label == "local:fallback-a"
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass

    print("  v11 LLM provider layer: OK")


def test_v12_llm_reliability():
    """JSON repair, circuit breaker, LM Studio control and the eval scoring."""
    import asyncio
    import json

    from bettercalltrader.llm import (
        JSONParseError,
        LLMClient,
        LLMConfig,
        LLMError,
        extract_json_object,
        parse_model_json,
        repair_json_text,
    )
    from bettercalltrader.lmstudio import enforce_max_loaded, parse_ps
    from bettercalltrader.prompts import build_research_prompt

    # --- JSON repair -------------------------------------------------------
    assert parse_model_json('{"a": 1}') == ({"a": 1}, "raw")
    assert parse_model_json('{"a": 1,}')[0] == {"a": 1}
    assert parse_model_json("{'a': True, 'b': None}")[0] == {"a": True, "b": None}
    assert parse_model_json('{"a": 1}\n{"b": 2}')[0] == {"a": 1}
    assert parse_model_json('{"a": {"b": 2}')[0] == {"a": {"b": 2}}
    assert parse_model_json('```json\n{"a": [1, 2,]}\n```')[0] == {"a": [1, 2]}
    assert parse_model_json('{"a": 1 // note\n}')[0] == {"a": 1}
    assert repair_json_text('noise {"a": 1}') == '{"a": 1}'
    # A truncated object is repaired, not rejected: that is the point.
    assert parse_model_json('{"a": "b"')[0] == {"a": "b"}
    for bad in ("100%", "", "{unterminated", '{"a": }'):
        try:
            extract_json_object(bad)
            raise AssertionError(f"{bad!r} should not parse")
        except JSONParseError:
            pass

    # --- circuit breaker ---------------------------------------------------
    clock = {"now": 100.0}

    def now():
        return clock["now"]

    cfg = LLMConfig(
        provider="openai",
        model="a",
        local_model="a",
        local_models=("a", "b"),
        base_url="http://127.0.0.1:9/v1",
    )
    client = LLMClient(cfg, breaker_threshold=2, breaker_cooldown_sec=30.0, clock=now)
    calls = {"a": 0, "b": 0}

    async def fake_call(spec, prompt, schema, session):
        calls[spec.model] += 1
        if spec.model == "a":
            raise RuntimeError("connection refused")
        return '{"decision": "SKIP", "risk_score": 50, "hype_score": 50, ' \
               '"summary_hr": "ok", "red_flags": [], "green_flags": []}'

    client._call = fake_call  # type: ignore[method-assign]
    asyncio.run(client.complete_json("pick"))
    asyncio.run(client.complete_json("pick"))
    health = {entry["provider"]: entry for entry in client.health()}
    assert health["local:a"]["consecutiveFailures"] == 2
    assert health["local:a"]["open"] is True
    assert health["local:b"]["successes"] == 2
    # Provider "a" is skipped now, so only "b" is called.
    asyncio.run(client.complete_json("pick"))
    assert calls["a"] == 2 and calls["b"] == 3
    assert {entry["provider"]: entry for entry in client.health()}["local:a"]["skipped"] >= 1
    # Half-open: after the cooldown it is attempted again.
    clock["now"] += 31
    try:
        asyncio.run(client.complete_json("pick"))
    except Exception:  # noqa: BLE001 - both providers may fail
        pass
    assert calls["a"] == 3

    # A JSON parse failure gets exactly one corrective retry, then counts once.
    retry_client = LLMClient(
        LLMConfig(provider="openai", model="a", local_model="a", local_models=("a",),
                  base_url="http://127.0.0.1:9/v1"),
        clock=now,
    )
    attempts = {"n": 0}

    async def bad_call(spec, prompt, schema, session):
        attempts["n"] += 1
        return "not json at all"

    retry_client._call = bad_call  # type: ignore[method-assign]
    try:
        asyncio.run(retry_client.complete_json("pick"))
        raise AssertionError("malformed replies should raise")
    except JSONParseError:
        pass
    except LLMError as exc:  # the aggregate error keeps the per-provider reason
        assert "invalid JSON" in str(exc)
    assert attempts["n"] == 2 and retry_client.retries == 1
    assert retry_client.health()[0]["failures"] == 1

    # --- LM Studio control -------------------------------------------------
    ps = json.dumps([
        {
            "identifier": "qwen3.5-0.8b-uncensored",
            "modelKey": "qwen3.5-0.8b-uncensored",
            "status": "idle",
            "sizeBytes": 811836000,
            "contextLength": 262144,
            "paramsString": "0.8B",
        },
        {"identifier": "big:2", "modelKey": "big", "status": "loaded"},
    ])
    parsed = parse_ps(ps)
    assert [entry["identifier"] for entry in parsed] == ["qwen3.5-0.8b-uncensored", "big:2"]
    assert parsed[0]["sizeBytes"] == 811836000 and parsed[0]["params"] == "0.8B"
    assert parsed[1]["modelKey"] == "big"
    assert parse_ps("") == [] and parse_ps("not json") == []

    import bettercalltrader.lmstudio as lms

    original_loaded, original_unload = lms.loaded_models, lms.unload
    unloaded = []
    try:
        lms.loaded_models = lambda timeout=20.0: {  # type: ignore[assignment]
            "available": True,
            "reachable": True,
            "models": parsed,
            "detail": "",
        }
        lms.unload = lambda model="", all_models=False, timeout=60.0: (  # type: ignore[assignment]
            unloaded.append(model) or {"ok": True, "detail": "", "unloaded": [model]}
        )
        result = enforce_max_loaded("qwen3.5-0.8b-uncensored", max_loaded=1)
        assert result["applied"] is True
        assert result["unloaded"] == ["big:2"], result
    finally:
        lms.loaded_models = original_loaded
        lms.unload = original_unload

    # --- shared prompt -----------------------------------------------------
    prompt = build_research_prompt({"mint": "M", "symbol": "S"}, {"top10_holder_pct": 12.0})
    assert '"mint": "M"' in prompt and "PRAVILA EVALUACIJE" in prompt
    assert "PRAVILA EVALUACIJE" in rb.build_research_prompt({"mint": "M"}, {})

    print("  v12 LLM reliability + memory control: OK")


def _training_row(mint, decision="SKIP", rule="AI_VERDICT", net=None, trades=0, verdict=None):
    import json

    market = {"mint": mint, "symbol": mint[:4].upper(), "price_usd": "0.001", "liquidity_usd": 25000}
    security = {"status": "ok", "top10_holder_pct": 12.5, "is_bundled": False}
    payload = verdict if verdict is not None else {
        "decision": decision,
        "risk_score": 55,
        "hype_score": 61,
        "summary_hr": "test verdict",
        "red_flags": [],
        "green_flags": ["clean mint"],
    }
    return {
        "mint": mint,
        "symbol": mint[:4].upper(),
        "source": "TEST",
        "market_json": json.dumps(market),
        "security_json": json.dumps(security),
        "verdict_json": json.dumps(payload) if not isinstance(payload, str) else payload,
        "scanned_at": "2026-01-01 00:00:00",
        "source_tag": "TEST",
        "rule": rule,
        "net_pnl": net,
        "trades": trades,
    }


def test_v12_export_training():
    """The SFT exporter keeps the right rows and emits a trainable chat record."""
    import json

    import export_training as exp

    rows = [
        _training_row("MintAI0001", "SKIP", "AI_VERDICT"),
        _training_row("MintAI0002", "WATCH", "LOCAL_AI_VERDICT"),
        _training_row("MintHeur01", "SKIP", "HEURISTIC_VERDICT"),
        _training_row("MintHard01", "SKIP", "HARD_FILTER"),
        _training_row("MintAI0001", "SKIP", "AI_VERDICT"),  # duplicate mint
        _training_row("MintBought1", "BUY", "AI_VERDICT", net=0.42, trades=1),
        _training_row("MintLosing1", "BUY", "AI_VERDICT", net=-0.18, trades=2),
        _training_row("MintBroken1", verdict="{not json"),
        _training_row("MintSchema1", verdict={"decision": "MAYBE"}),
    ]

    # AI labels only, deduplicated by mint, broken verdicts dropped.
    examples, stats = exp.build_dataset(rows)
    mints = [example["metadata"]["mint"] for example in examples]
    assert mints == ["MintAI0001", "MintAI0002", "MintBought1", "MintLosing1"], mints
    assert stats["byLabelSource"] == {"ai": 4}
    assert stats["skipped"] == 5
    assert stats["withOutcome"] == 2
    assert stats["byDecision"] == {"SKIP": 1, "WATCH": 1, "BUY": 2}

    # A chat example is a valid SFT record in the model's own contract.
    first = examples[0]
    assert [message["role"] for message in first["messages"]] == ["system", "user", "assistant"]
    assert "JSON Schema" in first["messages"][0]["content"]
    assert "PRAVILA EVALUACIJE" in first["messages"][1]["content"]
    answer = json.loads(first["messages"][2]["content"])
    assert set(answer) == {"decision", "risk_score", "hype_score", "summary_hr", "red_flags", "green_flags"}

    # Pipeline knobs do exactly one thing each.
    heuristic, stats_h = exp.build_dataset(rows, include_heuristic=True)
    assert "MintHeur01" in [example["metadata"]["mint"] for example in heuristic]
    assert stats_h["byLabelSource"]["heuristic"] == 1
    hard, _ = exp.build_dataset(rows, include_heuristic=True, include_hard_filter=True)
    assert "MintHard01" in [example["metadata"]["mint"] for example in hard]
    buys, _ = exp.build_dataset(rows, decisions={"BUY"})
    assert [example["metadata"]["decision"] for example in buys] == ["BUY", "BUY"]
    outcomes, _ = exp.build_dataset(rows, only_outcomes=True)
    assert {example["metadata"]["mint"] for example in outcomes} == {"MintBought1", "MintLosing1"}
    winners, _ = exp.build_dataset(rows, positive_only=True)
    assert [example["metadata"]["mint"] for example in winners] == ["MintBought1"]
    no_dedupe, _ = exp.build_dataset(rows, dedupe=False)
    assert len(no_dedupe) == 5
    no_system, _ = exp.build_dataset(rows, dedupe=False, include_system=False)
    assert [message["role"] for message in no_system[0]["messages"]] == ["user", "assistant"]

    # Alpaca is a lossless re-shaping of the same three messages.
    alpaca = exp.to_alpaca(first)
    assert alpaca["input"] == first["messages"][1]["content"]
    assert alpaca["output"] == first["messages"][2]["content"]
    assert alpaca["metadata"] == first["metadata"]

    # End to end against a real (temporary) database, including the val split.
    import json as _json

    db_path = make_temp_db()
    out_dir = Path(tempfile.mkdtemp(dir=Path(tempfile.gettempdir())))
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        for index in range(12):
            mint = f"ExportMint{index:03d}"
            market = {
                "mint": mint,
                "symbol": f"E{index}",
                "name": f"Export {index}",
                "price_usd": "0.001",
                "liquidity_usd": 20000,
                "fetched_at": portfolio.now_ts(),
            }
            decision = rb.TokenAnalysisDecision(
                decision="WATCH" if index % 2 else "SKIP",
                risk_score=40 + index,
                hype_score=50 + index,
                summary_hr=f"export {index}",
                red_flags=[],
                green_flags=["clean"],
            )
            portfolio.save_scan_result(market, {"status": "ok"}, decision, "TEST")
            portfolio.journal(
                decision.decision,
                mint=mint,
                symbol=market["symbol"],
                rule="AI_VERDICT",
                reasons="export",
            )
        train_path = out_dir / "sft.jsonl"
        val_path = out_dir / "val.jsonl"
        assert exp.main(
            ["--db", db_path, "--out", str(train_path), "--val-out", str(val_path), "--no-dedupe"]
        ) == 0
        train_lines = [line for line in train_path.read_text(encoding="utf-8").splitlines() if line]
        val_lines = [line for line in val_path.read_text(encoding="utf-8").splitlines() if line]
        assert len(train_lines) == 10 and len(val_lines) == 2, (len(train_lines), len(val_lines))
        parsed = [_json.loads(line) for line in train_lines]
        assert all(row["messages"][-1]["role"] == "assistant" for row in parsed)
    finally:
        cleanup_db(db_path)
        import shutil

        shutil.rmtree(out_dir, ignore_errors=True)

    print("  v12 SFT data export: OK")


def test_v12_paper_live_parity():
    """replay_v10 must rebuild the same realized book the live path recorded.

    The interesting failure is the WAL: the bot keeps writing while the replay
    copies the file, so a snapshot that ignores ``-wal`` silently loses the most
    recent trades. This test holds a reader open (blocking checkpoint on close),
    then checks the replay still matches the live metrics exactly.
    """
    import sqlite3 as _sqlite3

    import replay_v10
    from bettercalltrader.backtest import build_report
    from bettercalltrader.metrics import collect_basic_metrics

    db_path = make_temp_db()
    snapshot = db_path + ".replay"
    keeper = None
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        portfolio.cluster_cooldown_sec = 0
        portfolio.stop_loss_cooldown_sec = 0
        portfolio.min_seconds_between_entries = 0
        portfolio.max_daily_stop_losses = 100
        portfolio.max_daily_trades = 100

        # Keep one connection open so the WAL cannot be truncated on close.
        keeper = _sqlite3.connect(db_path, timeout=15)
        keeper.execute("SELECT COUNT(*) FROM wallet").fetchone()

        trades = [
            ("ParityWin111111111111111111111111111111111", 1.0, 1.6, "WIN"),
            ("ParityLoss11111111111111111111111111111111", 1.0, 0.7, "LOSS"),
        ]
        for mint, entry, exit_price, symbol in trades:
            market = {
                "mint": mint,
                "symbol": symbol,
                "name": symbol,
                "price_usd": str(entry),
                "liquidity_usd": 50000.0,
                "dex_id": "raydium",
                "fetched_at": portfolio.now_ts(),
            }
            assert portfolio.open_virtual_position(
                market, conviction="manual", source="TEST", size_override=0.5
            ) is True
            conn = portfolio._conn()
            conn.execute("UPDATE positions SET current_price_usd = ? WHERE mint = ?", (exit_price, mint))
            conn.commit()
            conn.close()
            time.sleep(0.01)
            assert portfolio.close_position_manually(mint) is True

        live = collect_basic_metrics(db_path)
        assert live["totalTrades"] == 2
        assert live["netPnlSol"] != 0
        # The sidecar really exists: this is the un-checkpointed data the replay
        # has to copy, not a database that was already flushed on close.
        wal = Path(str(db_path) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0

        replay_v10.snapshot_database(Path(db_path), Path(snapshot))
        report = build_report(snapshot, initial_sol=10.0)
        summary = report["summary"]

        assert summary["trades"] == int(live["totalTrades"])
        assert abs(summary["netPnlSol"] - live["netPnlSol"]) < 1e-9, (summary["netPnlSol"], live["netPnlSol"])
        assert abs(summary["winRate"] - live["winRate"]) < 1e-9
        assert [trade["symbol"] for trade in report["trades"]] == ["WIN", "LOSS"]

        # The replay's own accounting must be internally consistent.
        assert abs(sum(trade["pnlSol"] for trade in report["trades"]) - summary["netPnlSol"]) < 1e-9
        assert abs(report["equityCurve"][-1]["equitySol"] - (10.0 + summary["netPnlSol"])) < 1e-9
        assert abs(sum(report["dailyPnl"].values()) - summary["netPnlSol"]) < 1e-9

        # Snapshotting twice is idempotent: replay is a pure function of the file.
        second = db_path + ".replay2"
        try:
            replay_v10.snapshot_database(Path(db_path), Path(second))
            again = build_report(second, initial_sol=10.0)
            assert again["summary"]["trades"] == summary["trades"]
            assert abs(again["summary"]["netPnlSol"] - summary["netPnlSol"]) < 1e-12
        finally:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(second + suffix):
                    os.remove(second + suffix)
    finally:
        if keeper is not None:
            keeper.close()
        cleanup_db(db_path)
        cleanup_db(snapshot)
    print("  v12 paper/live replay parity: OK")


def test_v12_totp():
    """RFC 6238 vectors, clock-drift window and fail-closed parsing."""
    from bettercalltrader import security as sec

    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    vectors = [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ]
    for at, expected in vectors:
        assert sec.totp_now(secret, at=at, digits=8) == expected, at
        assert sec.verify_totp(expected, secret, at=at, digits=8) is True

    # Authenticator exports often arrive lower-case with spaces and dashes.
    pretty = "gezd gnbv-gy3t qojq gezd gnbv gy3t qojq"
    assert sec.normalize_totp_secret(pretty) == secret
    assert sec.totp_now(pretty, at=59, digits=8) == "94287082"

    # Default is 6 digits: the last six of the 8-digit vector.
    assert sec.totp_now(secret, at=59) == "287082"
    assert sec.verify_totp("287082", secret, at=59) is True
    assert sec.verify_totp(" 287082 ", secret, at=59) is True

    # +/- one 30s step is accepted, anything further is not.
    assert sec.verify_totp(sec.totp_now(secret, at=29), secret, at=59) is True
    assert sec.verify_totp(sec.totp_now(secret, at=89), secret, at=59) is True
    assert sec.verify_totp(sec.totp_now(secret, at=119), secret, at=59) is False
    assert sec.verify_totp(sec.totp_now(secret, at=149), secret, at=59) is False
    assert sec.verify_totp(sec.totp_now(secret, at=89), secret, at=59, window=0) is False

    # Malformed input fails closed instead of raising.
    assert sec.verify_totp(None, secret, at=59) is False
    assert sec.verify_totp("", secret, at=59) is False
    assert sec.verify_totp("abcdef", secret, at=59) is False
    assert sec.verify_totp("28708", secret, at=59) is False
    assert sec.verify_totp("2870821", secret, at=59) is False
    assert sec.verify_totp("287082", "", at=59) is False
    assert sec.verify_totp("287082", "!!!!", at=59) is False
    try:
        sec.totp_now("", at=59)
    except ValueError:
        pass
    else:
        raise AssertionError("an empty TOTP secret must raise")

    print("  v12 TOTP (RFC 6238): OK")


def test_v12_telegram_digest():
    """The digest reflects the portfolio, flags open breakers and is throttled."""
    import asyncio
    from datetime import datetime, timezone

    from bettercalltrader.telegram import TelegramNotifier, build_digest

    class _FakeLLM:
        def health(self):
            return [
                {"provider": "gemini", "calls": 12, "failures": 1, "open": False},
                {"provider": "openai", "calls": 3, "failures": 3, "open": True},
            ]

    class _FakeResearcher:
        llm = _FakeLLM()

    researcher = _FakeResearcher()
    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)

        text = build_digest(
            portfolio,
            researcher,
            now=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        )
        assert "BetterCallTrader digest" in text
        assert "2026-01-02 03:04 UTC" in text
        assert "10.000 SOL" in text
        assert "Breaker: ✅ OK" in text
        assert "gemini 12c/1f" in text
        assert "openai 3c/3f 🚨" in text
        assert "📂" not in text  # no open positions yet

        # An open position contributes its unrealized move.
        mint = "DigestMint111111111111111111111111111111"
        market = {
            "mint": mint,
            "symbol": "DGT",
            "name": "Digest",
            "price_usd": "1.0",
            "liquidity_usd": 50000.0,
            "dex_id": "raydium",
            "fetched_at": portfolio.now_ts(),
        }
        assert portfolio.open_virtual_position(
            market, conviction="manual", source="TEST", size_override=0.5
        ) is True
        conn = portfolio._conn()
        conn.execute("UPDATE positions SET current_price_usd = 1.2 WHERE mint = ?", (mint,))
        conn.commit()
        conn.close()

        text = build_digest(portfolio, researcher)
        assert "📂 $DGT" in text and "%" in text

        # Interval throttle: send, skip inside the window, send once it elapses.
        notifier = TelegramNotifier()
        notifier.token = "test-token"
        notifier.chat_id = "test-chat"
        notifier.digest_interval_sec = 3600
        sent = []

        async def fake_send(session, message, buttons=None):
            sent.append(message)

        notifier.send_now = fake_send  # type: ignore[assignment]

        async def run():
            assert await notifier.maybe_send_digest(None, portfolio, researcher, now=1000.0) is True
            assert await notifier.maybe_send_digest(None, portfolio, researcher, now=2000.0) is False
            assert await notifier.maybe_send_digest(None, portfolio, researcher, now=4600.0) is True
            assert (
                await notifier.maybe_send_digest(
                    None, portfolio, researcher, now=5000.0, interval_sec=0
                )
                is False
            )
            # /digest is on demand and ignores the schedule.
            await notifier._handle_text_command(None, portfolio, researcher, None, "/digest")

        asyncio.run(run())
        assert len(sent) == 3, len(sent)
        assert "BetterCallTrader digest" in sent[-1]

        # A notifier without credentials never sends.
        disabled = TelegramNotifier()
        disabled.token = ""
        disabled.chat_id = ""
        assert asyncio.run(disabled.maybe_send_digest(None, portfolio, researcher, now=1.0)) is False
    finally:
        cleanup_db(db_path)
    print("  v12 Telegram digest: OK")


def test_v12_staged_arming():
    """dry_run/probe/full gate live orders without a socket and fail closed."""
    import asyncio

    from bettercalltrader import live_readiness as lr

    class _RefusingSession:
        def __init__(self):
            self.calls = 0

        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)

            def _refuse(*args, **kwargs):
                self.calls += 1
                raise AssertionError(f"unexpected network call via {name}")

            return _refuse

    def make_agent(**kwargs):
        return rb.SolanaExecutionAgent(
            rpc_url="http://127.0.0.1:9",
            private_key_b58="",
            slippage_bps=500,
            priority_fee_sol=0.001,
            **kwargs,
        )

    mint = rb.SolanaExecutionAgent.SOL_MINT

    async def run():
        # Paper mode ignores the stage entirely.
        paper = make_agent(live_trading_enabled=False, live_trading_stage="dry_run")
        outcome = await paper.execute_buy(_RefusingSession(), mint, 0.05, dex_id="pumpfun")
        assert outcome["status"] == "DRY_RUN_READY"

        # dry_run stage plans the order and touches no network.
        session = _RefusingSession()
        agent = make_agent(
            live_trading_enabled=True,
            live_trading_confirmed=True,
            live_trading_stage="dry_run",
            max_live_trade_sol=1.0,
        )
        outcome = await agent.execute_buy(session, mint, 0.05, dex_id="pumpfun")
        assert outcome["status"] == "DRY_RUN_STAGED"
        assert session.calls == 0
        assert agent.live_orders_attempted == 0

        # An unknown stage fails closed.
        session = _RefusingSession()
        agent = make_agent(
            live_trading_enabled=True,
            live_trading_confirmed=True,
            live_trading_stage="banana",
            max_live_trade_sol=1.0,
        )
        outcome = await agent.execute_buy(session, mint, 0.05, dex_id="pumpfun")
        assert outcome["status"] == "BLOCKED"
        assert "LIVE_TRADING_STAGE" in outcome["reason"]
        assert agent.live_trading_stage == "dry_run"
        assert session.calls == 0

        # Probe spends its budget on the attempt, even when the send fails.
        session = _RefusingSession()
        agent = make_agent(
            live_trading_enabled=True,
            live_trading_confirmed=True,
            live_trading_stage="probe",
            stage_max_live_orders=1,
            max_live_trade_sol=1.0,
        )
        first = await agent.execute_buy(session, mint, 0.05, dex_id="pumpfun")
        # No key, so the live path fails before the socket — but the attempt
        # still spends the probe budget (conservative accounting).
        assert first["status"] == "ERROR"
        assert session.calls == 0
        assert agent.live_orders_attempted == 1
        second = await agent.execute_buy(session, mint, 0.05, dex_id="pumpfun")
        assert second["status"] == "BLOCKED"
        assert "budget exhausted" in second["reason"]
        assert session.calls == 0

        # Full keeps the historical behaviour and has no local budget.
        session = _RefusingSession()
        agent = make_agent(
            live_trading_enabled=True,
            live_trading_confirmed=True,
            live_trading_stage="full",
            max_live_trade_sol=1.0,
        )
        outcome = await agent.execute_buy(session, mint, 0.05, dex_id="pumpfun")
        assert outcome["status"] == "ERROR"
        assert session.calls == 0
        assert agent.live_orders_attempted == 0

    asyncio.run(run())

    # Stage parsing and readiness reporting.
    assert lr.normalize_live_stage(None) == "full"
    assert lr.normalize_live_stage("") == "full"
    assert lr.normalize_live_stage("DRY-RUN") == "dry_run"
    assert lr.normalize_live_stage("probe") == "probe"
    assert lr.normalize_live_stage("nope") is None

    live_env = {
        "LIVE_TRADING_ENABLED": "true",
        "LIVE_TRADING_CONFIRMED": "true",
        "SOLANA_PRIVATE_KEY": "x",
        "MAX_LIVE_TRADE_SOL": "0.05",
        "SOLANA_RPC_URL": "http://127.0.0.1:8899",
    }
    dry = lr.check_environment({**live_env, "LIVE_TRADING_STAGE": "dry_run"}, solders_available=True)
    assert dry["fails"] == 0
    assert dry["liveTradingStage"] == "dry_run"
    assert any(
        issue["key"] == "LIVE_TRADING_STAGE" and issue["level"] == "info"
        for issue in dry["issues"]
    )

    probe = lr.check_environment(
        {**live_env, "LIVE_TRADING_STAGE": "probe", "LIVE_STAGE_MAX_TRADES": "2"},
        solders_available=True,
    )
    assert probe["liveTradingStage"] == "probe"
    assert probe["stageMaxLiveOrders"] == 2
    assert any(
        issue["level"] == "warn" and "probe stage" in issue["message"] for issue in probe["issues"]
    )

    invalid = lr.check_environment(
        {**live_env, "LIVE_TRADING_STAGE": "banana"}, solders_available=True
    )
    assert invalid["fails"] >= 1
    assert invalid["liveTradingStageInvalid"] is True
    assert invalid["liveTradingStage"] == "dry_run"

    # The guard harness covers all three stages and stays network-free.
    guards = lr.audit_guards()
    assert guards["ok"] is True
    observed = {item["name"]: item["observed"] for item in guards["scenarios"]}
    assert observed["live_dry_run_stage"] == "DRY_RUN_STAGED"
    assert observed["live_probe_exhausted"] == "BLOCKED"
    assert observed["live_invalid_stage"] == "BLOCKED"
    assert guards["networkCalls"] == 0

    print("  v12 staged go-live arming: OK")


def test_v13_safety_batch():
    import asyncio
    import json
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    class _Store:
        def __init__(self, data=None):
            self.data = dict(data or {})

        def _get_state(self, key, default=None):
            return self.data.get(key, default)

        def _set_state(self, key, value):
            self.data[key] = json.loads(json.dumps(value))

    class _RefusingSession:
        def __init__(self):
            self.calls = 0

        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)

            def _refuse(*args, **kwargs):
                self.calls += 1
                raise AssertionError(f"unexpected network call via {name}")

            return _refuse

    class _ConfigStub:
        class _Llm:
            def describe(self):
                return {}

        def __init__(self):
            self.llm = self._Llm()

        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return 0.0

    def make_agent(store, **kwargs):
        return rb.SolanaExecutionAgent(
            rpc_url="http://127.0.0.1:9",
            private_key_b58="",
            slippage_bps=500,
            priority_fee_sol=0.001,
            live_trading_enabled=True,
            live_trading_confirmed=True,
            live_trading_stage="probe",
            stage_max_live_orders=2,
            max_live_trade_sol=1.0,
            state_store=store,
            **kwargs,
        )

    mint = rb.SolanaExecutionAgent.SOL_MINT

    async def run():
        store = _Store()
        first = make_agent(store)
        assert first.live_orders_attempted == 0
        session = _RefusingSession()
        # No key: the attempt errors before the socket, but still spends budget.
        assert (await first.execute_buy(session, mint, 0.05, dex_id="pumpfun"))["status"] == "ERROR"
        assert (await first.execute_buy(session, mint, 0.05, dex_id="pumpfun"))["status"] == "ERROR"
        assert first.live_orders_attempted == 2
        assert session.calls == 0
        assert store.data["live_probe_budget"] == {"day": today, "attempted": 2}

        # Restart: a fresh agent restores the persisted budget and blocks.
        restarted = make_agent(store)
        assert restarted.live_orders_attempted == 2
        blocked = await restarted.execute_buy(_RefusingSession(), mint, 0.05, dex_id="pumpfun")
        assert blocked["status"] == "BLOCKED"
        assert "budget exhausted" in blocked["reason"]
        assert restarted.live_orders_attempted == 2

        # A new UTC day starts from zero and rewrites on the next attempt.
        stale = _Store({"live_probe_budget": {"day": "2000-01-01", "attempted": 9}})
        rolled = make_agent(stale)
        assert rolled.live_orders_attempted == 0
        assert (await rolled.execute_buy(_RefusingSession(), mint, 0.05, dex_id="pumpfun"))["status"] == "ERROR"
        assert rolled.live_orders_attempted == 1
        assert stale.data["live_probe_budget"] == {"day": today, "attempted": 1}

        # An in-process midnight rollover resets before the budget check.
        rolled._probe_day = "2000-01-01"
        stale.data["live_probe_budget"] = {"day": "2000-01-01", "attempted": 1}
        rolled.refresh_probe_budget()
        assert rolled.live_orders_attempted == 0

    asyncio.run(run())

    # build_config_payload reports the persisted count and refreshes the day.
    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path)
        store = _Store({"live_probe_budget": {"day": today, "attempted": 1}})
        agent = make_agent(store)
        assert agent.live_orders_attempted == 1
        cfg = rb.build_config_payload(portfolio, _ConfigStub(), agent)
        assert cfg["stageOrdersAttempted"] == 1
        assert cfg["stageMaxLiveOrders"] == 2
        # Pretend the process slept past midnight with a stale in-memory day.
        agent._probe_day = "2000-01-01"
        agent.live_orders_attempted = 5
        store.data["live_probe_budget"] = {"day": "2000-01-01", "attempted": 5}
        cfg = rb.build_config_payload(portfolio, _ConfigStub(), agent)
        assert cfg["stageOrdersAttempted"] == 0
    finally:
        cleanup_db(db_path)

    # Failed token/TOTP checks leave an audit trail and increment the counter.
    class _Req:
        method = "POST"
        path = "/api/config"
        headers = {"Origin": "http://127.0.0.1:8080"}

    def counter(kind):
        needle = f'bettercalltrader_auth_failures_total{{kind="{kind}"}}'
        total = 0.0
        for line in rb.HTTP_METRICS.render().splitlines():
            if line.startswith(needle + " "):
                total += float(line.rsplit(" ", 1)[1])
        return total

    fd, audit_name = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    audit_path = Path(audit_name)
    try:
        before_token = counter("token")
        before_totp = counter("totp")
        rb.record_auth_failure(
            "token", request=_Req(), ip="203.0.113.9", reason="invalid_token", log_path=str(audit_path)
        )
        rb.record_auth_failure(
            "totp", request=_Req(), ip="203.0.113.9", reason="invalid_totp", log_path=str(audit_path)
        )
        assert counter("token") == before_token + 1
        assert counter("totp") == before_totp + 1
        rows = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert [row["event"] for row in rows] == ["auth_failed", "auth_failed"]
        assert [row["kind"] for row in rows] == ["token", "totp"]
        assert rows[1]["reason"] == "invalid_totp"
        assert rows[1]["path"] == "/api/config"
        assert rows[1]["origin"] == "http://127.0.0.1:8080"
        assert rows[0]["ip"] == "203.0.113.9"
        assert rows[0]["method"] == "POST"
    finally:
        try:
            audit_path.unlink()
        except OSError:
            pass

    print("  v13 probe budget persistence + auth failure audit: OK")


def test_v13_eval_compare():
    from eval_models import _build_parser, compare_results

    baseline = {
        "results": [
            {"model": "alpha", "score": 80.0, "valid": True},
            {"model": "beta", "score": 60.0, "valid": True},
            {"model": "gamma", "score": 99.0, "valid": False},
            {"model": "dropped", "score": 50.0, "valid": True},
        ]
    }
    current = [
        {"model": "alpha", "score": 82.0, "valid": True},
        {"model": "beta", "score": 50.0, "valid": True},
        {"model": "gamma", "score": 10.0, "valid": False},
        {"model": "new", "score": 70.0, "valid": True},
    ]

    table, regressions = compare_results(current, baseline, max_drop=5.0)
    assert regressions == [("beta", -10.0)]
    assert "| `alpha` | 80.0 | 82.0 | +2.0 |" in table
    assert "| `beta` | 60.0 | 50.0 | -10.0 |" in table
    assert "| `dropped` | 50.0 | not run | — |" in table
    assert "| `new` | — | 70.0 | new |" in table
    # An invalid baseline entry is not a baseline, and an invalid current entry
    # is not a result: gamma's 99 -> 10 must not be reported as a regression.
    assert "`gamma`" not in table
    assert "max allowed drop: 5.0 points" in table

    # A drop exactly at the limit is tolerated; one point past it is not.
    _, at_limit = compare_results(
        [{"model": "beta", "score": 55.0, "valid": True}], baseline, max_drop=5.0
    )
    assert at_limit == []
    _, past_limit = compare_results(
        [{"model": "beta", "score": 54.9, "valid": True}], baseline, max_drop=5.0
    )
    assert len(past_limit) == 1
    assert past_limit[0][0] == "beta"
    assert abs(past_limit[0][1] - (-5.1)) < 1e-9

    # A looser threshold clears the same regression.
    _, loose = compare_results(current, baseline, max_drop=25.0)
    assert loose == []

    args = _build_parser().parse_args([])
    assert args.fail_under is None
    assert args.compare_json == ""
    assert args.max_drop == 5.0

    print("  v13 eval compare/regression gate: OK")


def test_v11_ml_contract():
    import json
    import sqlite3

    from bettercalltrader.ml import (
        FEATURE_SET_VERSION, MODEL_SCHEMA_VERSION, build_training_rows,
        calibration_report, data_fingerprint, load_model, model_status, save_model,
        train_if_stale, train_model,
    )

    db_path = make_temp_db()
    model_path = db_path + ".model.json"
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, closed_at TEXT,
                net_pnl_sol REAL, invested_sol REAL, conviction TEXT, source TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE decision_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, risk_score INTEGER,
                hype_score INTEGER, suggested_size_sol REAL, extra_json TEXT
            )"""
        )
        for i in range(30):
            mint = f"mint{i}"
            risk = 25 + (i % 10) * 7
            hype = 25 + ((i * 3) % 10) * 7
            win = risk >= 60 and hype >= 60
            conn.execute(
                "INSERT INTO trade_history (mint, closed_at, net_pnl_sol, invested_sol, conviction, source) VALUES (?,?,?,?,?,?)",
                (mint, "2026-01-01 12:00:00", 0.25 if win else -0.2, 0.5, "high", "TEST"),
            )
            conn.execute(
                "INSERT INTO decision_journal (mint, risk_score, hype_score, suggested_size_sol, extra_json) VALUES (?,?,?,?,?)",
                (mint, risk, hype, 0.5, "{}"),
            )
        conn.commit()
        conn.close()

        rows = build_training_rows(db_path)
        first = data_fingerprint(rows)
        assert len(first) == 12 and data_fingerprint(list(rows)) == first

        model = train_model(db_path)
        assert model.get("ok"), model
        assert model["schema"] == MODEL_SCHEMA_VERSION
        assert model["featureSetVersion"] == FEATURE_SET_VERSION
        assert model["dataFingerprint"] == first
        save_model(model, model_path)

        status = model_status(model_path)
        assert status["state"] == "ok" and not status["reasons"]
        assert status["summary"]["schemaVersion"] == MODEL_SCHEMA_VERSION
        assert load_model(model_path) is not None

        # Accuracy alone hides miscalibration: report predicted vs realized per
        # bucket, plus Brier and ECE, and attach it to the status when a DB is
        # available.
        report = calibration_report(model, db_path)
        assert report["available"] is True
        assert report["samples"] == 30
        assert abs(report["baseRate"] - model["positives"] / model["samples"]) < 1e-3
        assert 0.0 <= report["brier"] <= 1.0
        assert 0.0 <= report["expectedCalibrationError"] <= 1.0
        assert sum(bucket["count"] for bucket in report["buckets"]) == 30
        for bucket in report["buckets"]:
            assert 0.0 <= bucket["avgPredicted"] <= 1.0
            assert 0.0 <= bucket["observed"] <= 1.0
            assert abs(bucket["gap"] - (bucket["observed"] - bucket["avgPredicted"])) < 2e-4
        assert calibration_report(None, db_path)["available"] is False
        clamped = calibration_report(model, db_path, buckets=500)
        assert clamped["available"] is True and len(clamped["buckets"]) <= 20
        status_with_db = model_status(model_path, db_path=db_path)
        assert status_with_db["summary"]["calibration"]["available"] is True

        # Any contract bump (e.g. the shared cost model) invalidates the file.
        stale = dict(model, costModelVersion="1")
        Path(model_path).write_text(json.dumps(stale), encoding="utf-8")
        assert load_model(model_path) is None
        stale_status = model_status(model_path)
        assert stale_status["state"] == "stale"
        assert any("cost model" in reason for reason in stale_status["reasons"])

        retrained = train_if_stale(db_path, model_path)
        assert retrained["trained"] is True and retrained["retrained"] is True
        assert model_status(model_path)["state"] == "ok"
        assert train_if_stale(db_path, model_path)["retrained"] is False
    finally:
        cleanup_db(db_path)
        try:
            os.remove(model_path)
        except OSError:
            pass
    print("  v11 ML model contract: OK")


def test_v12_walk_forward_optimizer():
    import json
    import math

    from bettercalltrader.ohlcv import BacktestConfig, make_strategy, run_backtest
    from bettercalltrader.optimizer import (
        OBJECTIVES,
        default_grid,
        grid_combinations,
        optimize_walk_forward,
        parse_grid,
        render_report,
    )

    closes = []
    price = 1.0
    for index in range(600):
        price *= 1.0 + 0.02 * math.sin(index / 7.0) + 0.004 * math.sin(index / 31.0)
        closes.append(max(0.05, price))
    candles = _synthetic_candles(closes)
    config = BacktestConfig(initial_sol=10.0, trade_size_sol=0.5)

    # Parsing and grid hygiene
    assert parse_grid("5, 9,,13", int) == [5, 9, 13]
    assert parse_grid("", int) == []
    assert parse_grid(None, int) == []
    try:
        parse_grid("5,oops", int)
        raise AssertionError("bad grid value should raise")
    except ValueError:
        pass

    grid = default_grid("momentum")
    combos = grid_combinations("momentum", grid)
    assert combos, "default momentum grid must produce combinations"
    assert all(item["slow"] > item["fast"] for item in combos), combos
    assert len({tuple(sorted(item.items())) for item in combos}) == len(combos)
    assert grid_combinations("momentum", {"fast": [], "slow": []}) == []

    report = optimize_walk_forward(
        candles,
        "momentum",
        grid,
        config,
        segments=3,
        objective="pnl",
        min_trades=1,
        baseline_params={"fast": 9, "slow": 21},
    )
    again = optimize_walk_forward(
        candles,
        "momentum",
        grid,
        config,
        segments=3,
        objective="pnl",
        min_trades=1,
        baseline_params={"fast": 9, "slow": 21},
    )
    assert json.dumps(report, sort_keys=True, default=str) == json.dumps(again, sort_keys=True, default=str), (
        "optimization must be deterministic"
    )
    assert report["objective"] == "pnl"
    assert report["combinations"] == len(combos)
    assert len(report["segments"]) == 3
    eligible_params = {tuple(sorted(item.items())) for item in combos}
    for segment in report["segments"]:
        if segment.get("skipped"):
            continue
        chosen = tuple(sorted(segment["bestParams"].items()))
        assert chosen in eligible_params, segment["bestParams"]

    # Out-of-sample aggregate is exactly the sum of the traded test slices.
    traded = [item for item in report["segments"] if not item.get("skipped")]
    assert traded, "expected at least one traded segment"
    assert report["aggregate"]["trades"] == sum(item["test"]["trades"] for item in traded)
    assert abs(report["aggregate"]["netPnlSol"] - sum(item["testPnlSol"] for item in traded)) < 1e-9
    assert report["aggregate"]["segments"] == len(traded)
    assert "baseline" in report and "upliftSol" in report
    assert abs(
        report["upliftSol"]
        - (report["aggregate"]["netPnlSol"] - report["baseline"]["summary"]["netPnlSol"])
    ) < 1e-9
    assert report["baseline"]["params"] == {"fast": 9, "slow": 21}

    # No candidate reaches the trade floor: every segment is skipped, not filled.
    strict = optimize_walk_forward(
        candles, "momentum", grid, config, segments=3, min_trades=10_000, baseline_params={"fast": 9, "slow": 21}
    )
    assert all(item.get("skipped") for item in strict["segments"])
    assert strict["aggregate"]["trades"] == 0
    assert strict["aggregate"]["skippedSegments"] == 3

    # Breakdowns are loud, never silent.
    try:
        optimize_walk_forward(candles, "momentum", {}, config)
        raise AssertionError("empty grid should raise")
    except ValueError:
        pass
    try:
        optimize_walk_forward(candles, "momentum", grid, config, objective="nonsense")
        raise AssertionError("bad objective should raise")
    except ValueError:
        pass
    try:
        optimize_walk_forward(candles, "nope", {"x": [1]}, config)
        raise AssertionError("unknown strategy should raise")
    except ValueError:
        pass
    assert set(OBJECTIVES) >= {"pnl", "return_pct", "profit_factor", "expectancy", "sharpe"}

    # Each objective is usable and each breakout strategy is covered by the CLI path.
    for objective in OBJECTIVES:
        assert "aggregate" in optimize_walk_forward(
            candles, "momentum", grid, config, segments=2, objective=objective, min_trades=0
        )
    breakout_grid = default_grid("breakout")
    breakout = optimize_walk_forward(candles, "breakout", breakout_grid, config, segments=2, min_trades=0)
    assert breakout["combinations"] == len(grid_combinations("breakout", breakout_grid))

    markdown = render_report(report)
    assert "Walk-forward optimization" in markdown
    assert "Out-of-sample aggregate" in markdown
    assert "Uplift over baseline" in markdown

    # The CLI shares the same machinery; --optimize wins over --walk-forward.
    from bettercalltrader import ohlcv as ohlcv_module

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(
            [
                {
                    "ts": candle.ts,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                }
                for candle in candles
            ],
            handle,
        )
        candle_path = handle.name
    try:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ohlcv_module.main(
                [
                    "--json",
                    candle_path,
                    "--optimize",
                    "--walk-forward",
                    "2",
                    "--min-trades",
                    "0",
                    "--objective",
                    "sharpe",
                    "--output",
                    "json",
                ]
            )
        assert code == 0
        payload = json.loads(buffer.getvalue())
        assert payload["optimization"]["objective"] == "sharpe"
        assert payload["optimization"]["segmentCount"] == 2
    finally:
        try:
            os.remove(candle_path)
        except OSError:
            pass

    # Sanity: the baseline runner really is the unoptimized strategy.
    direct = run_backtest(candles[:200], make_strategy("momentum", fast=9, slow=21), config)
    assert direct.summary["trades"] >= 0
    print("  v12 walk-forward optimizer: OK")


def test_v12_cost_stress():
    import json
    import math

    from bettercalltrader.cost_stress import (
        DEFAULT_MULTIPLIERS,
        parse_multipliers,
        render_report,
        scaled_config,
        stress_costs,
    )
    from bettercalltrader.ohlcv import BacktestConfig, EmaCrossStrategy, run_backtest

    class _NoTradeStrategy:
        name = "no_trade"

        def generate(self, history):
            return None

    # Multiplier parsing is sorted, de-duplicated and fail-closed.
    assert parse_multipliers("") == list(DEFAULT_MULTIPLIERS)
    assert parse_multipliers("2,0.5, 1,1,0") == [0.0, 0.5, 1.0, 2.0]
    for bad in ("abc", "-1", "nan", "inf", ","):
        try:
            parse_multipliers(bad)
            raise AssertionError(f"multiplier {bad!r} should raise")
        except ValueError:
            pass

    # The scaled config touches only cost knobs.
    base = BacktestConfig(
        initial_sol=10.0,
        trade_size_sol=0.5,
        fee_bps=25.0,
        slippage_bps=50.0,
        exit_slippage_bps=80.0,
        impact_coeff=0.5,
        priority_fee_sol=0.0015,
        failed_tx_prob=0.4,
        liquidity_usd=25_000.0,
    )
    doubled = scaled_config(base, 2.0)
    assert doubled.fee_bps == 50.0
    assert doubled.slippage_bps == 100.0
    assert doubled.exit_slippage_bps == 160.0
    assert doubled.impact_coeff == 1.0
    assert doubled.priority_fee_sol == 0.003
    assert doubled.failed_tx_prob == 0.8
    assert doubled.trade_size_sol == base.trade_size_sol
    assert scaled_config(base, 10.0).failed_tx_prob == 1.0
    try:
        scaled_config(base, -1.0)
        raise AssertionError("negative multiplier should raise")
    except ValueError:
        pass

    closes = []
    price = 1.0
    for index in range(600):
        price *= 1.0 + 0.012 * math.sin(index / 6.0) + 0.0015
        closes.append(max(0.05, price))
    candles = _synthetic_candles(closes)
    strategy = EmaCrossStrategy(fast=5, slow=13)
    base_config = BacktestConfig(
        initial_sol=10.0,
        trade_size_sol=1.0,
        fee_bps=200.0,
        slippage_bps=100.0,
        exit_slippage_bps=200.0,
    )

    report = stress_costs(candles, EmaCrossStrategy(fast=5, slow=13), base_config)
    again = stress_costs(candles, EmaCrossStrategy(fast=5, slow=13), base_config)
    assert json.dumps(report, sort_keys=True, default=str) == json.dumps(again, sort_keys=True, default=str)

    assert report["multipliers"] == list(DEFAULT_MULTIPLIERS)
    assert [point["multiplier"] for point in report["points"]] == list(DEFAULT_MULTIPLIERS)
    free = report["points"][0]
    assert free["feeBps"] == 0.0 and free["priorityFeeSol"] == 0.0
    assert free["feesPaidSol"] == 0.0 and abs(free["costsPaidSol"]) < 1e-12
    assert abs(report["grossEdgeSol"] - free["netPnlSol"]) < 1e-12

    # The baseline point is exactly a normal backtest with the original config.
    direct = run_backtest(candles, EmaCrossStrategy(fast=5, slow=13), base_config)
    assert abs(report["baseline"]["netPnlSol"] - direct.summary["netPnlSol"]) < 1e-12
    assert report["baseline"]["trades"] == direct.summary["trades"]

    # Costs rise and the edge shrinks across the sweep.
    assert report["points"][-1]["costsPaidSol"] > free["costsPaidSol"]
    assert report["points"][-1]["netPnlSol"] < free["netPnlSol"]

    # Break-even is interpolated inside the crossing bracket.
    assert report["status"] == "break-even", report["status"]
    break_even = report["breakEvenMultiplier"]
    assert break_even is not None and 0.0 < break_even < 10.0
    points = report["points"]
    crossing = next(
        index for index, point in enumerate(points) if float(point["netPnlSol"]) <= 0.0
    )
    assert points[crossing - 1]["multiplier"] <= break_even <= points[crossing]["multiplier"]
    assert report["headroomPct"] is not None
    assert abs(report["headroomPct"] - (break_even - 1.0) * 100.0) < 1e-9

    # A costlier baseline pushes break-even below 1.0: no headroom left.
    stressed_config = BacktestConfig(
        initial_sol=10.0,
        trade_size_sol=1.0,
        fee_bps=400.0,
        slippage_bps=200.0,
        exit_slippage_bps=400.0,
    )
    stressed = stress_costs(candles, EmaCrossStrategy(fast=5, slow=13), stressed_config)
    assert stressed["status"] == "break-even"
    assert stressed["breakEvenMultiplier"] is not None and stressed["breakEvenMultiplier"] < 1.0
    assert stressed["headroomPct"] is not None and stressed["headroomPct"] < 0
    assert stressed["baseline"]["netPnlSol"] < 0.0

    # Default multipliers can themselves be overridden and a custom baseline honoured.
    custom = stress_costs(
        candles,
        EmaCrossStrategy(fast=5, slow=13),
        base_config,
        multipliers=[0.0, 1.0, 2.0],
        baseline_multiplier=2.0,
    )
    assert custom["multipliers"] == [0.0, 1.0, 2.0]
    assert custom["baseline"]["multiplier"] == 2.0
    assert custom["breakEvenMultiplier"] is not None
    assert (
        abs(custom["headroomPct"] - (custom["breakEvenMultiplier"] / 2.0 - 1.0) * 100.0) < 1e-9
    )
    try:
        stress_costs(candles, strategy, base_config, multipliers=[0.0, 0.5], baseline_multiplier=1.0)
        raise AssertionError("baseline outside the sweep should raise")
    except ValueError:
        pass

    # A sweep that never visits 0x cannot claim a gross edge.
    no_zero = stress_costs(
        candles, EmaCrossStrategy(fast=5, slow=13), base_config, multipliers=[0.5, 1.0], baseline_multiplier=1.0
    )
    assert no_zero["grossEdgeSol"] is None

    # A strategy with no gross edge has no break-even to report.
    flat = stress_costs(candles, _NoTradeStrategy(), base_config)
    assert flat["status"] == "unprofitable-at-zero-cost"
    assert flat["breakEvenMultiplier"] is None and flat["headroomPct"] is None
    assert flat["grossEdgeSol"] == 0.0

    # Zero-configured costs make every point identical; the edge never breaks.
    free_config = BacktestConfig(
        initial_sol=10.0,
        trade_size_sol=1.0,
        fee_bps=0.0,
        slippage_bps=0.0,
        exit_slippage_bps=0.0,
        priority_fee_sol=0.0,
        failed_tx_prob=0.0,
    )
    never = stress_costs(candles, EmaCrossStrategy(fast=5, slow=13), free_config)
    assert never["status"] == "profitable-at-max-multiplier"
    assert never["breakEvenMultiplier"] is None
    assert len({round(point["netPnlSol"], 12) for point in never["points"]}) == 1

    markdown = render_report(report)
    assert "Cost stress" in markdown
    assert "Verdict" in markdown
    assert "break-even" in markdown.lower()

    # The CLI exposes the sweep, refuses bad multipliers and incompatible flags.
    from bettercalltrader import ohlcv as ohlcv_module

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(
            [
                {
                    "ts": candle.ts,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                }
                for candle in candles
            ],
            handle,
        )
        candle_path = handle.name
    try:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ohlcv_module.main(
                ["--json", candle_path, "--stress-costs", "--cost-multipliers", "0,1,2", "--output", "json"]
            )
        assert code == 0
        payload = json.loads(buffer.getvalue())
        assert payload["costStress"]["multipliers"] == [0.0, 1.0, 2.0]

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            assert ohlcv_module.main(["--json", candle_path, "--stress-costs", "--cost-multipliers", "x"]) == 2
        assert "invalid cost multiplier" in stderr.getvalue()

        with contextlib.redirect_stderr(io.StringIO()):
            assert ohlcv_module.main(["--json", candle_path, "--stress-costs", "--walk-forward", "2"]) == 2

        try:
            with contextlib.redirect_stderr(io.StringIO()):
                ohlcv_module.main(["--json", candle_path, "--stress-costs", "--optimize"])
            raise AssertionError("--stress-costs and --optimize are mutually exclusive")
        except SystemExit as exit_error:
            assert exit_error.code == 2
    finally:
        try:
            os.remove(candle_path)
        except OSError:
            pass

    print("  v12 cost stress and break-even: OK")


def test_v12_rolling_windows():
    import json
    import math

    from bettercalltrader.ohlcv import BacktestConfig
    from bettercalltrader.optimizer import (
        WINDOW_MODES,
        default_grid,
        optimize_walk_forward,
        render_report,
        window_ranges,
    )

    # Fixed mode keeps the original block split.
    fixed = window_ranges(600, "fixed", segments=3, train_ratio=0.6)
    assert len(fixed) == 3
    assert fixed[0] == {
        "index": 1,
        "trainStart": 0,
        "trainEnd": 120,
        "testStart": 120,
        "testEnd": 200,
    }
    assert fixed[-1]["testEnd"] == 600

    # Anchored: train always starts at zero and grows; tests are contiguous.
    anchored = window_ranges(600, "anchored", train_candles=240, test_candles=90)
    assert len(anchored) == 4
    assert all(window["trainStart"] == 0 for window in anchored)
    assert [window["trainEnd"] - window["trainStart"] for window in anchored] == [240, 330, 420, 510]
    assert anchored[0]["testStart"] == 240
    for left, right in zip(anchored, anchored[1:]):
        assert left["testEnd"] == right["testStart"]
    assert anchored[-1]["testEnd"] == 600

    # Rolling: constant train size, both windows slide forward.
    rolling = window_ranges(600, "rolling", train_candles=240, test_candles=90)
    assert len(rolling) == 4
    assert all(window["trainEnd"] - window["trainStart"] == 240 for window in rolling)
    assert [window["trainStart"] for window in rolling] == [0, 90, 180, 270]
    assert all(window["trainEnd"] == window["testStart"] for window in rolling)

    # Auto sizes only need the series length.
    auto = window_ranges(600, "rolling")
    assert auto and auto[0]["trainEnd"] - auto[0]["trainStart"] == 200
    assert auto[0]["testEnd"] - auto[0]["testStart"] == 75

    for bad_mode in ("nope", "sideways"):
        try:
            window_ranges(600, bad_mode)
            raise AssertionError("bad window mode should raise")
        except ValueError:
            pass
    # An empty mode is normalised to the default rather than rejected.
    assert window_ranges(600, "") == window_ranges(600, "fixed")
    for kwargs in (
        {"train_candles": 600, "test_candles": 90},
        {"train_candles": 1, "test_candles": 90},
        {"train_candles": 240, "test_candles": 5},
    ):
        try:
            window_ranges(600, "rolling", **kwargs)
            raise AssertionError(f"{kwargs} should raise")
        except ValueError:
            pass

    # Walk-forward data: 600 deterministic candles with a tradable rhythm.
    closes = []
    price = 1.0
    for index in range(600):
        price *= 1.0 + 0.012 * math.sin(index / 6.0) + 0.0015
        closes.append(max(0.05, price))
    candles = _synthetic_candles(closes)
    config = BacktestConfig(initial_sol=10.0, trade_size_sol=0.5)
    grid = default_grid("momentum")

    reports = {}
    for mode in ("anchored", "rolling"):
        report = optimize_walk_forward(
            candles,
            "momentum",
            grid,
            config,
            window_mode=mode,
            train_candles=240,
            test_candles=90,
            min_trades=1,
            baseline_params={"fast": 9, "slow": 21},
        )
        again = optimize_walk_forward(
            candles,
            "momentum",
            grid,
            config,
            window_mode=mode,
            train_candles=240,
            test_candles=90,
            min_trades=1,
            baseline_params={"fast": 9, "slow": 21},
        )
        assert json.dumps(report, sort_keys=True, default=str) == json.dumps(
            again, sort_keys=True, default=str
        )
        reports[mode] = report
        assert report["windowMode"] == mode
        assert report["windowCount"] == len(
            window_ranges(600, mode, train_candles=240, test_candles=90)
        )
        assert report["windowCount"] == len(report["segments"])
        traded = [item for item in report["segments"] if not item.get("skipped")]
        assert traded
        assert abs(
            report["aggregate"]["netPnlSol"] - sum(item["testPnlSol"] for item in traded)
        ) < 1e-9

        stability = report["stability"]
        assert stability["windows"] == len(traded)
        assert stability["profitableWindows"] == sum(
            1 for item in traded if item["testPnlSol"] > 0
        )
        shares = [entry["sharePct"] for entry in stability["paramSets"]]
        assert abs(sum(shares) - 100.0) < 1e-9
        assert shares == sorted(shares, reverse=True)
        assert stability["bestWindowPnlSol"] >= stability["medianWindowPnlSol"] >= stability[
            "worstWindowPnlSol"
        ]
        assert stability["consistency"] in ("stable", "mixed", "unstable")
        assert stability["dominantSharePct"] >= shares[0]

    # The anchored and rolling modes really build different windows.
    anchored_starts = [item["trainStartTs"] for item in reports["anchored"]["segments"]]
    rolling_starts = [item["trainStartTs"] for item in reports["rolling"]["segments"]]
    assert len(set(anchored_starts)) == 1
    assert len(set(rolling_starts)) == len(rolling_starts)

    # No eligible candidate anywhere: stability has nothing to score.
    flat = optimize_walk_forward(
        candles,
        "momentum",
        grid,
        config,
        window_mode="anchored",
        train_candles=240,
        test_candles=90,
        min_trades=10_000,
        baseline_params={"fast": 9, "slow": 21},
    )
    assert flat["stability"]["windows"] == 0
    assert flat["stability"]["paramSets"] == []
    assert flat["stability"]["consistency"] == "unstable"
    assert flat["aggregate"]["trades"] == 0

    # Too little test data per window is refused instead of silently empty.
    try:
        optimize_walk_forward(
            candles[:120],
            "momentum",
            grid,
            config,
            window_mode="rolling",
            train_candles=115,
            test_candles=20,
        )
        raise AssertionError("all-short windows should raise")
    except ValueError:
        pass

    markdown = render_report(reports["rolling"])
    assert "Window mode: `rolling`" in markdown
    assert "## Stability" in markdown
    assert "Consistency:" in markdown

    # CLI: the mode flags reach the optimizer and are rejected elsewhere.
    from bettercalltrader import ohlcv as ohlcv_module

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(
            [
                {
                    "ts": candle.ts,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                }
                for candle in candles
            ],
            handle,
        )
        candle_path = handle.name
    try:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ohlcv_module.main(
                [
                    "--json",
                    candle_path,
                    "--optimize",
                    "--window-mode",
                    "rolling",
                    "--train-candles",
                    "240",
                    "--test-candles",
                    "90",
                    "--min-trades",
                    "1",
                    "--output",
                    "json",
                ]
            )
        assert code == 0
        payload = json.loads(buffer.getvalue())
        assert payload["optimization"]["windowMode"] == "rolling"

        with contextlib.redirect_stderr(io.StringIO()):
            assert ohlcv_module.main(
                ["--json", candle_path, "--window-mode", "rolling"]
            ) == 2
        with contextlib.redirect_stderr(io.StringIO()):
            assert ohlcv_module.main(
                ["--json", candle_path, "--stress-costs", "--window-mode", "rolling"]
            ) == 2
        with contextlib.redirect_stderr(io.StringIO()):
            assert ohlcv_module.main(
                ["--json", candle_path, "--optimize", "--train-candles", "240"]
            ) == 2
        assert "fixed" in WINDOW_MODES and "anchored" in WINDOW_MODES
    finally:
        try:
            os.remove(candle_path)
        except OSError:
            pass

    print("  v12 rolling windows and stability: OK")


def test_v12_strategy_comparison():
    import json
    import math

    from bettercalltrader.compare import compare_strategies, default_strategies, render_report
    from bettercalltrader.ohlcv import BacktestConfig

    closes = []
    price = 1.0
    for index in range(600):
        price *= 1.0 + 0.012 * math.sin(index / 6.0) + 0.0015
        closes.append(max(0.05, price))
    candles = _synthetic_candles(closes)
    config = BacktestConfig(initial_sol=10.0, trade_size_sol=0.5)
    specs = default_strategies()
    assert [spec["name"] for spec in specs] == ["momentum", "breakout"]

    report = compare_strategies(
        candles,
        specs,
        config,
        objective="pnl",
        segments=2,
        min_trades=1,
        multipliers=[0.0, 1.0, 2.0],
    )
    again = compare_strategies(
        candles,
        specs,
        config,
        objective="pnl",
        segments=2,
        min_trades=1,
        multipliers=[0.0, 1.0, 2.0],
    )
    assert json.dumps(report, sort_keys=True, default=str) == json.dumps(
        again, sort_keys=True, default=str
    )

    assert report["candles"] == 600
    assert report["windowMode"] == "fixed"
    assert len(report["strategies"]) == 2
    assert sorted(report["ranking"]) == ["breakout", "momentum"]
    assert report["winner"] == report["ranking"][0]

    for entry in report["strategies"]:
        assert set(entry["aggregate"]) >= {"trades", "netPnlSol", "returnPct", "profitFactor"}
        assert entry["stability"]["windows"] >= 0
        assert entry["stability"]["consistency"] in ("stable", "mixed", "unstable")
        assert entry["costStress"]["status"] in (
            "break-even",
            "unprofitable-at-zero-cost",
            "unprofitable-throughout",
            "profitable-at-max-multiplier",
        )
        assert isinstance(entry["upliftSol"], float)
        assert "score" in entry and math.isfinite(entry["score"])

    # Ranking follows the documented key: score desc, then break-even desc.
    scores = [entry["score"] for entry in report["strategies"]]
    ranked_scores = [
        next(entry["score"] for entry in report["strategies"] if entry["strategy"] == name)
        for name in report["ranking"]
    ]
    assert ranked_scores == sorted(scores, reverse=True)

    # One strategy, rolling windows, and objective pass-through.
    single = compare_strategies(
        candles,
        [specs[0]],
        config,
        objective="sharpe",
        window_mode="rolling",
        train_candles=240,
        test_candles=90,
        min_trades=1,
    )
    assert single["ranking"] == ["momentum"]
    assert single["winner"] == "momentum"
    assert single["objective"] == "sharpe"
    assert single["windowMode"] == "rolling"
    assert single["trainCandles"] == 240 and single["testCandles"] == 90

    # Window mode is not cosmetic. On a regime-shifting series (300 trending
    # candles then 300 oscillating ones) the same strategies must score
    # differently when the train/test windows move, or `--window-mode rolling`
    # would be a label with no effect.
    shift_closes = []
    shift_price = 1.0
    for _ in range(300):
        shift_price *= 1.006
        shift_closes.append(shift_price)
    for index in range(300):
        shift_price *= 1.0 + 0.02 * math.sin(index / 4.0)
        shift_closes.append(max(0.05, shift_price))
    shift_candles = _synthetic_candles(shift_closes)
    regime_args = dict(
        objective="pnl",
        min_trades=1,
        multipliers=[0.0, 1.0, 2.0],
    )
    fixed_regime = compare_strategies(shift_candles, specs, config, segments=2, **regime_args)
    rolling_regime = compare_strategies(
        shift_candles,
        specs,
        config,
        window_mode="rolling",
        train_candles=150,
        test_candles=150,
        **regime_args,
    )
    fixed_pnl = {entry["strategy"]: round(entry["aggregate"]["netPnlSol"], 9) for entry in fixed_regime["strategies"]}
    rolling_pnl = {entry["strategy"]: round(entry["aggregate"]["netPnlSol"], 9) for entry in rolling_regime["strategies"]}
    assert fixed_pnl != rolling_pnl, (fixed_pnl, rolling_pnl)
    assert fixed_regime["windowMode"] == "fixed" and rolling_regime["windowMode"] == "rolling"
    rolling_again = compare_strategies(
        shift_candles,
        specs,
        config,
        window_mode="rolling",
        train_candles=150,
        test_candles=150,
        **regime_args,
    )
    assert json.dumps(rolling_regime, sort_keys=True, default=str) == json.dumps(
        rolling_again, sort_keys=True, default=str
    )

    # Fail loudly on unusable inputs.
    for call in (
        lambda: compare_strategies(candles, [], config),
        lambda: compare_strategies([], specs, config),
        lambda: compare_strategies(candles, specs, config, objective="bogus"),
        lambda: compare_strategies(candles, specs, config, window_mode="sideways"),
        lambda: compare_strategies(candles, [{"name": "nope", "params": {}, "grid": {"x": [1]}}], config),
        lambda: compare_strategies(candles, [{"name": "momentum", "params": {}, "grid": {}}], config),
    ):
        try:
            call()
            raise AssertionError("invalid comparison input should raise")
        except ValueError:
            pass

    markdown = render_report(report)
    assert "# Strategy comparison" in markdown
    assert "Winner:" in markdown
    assert "| Rank |" in markdown
    assert "break-even" in markdown.lower()
    for name in report["ranking"]:
        assert name in markdown

    # CLI: --compare builds the specs, reports JSON and rejects bad inputs.
    from bettercalltrader import ohlcv as ohlcv_module

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(
            [
                {
                    "ts": candle.ts,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                }
                for candle in candles
            ],
            handle,
        )
        candle_path = handle.name
    try:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ohlcv_module.main(
                [
                    "--json",
                    candle_path,
                    "--compare",
                    "--compare-strategies",
                    "momentum,breakout",
                    "--min-trades",
                    "1",
                    "--output",
                    "json",
                ]
            )
        assert code == 0
        payload = json.loads(buffer.getvalue())
        assert set(payload["comparison"]["ranking"]) == {"momentum", "breakout"}

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            assert ohlcv_module.main(
                ["--json", candle_path, "--compare", "--compare-strategies", "nope"]
            ) == 2
        assert "unknown strategy" in stderr.getvalue()

        try:
            with contextlib.redirect_stderr(io.StringIO()):
                ohlcv_module.main(["--json", candle_path, "--compare", "--optimize"])
            raise AssertionError("--compare and --optimize are mutually exclusive")
        except SystemExit as exit_error:
            assert exit_error.code == 2
    finally:
        try:
            os.remove(candle_path)
        except OSError:
            pass

    print("  v12 strategy comparison: OK")


def test_v12_live_readiness():
    import contextlib
    import io
    import json
    import unittest.mock

    from bettercalltrader import doctor as doctor_module
    from bettercalltrader import live_readiness as lr

    # Paper defaults are unambiguous: no fails and no private key reported.
    paper = lr.check_environment({})
    assert paper["mode"] == "paper"
    assert paper["liveTradingEnabled"] is False
    assert paper["liveTradingConfirmed"] is False
    assert paper["fails"] == 0
    assert paper["privateKeyPresent"] is False

    # Unset booleans are defaults, not parse errors; typos are fails.
    assert lr.check_environment({"LIVE_TRADING_ENABLED": ""})["fails"] == 0
    typo = lr.check_environment({"LIVE_TRADING_ENABLED": "ture"})
    assert typo["mode"] == "paper" and typo["fails"] == 1
    assert any(issue["key"] == "LIVE_TRADING_ENABLED" for issue in typo["issues"])

    live_env = {
        "LIVE_TRADING_ENABLED": "true",
        "LIVE_TRADING_CONFIRMED": "true",
        "SOLANA_PRIVATE_KEY": "SECRET_SENTINEL_VALUE_DO_NOT_LEAK",
        "MAX_LIVE_TRADE_SOL": "0.05",
        "SOLANA_RPC_URL": "http://127.0.0.1:8899",
    }
    live = lr.check_environment(live_env, solders_available=True)
    assert live["mode"] == "live"
    assert live["fails"] == 0
    assert live["privateKeyPresent"] is True
    assert live["rpcLoopback"] is True

    # A live config without the optional signing package is a hard fail.
    missing_signer = lr.check_environment(live_env, solders_available=False)
    assert missing_signer["fails"] == 1
    assert any(issue["key"] == "solders" and issue["level"] == "fail" for issue in missing_signer["issues"])

    # The key value must never appear in the report.
    assert "SECRET_SENTINEL_VALUE_DO_NOT_LEAK" not in json.dumps(live)
    assert "SECRET_SENTINEL_VALUE_DO_NOT_LEAK" not in lr.render_report(
        {"status": lr.STATUS_READY, "environment": live, "guards": {"scenarios": []}}
    )

    for broken, key in (
        ({"LIVE_TRADING_ENABLED": "true", "LIVE_TRADING_CONFIRMED": "false"}, "LIVE_TRADING_CONFIRMED"),
        ({"LIVE_TRADING_ENABLED": "true", "LIVE_TRADING_CONFIRMED": "true"}, "SOLANA_PRIVATE_KEY"),
        (
            {
                "LIVE_TRADING_ENABLED": "true",
                "LIVE_TRADING_CONFIRMED": "true",
                "SOLANA_PRIVATE_KEY": "x",
                "MAX_LIVE_TRADE_SOL": "0",
            },
            "MAX_LIVE_TRADE_SOL",
        ),
    ):
        report = lr.check_environment(broken)
        assert report["fails"] >= 1
        assert any(issue["key"] == key and issue["level"] == "fail" for issue in report["issues"])

    # The guard harness drives the real execute_buy with a session that refuses
    # every request, so it must pass with zero network calls.
    before_audit = rb.AUDIT_LOG_PATH
    guards = lr.audit_guards()
    assert rb.AUDIT_LOG_PATH == before_audit, "readiness must restore the audit path"
    assert guards["ok"] is True
    assert guards["failed"] == 0
    assert guards["networkCalls"] == 0
    observed = {item["name"]: item["observed"] for item in guards["scenarios"]}
    assert observed == {
        "paper_pumpfun": "DRY_RUN_READY",
        "live_unconfirmed": "BLOCKED",
        "live_over_max": "BLOCKED",
        "live_missing_key": "ERROR",
        "live_dry_run_stage": "DRY_RUN_STAGED",
        "live_probe_exhausted": "BLOCKED",
        "live_invalid_stage": "BLOCKED",
    }
    for item in guards["scenarios"]:
        assert item["networkCalls"] == 0
        assert item["passed"] is True

    # Status matrix.
    assert lr.run_readiness({})["status"] == lr.STATUS_PAPER
    assert (
        lr.run_readiness(
            {"LIVE_TRADING_ENABLED": "true", "LIVE_TRADING_CONFIRMED": "false"}
        )["status"]
        == lr.STATUS_BLOCKED
    )
    ready = lr.run_readiness(dict(live_env), solders_available=True)
    assert ready["status"] == lr.STATUS_READY
    assert lr.run_readiness(dict(live_env), solders_available=False)["status"] == lr.STATUS_BLOCKED
    assert ready["probes"] is None and ready["networkUsed"] is False
    assert ready["guards"]["networkCalls"] == 0

    markdown = lr.render_report(lr.run_readiness({}))
    assert "# Live-execution readiness" in markdown
    assert "**PAPER**" in markdown
    assert "## Guard scenarios" in markdown
    assert "7 passed, 0 failed, 0 network call(s)." in markdown

    # The execution agent accepts explicit guard values, and an explicit empty
    # key no longer falls back to the process environment.
    with unittest.mock.patch.dict(
        os.environ, {"SOLANA_PRIVATE_KEY": "ENV_SENTINEL_VALUE"}, clear=False
    ):
        agent = rb.SolanaExecutionAgent(
            private_key_b58="",
            live_trading_enabled=True,
            live_trading_confirmed=False,
            max_live_trade_sol=0.01,
        )
        assert agent.private_key_b58 == ""
        assert agent.live_trading_confirmed is False
        assert agent.max_live_trade_sol == 0.01

    # Doctor reports the same mode without importing the bot or opening a socket.
    doctor_check = doctor_module.check_live_readiness({})
    assert doctor_check.name == "live_readiness"
    assert doctor_check.status == "info"
    assert doctor_module.check_live_readiness(
        {"LIVE_TRADING_ENABLED": "true", "LIVE_TRADING_CONFIRMED": "false"}
    ).status in ("warn", "fail")
    assert doctor_module.check_live_readiness(dict(live_env)).status == "warn"

    # CLI: PAPER exits 0, BLOCKED exits 1, and the JSON stays parseable (the
    # research_bot import banner must not leak into stdout).
    with unittest.mock.patch.dict(
        os.environ,
        {"LIVE_TRADING_ENABLED": "false", "LIVE_TRADING_CONFIRMED": "false"},
        clear=False,
    ):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = lr.main(["--json", "--env-file", "/nonexistent"])
        assert code == 0
        payload = json.loads(buffer.getvalue())
        assert payload["status"] == lr.STATUS_PAPER
        assert payload["guards"]["networkCalls"] == 0
        assert "SECRET_SENTINEL_VALUE" not in buffer.getvalue()

    with unittest.mock.patch.dict(
        os.environ,
        {"LIVE_TRADING_ENABLED": "true", "LIVE_TRADING_CONFIRMED": "false"},
        clear=False,
    ):
        with contextlib.redirect_stdout(io.StringIO()):
            assert lr.main(["--json", "--env-file", "/nonexistent"]) == 1

    with contextlib.redirect_stderr(io.StringIO()):
        try:
            lr.main(["--not-a-flag"])
            raise AssertionError("argparse must reject unknown flags")
        except SystemExit as exit_error:
            assert exit_error.code == 2

    print("  v12 live-execution readiness: OK")


def main():
    print("Running BetterCallTrader v13.0 self-tests...")
    run_audit_path = ROOT / "_audit_test_run.jsonl"
    original_audit_path = rb.AUDIT_LOG_PATH
    rb.AUDIT_LOG_PATH = str(run_audit_path)
    try:
        test_env_helpers()
        test_paper_fill()
        test_exit_icon()
        test_portfolio_basic()
        test_risk_snapshot_and_health_shape()
        test_confirmation_cancel()
        test_v12_pending_persistence()
        test_v12_pumpfun_curve_quote()
        test_v12_walk_forward_optimizer()
        test_v12_cost_stress()
        test_v12_rolling_windows()
        test_v12_strategy_comparison()
        test_v12_live_readiness()
        test_exit_rules()
        test_flow_exit()
        test_whale_exit()
        test_time_stop()
        test_circuit_breaker_trips()
        test_v10_schema_and_metrics()
        test_v10_risk_and_backtest()
        test_v10_security()
        test_v11_phase0_hardening()
        test_v10_observability()
        test_v10_strategy_profiles()
        test_v10_ml_advisory()
        test_v10_price_cache()
        test_v11_prometheus()
        test_v11_origin_policy()
        test_v11_shutdown_controller()
        test_v11_ohlcv()
        test_v11_cost_model()
        test_v11_backtest_exits()
        test_v11_partial_tp1()
        test_v11_depth_and_participation()
        test_v11_exit_policy_parity()
        test_v11_llm_layer()
        test_v12_llm_reliability()
        test_v12_export_training()
        test_v12_paper_live_parity()
        test_v12_totp()
        test_v12_telegram_digest()
        test_v12_staged_arming()
        test_v13_safety_batch()
        test_v13_eval_compare()
        test_v11_ml_contract()
        test_v11_cost_parity()
        test_v11_config_fingerprint()
        test_v11_config_validation()
        test_v11_doctor()
        test_v11_extracted_helpers()
    finally:
        rb.AUDIT_LOG_PATH = original_audit_path
        try:
            if run_audit_path.exists():
                os.remove(run_audit_path)
        except OSError:
            pass
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()