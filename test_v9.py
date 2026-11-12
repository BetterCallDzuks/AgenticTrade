import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import research_bot as rb


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
        assert cfg["version"] == "v10.0"
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
    db_path = make_temp_db()
    try:
        portfolio = rb.PaperTradingManager(db_path=db_path, initial_sol=10.0, trade_size_sol=0.5)
        portfolio.cluster_cooldown_sec = 0
        portfolio.stop_loss_cooldown_sec = 0
        portfolio.min_seconds_between_entries = 0
        portfolio.max_daily_stop_losses = 100
        portfolio.max_daily_trades = 100
        portfolio.whale_exit_enabled = True
        now_ts = portfolio.now_ts()
        mint = "WhaleMint11111111111111111111111111111111"
        market = {
            "mint": mint,
            "symbol": "WHALE",
            "name": "Whale Token",
            "price_usd": "1.0",
            "liquidity_usd": 100000.0,
            "dex_id": "raydium",
            "fetched_at": now_ts,
            "whale_signal": "SmartWhale",
            "whale_wallet": "Wallet1111111111111111111111111111111111",
        }
        assert portfolio.open_virtual_position(market, conviction="manual", source="TEST") is True
        rb.record_whale_sell(mint, "Wallet1111111111111111111111111111111111", "SmartWhale")
        tick_market = dict(market, fetched_at=portfolio.now_ts(), price_usd="1.0")
        action = portfolio.process_position_tick(mint, tick_market)
        assert action == "WHALE_EXIT", action
        print("  whale exit: OK")
    finally:
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
    from agentictrade.metrics import collect_basic_metrics
    from agentictrade.migrations import apply_migrations, CURRENT_SCHEMA_VERSION

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

        from agentictrade.observability import configure_file_logger
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
    from agentictrade.risk_analytics import herfindahl_index, summarize
    from agentictrade.backtest import build_report

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
    from agentictrade.security import RateLimiter, client_ip, is_authorized

    assert is_authorized(None, "") is True
    assert is_authorized("abc", "abc") is True
    assert is_authorized("abc", "xyz") is False
    assert is_authorized(None, "xyz") is False
    assert client_ip({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}, "9.9.9.9") == "1.2.3.4"
    assert client_ip({}, "9.9.9.9") == "9.9.9.9"

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


def test_v10_observability():
    import json
    import logging
    from agentictrade.observability import (
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
    from agentictrade.strategies import normalize_profile, profile_values, resolve_profile

    assert normalize_profile("AGGRESSIVE") == "aggressive"
    assert normalize_profile("nonsense") == "balanced"
    assert profile_values("conservative")["STOP_LOSS_PCT"] == -18.0
    assert resolve_profile({}, "aggressive")["stop_loss_pct"] == -35.0
    assert "stop_loss_pct" not in resolve_profile({"STOP_LOSS_PCT": "-10"}, "aggressive")

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
    from agentictrade.ml import (
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
    from agentictrade.price_feed import PriceCache

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


def main():
    print("Running AgenticTrade v10.0 self-tests...")
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
        test_exit_rules()
        test_flow_exit()
        test_whale_exit()
        test_time_stop()
        test_circuit_breaker_trips()
        test_v10_schema_and_metrics()
        test_v10_risk_and_backtest()
        test_v10_security()
        test_v10_observability()
        test_v10_strategy_profiles()
        test_v10_ml_advisory()
        test_v10_price_cache()
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