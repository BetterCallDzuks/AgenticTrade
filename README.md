# Solana Memecoin Multi-Agent Trading Bot: AgenticTrade v11.0

[![CI](https://github.com/BetterCallDzuks/AgenticTrade/actions/workflows/tests.yml/badge.svg)](https://github.com/BetterCallDzuks/AgenticTrade/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-11.0.0-e3a452.svg)](https://github.com/BetterCallDzuks/AgenticTrade)

A multi-agent system for discovering, checking and **paper-trading** Solana memecoins. It combines the Pump.fun WebSocket, the DexScreener API, RugCheck security checks, an optional Google Gemini analyst, a SQLite paper portfolio and a Jupiter/Jito dry-run executor, all behind a local web Command Center and an analytics dashboard.

> **Paper trading only by default.** `LIVE_TRADING_ENABLED=false` stays off, and nothing in v10 turns it on (the dashboard cannot either). This software does not promise or predict profit. Memecoins are extremely risky. Replay and paper results are a way to test rules, not a forecast.

---

## What's in v9.0 (still active)

v9.0 keeps the v8 risk engine and adds time-based and step-up protection plus Telegram control:

- **Time-Stop**: closes a position if it has not reached the configured minimum profit after the configured hold time.
- **Step-Up Trailing**: raises the stop floor at configured profit milestones before TP1.
- **Telegram notifications and commands**: `/status`, `/sell_all`, `/reset_breaker`, `/help`.
- **Mint/freeze authority fix**: RugCheck `mintAuthority: null` is correctly treated as revoked.
- **v8 risk engine retained**: flow exits, whale exits, circuit breaker, confirmation before entry, realistic paper fills, dynamic sizing, cluster guard, wallet quality, decision journal, equity curve, stale-price protection, breakeven after TP1.

### Exit order (checked every monitor tick, 4s)

1. **WHALE_EXIT** 🐳: the entry whale sold this token.
2. **TAKE_PROFIT_1** 🎯: +80% from the entry fill sells 50%.
3. **FAST_INVALIDATION** ⚡: -12% within the first 90s.
4. **STOP_LOSS** 🛑: -25%.
5. **TIME_STOP** ⏱️: no configured pump after the configured hold time.
6. **FLOW_EXIT** 💧: fresh flow, sells >= 3, sells > buys, and sells >= buys x 1.3.
7. **TRAILING_STOP** 📉 / **BREAKEVEN_STOP** 🛡️ / **STEP_UP_STOP** 🪜: stop = max(trailing stop, breakeven floor, step-up floors).

---

## Install / run (Windows PowerShell)

1. Back up `paper_portfolio.db` and `.env` if you already have them.
2. Install or update packages:

```powershell
cd C:\Users\LiveTony\Documents\AgenticTrade
pip install aiohttp pydantic python-dotenv tzdata google-genai
```

3. Create or update `.env` from `.env.example`. Keep `LIVE_TRADING_ENABLED=false`.
4. Run the bot and open the dashboard:

```powershell
python research_bot.py
```

Open **http://localhost:8080**. The header shows **v9.0**.

5. Optional checks:

```powershell
python test_v9.py
```

This runs local self-tests and prints `ALL TESTS PASSED`.

6. Optional backup:

```powershell
.\backup.ps1
```

7. Optional restore from the latest backup:

```powershell
.\restore.ps1
```

---

## Project layout

```
AgenticTrade/
├── research_bot.py       # Python multi-agent bot + local web/API server (http://localhost:8080)
├── command_center.html   # Web Command Center, live-synced with the bot
├── analytics.html        # v10 analytics dashboard (/analytics)
├── test_v9.py            # local self-tests (v9 + v10)
├── smoke_v10.py          # end-to-end API smoke test
├── requirements.txt      # Python dependencies
├── backup.ps1            # backup helper for DB, .env and log
├── restore.ps1           # restore helper from a backup folder
├── Dockerfile            # optional container image
├── docker-compose.yml    # optional container orchestration
├── replay_v10.py         # v10 realized-trade replay report
├── RELEASE_CHECKLIST.md  # pre-release/live-trading checklist
├── .github/workflows/tests.yml
├── agentictrade/         # incremental modular package
│   ├── config.py         # env helpers
│   ├── http.py           # retry/backoff helpers
│   ├── models.py         # Pydantic models
│   ├── telegram.py       # Telegram notifier + inline buttons
│   ├── migrations.py     # schema_version / migrations (v10)
│   ├── metrics.py        # metrics collection (v10)
│   ├── observability.py  # JSON/audit logging + alert throttle (v10)
│   ├── risk_analytics.py # VaR, drawdown, profit factor, HHI (v10)
│   ├── backtest.py       # realized replay report (v10)
│   ├── security.py       # optional token auth + rate limit (v10)
│   ├── strategies.py     # risk profiles (v10)
│   ├── ml.py             # advisory logistic model (v10)
│   └── price_feed.py     # TTL price cache (v10)
├── .env                  # your local config and keys (never share it)
├── .env.example          # template with every setting
├── paper_portfolio.db    # SQLite paper book (created automatically)
├── bot_history.log       # persistent file log
├── README.md
└── .gitignore
```

## Agents

1. **Discovery** (`DiscoveryAgent`, `PumpFunStreamAgent`): Pump.fun WebSocket (new tokens, migrations, tracked-whale trades) and DexScreener scans. Also records whale sells, which feed `WHALE_EXIT`.
2. **Research** (`ResearchAgent`): free hard filters, then Gemini (or a strict heuristic without a key) returns BUY / WATCH / SKIP with risk and hype scores.
3. **Risk & Portfolio** (`PaperTradingManager`): SQLite paper book plus the v9 risk engine.
4. **Execution** (`SolanaExecutionAgent`): Jupiter v6 / PumpPortal routing with a Jito tip. **Dry-run only** while `LIVE_TRADING_ENABLED=false`.

## API (camelCase)

| Endpoint | Purpose |
|---|---|
| `GET /api/state` | wallet, positions, scans, logs, risk snapshot, config |
| `GET /api/journal?limit=N` | decision journal |
| `GET /api/equity` | equity curve points |
| `GET /api/wallet_quality` | whale wallet scores |
| `GET /api/circuit_breaker` | breaker state |
| `GET /api/health` | lightweight health check |
| `GET /ready` | readiness check (database, AI, Telegram, auth, live mode) |
| `GET /metrics` | Prometheus 0.0.4 exposition (HTTP counters/latency, trades, PnL, breaker, uptime) |
| `GET /api/metrics` | v10 aggregate metrics (trades, win rate, net PnL, open positions) |
| `GET /api/portfolio_risk` | v10 portfolio risk: HHI, top position, open exposure, max drawdown, VaR95, expected shortfall, profit factor, expectancy, avg win/loss |
| `GET /api/backtest` | realized replay: equity curve, daily PnL, exit/source/conviction breakdowns |
| `GET /api/audit?limit=N` | JSONL audit trail (live attempts, dry-runs, blocked calls, WS failures) |
| `GET /api/ml/status` | ML advisory model summary (accuracy, top weights) |
| `GET /api/ml/predict?mint=...` | advisory buy/skip probability for a scored mint |
| `POST /api/ml/train` | retrain the advisory model from closed paper trades |
| `GET /analytics` | dedicated analytics dashboard page |
| `POST /api/breaker/reset` | manual breaker reset |
| `POST /api/confirmations/cancel` | cancel a pending entry `{mint}` |
| `POST /api/config` | change runtime settings (never changes live trading) |
| `POST /api/buy` / `POST /api/sell` | manual paper buy / manual close |

## Modular package (`agentictrade/`)

The project is being migrated incrementally from the original monolith into a small package:

- `agentictrade/config.py` — environment variable helpers
- `agentictrade/http.py` — retry/backoff helpers for HTTP calls
- `agentictrade/models.py` — Pydantic models such as `TokenAnalysisDecision`
- `agentictrade/telegram.py` — Telegram notifier and inline buttons
- `agentictrade/migrations.py` — schema version and migrations (v10)
- `agentictrade/metrics.py` — aggregate metrics (v10)
- `agentictrade/observability.py` — file/JSON logger, JSONL audit trail, alert throttle (v10)
- `agentictrade/risk_analytics.py` — drawdown, VaR, expected shortfall, profit factor, HHI (v10)
- `agentictrade/backtest.py` — realized-trade replay report (v10)
- `agentictrade/security.py` — optional dashboard token and rate limiter (v10)
- `agentictrade/strategies.py` — conservative/balanced/aggressive risk profiles (v10)
- `agentictrade/ml.py` — dependency-free logistic advisory model (v10)
- `agentictrade/price_feed.py` — TTL price cache shared by the scan and monitor loops (v10)
- `agentictrade/prometheus.py` — dependency-free counters/gauges/histograms and text exposition (v11)
- `agentictrade/shutdown.py` — cross-platform SIGINT/SIGTERM controller (v11)
- `agentictrade/ohlcv.py` — event-driven OHLCV backtester, strategy library and walk-forward splits (v11)

`research_bot.py` remains the main entry point and imports these components. The goal is to make future upgrades, tests and maintenance easier without changing trading behavior.

## Continuous integration

GitHub Actions workflow is located at:

```text
.github/workflows/tests.yml
```

It runs on every push and pull request and has three jobs:

- **lint**: Ruff (`E4`, `E7`, `E9`, `F`) plus mypy over the `agentictrade` package
- **test**: Python 3.11 and 3.12 on Ubuntu and Windows; runs `python test_v9.py` and `python smoke_v10.py`
- **audit**: `pip-audit` against the pinned runtime dependencies

Dependencies are fully pinned in `requirements.txt`; tooling lives in `requirements-dev.txt`. Optional local hooks are configured in `.pre-commit-config.yaml`.

## Docker (optional)

Build and run locally:

```powershell
docker compose up --build
```

The container exposes port `8080`. Mount or provide:

- `.env`
- `paper_portfolio.db`
- `bot_history.log`

Live trading remains disabled unless explicitly configured in `.env`.

## Backup and restore

Create a timestamped backup:

```powershell
.\backup.ps1
```

Restore from the latest backup:

```powershell
.\restore.ps1
```

Or restore from a specific folder:

```powershell
.\restore.ps1 -BackupPath "C:\Users\LiveTony\Desktop\AgenticTradeBackup\20260101_120000"
```

The restore script asks for confirmation before overwriting local files.

## Telegram bot (buttons)

The Telegram integration now uses inline buttons instead of slash commands:

- Status, positions, journal, sell all, reset breaker, help
- Analyze mint / buy mint prompt buttons, followed by sending the mint address
- Button callbacks are handled through Telegram `callback_query` updates

Slash commands are still supported for compatibility.

## v10.0 progress

The repository now includes the full v10 layer:

**Data & risk**
- `schema_version` table and migration helper (`agentictrade/migrations.py`), current schema `10.0.0`
- aggregate metrics (`agentictrade/metrics.py`) and a shared risk-analytics library
- portfolio risk now reports HHI concentration, top position weight, open exposure %, max drawdown, VaR95, expected shortfall, profit factor, expectancy and average win/loss
- realized-trade backtest report (`agentictrade/backtest.py`, `replay_v10.py`, `GET /api/backtest`)

**Execution safety**
- `LIVE_TRADING_CONFIRMED` and `MAX_LIVE_TRADE_SOL` guards in `SolanaExecutionAgent.execute_buy`
- JSONL audit trail for dry-runs, blocked live calls, sent transactions and RPC errors
- deduplicated Telegram alerts for circuit breaker and stream failures

**Operations**
- optional `DASHBOARD_TOKEN` (sent as `X-Auth-Token` or `?token=`) and per-IP rate limiting
- structured JSON logging option and `/api/audit`
- strategy profiles: `STRATEGY_PROFILE=conservative|balanced|aggressive`; env values always win
- TTL price cache to cut duplicate DexScreener calls (`PRICE_CACHE_ENABLED`, `PRICE_CACHE_TTL_SEC`)

**Analytics**
- dependency-free logistic advisory model (`agentictrade/ml.py`); it never changes sizing or trade decisions
- dedicated analytics dashboard at `/analytics` (equity curve, risk ledger, concentration, exit anatomy, daily PnL, ML weights, audit feed, recent trades)

These additions are intentionally non-breaking: v9 behavior remains the default. All new security features are opt-in.

## v11.0 progress

- **Reproducible builds**: exact pins in `requirements.txt`, separate dev tooling, `pyproject.toml` with Ruff/mypy settings, and a CI matrix (Ubuntu + Windows, Python 3.11 + 3.12).
- **Research**: event-driven OHLCV backtester (`agentictrade/ohlcv.py`) with CSV/JSON/GeckoTerminal loaders, momentum and breakout strategies, fee/slippage/latency modelling and walk-forward splits. See `examples/candles_sample.csv`.
- **Observability**: dependency-free Prometheus registry, `GET /metrics` (counters, latency histogram, DB-derived gauges, breaker state, uptime) and `GET /ready` for dependency checks. Both honour `DASHBOARD_TOKEN`.
- **Graceful shutdown**: `SIGINT`/`SIGTERM` stop new cycles, cancel monitor/confirmation tasks, close the aiohttp runner and append a `shutdown` audit event.
- **Safer browser surface**: CORS is now an allowlist (`ALLOWED_ORIGINS`, localhost on any port by default) instead of `*`; responses carry `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy` and a CSP for HTML pages. `CORS_ALLOW_ALL=true` restores the old behaviour.
- **Dependency auditing**: `pip-audit` runs in CI on every push.

```powershell
# scrape metrics locally
curl.exe -H "X-Auth-Token: $env:DASHBOARD_TOKEN" http://127.0.0.1:8080/metrics

# override the browser allowlist (empty = localhost only)
$env:ALLOWED_ORIGINS="https://my-dashboard.example.com"
```

### Verifying v10

```powershell
$env:PYTHONIOENCODING='utf-8'
python test_v9.py      # unit + integration self-tests
python smoke_v10.py    # starts the real API on port 18080 and checks every endpoint
python replay_v10.py   # realized-trade replay report
python -m agentictrade.ml status
python -m agentictrade.ohlcv --help   # candle backtester CLI
```

## OHLCV backtesting

`agentictrade/ohlcv.py` is a real candle backtester, separate from the realized-trade replay:

- loads CSV, JSON, or GeckoTerminal USD candles
- long-only, one position at a time
- signal at candle close, fill at a later candle open (no look-ahead)
- configurable fees, slippage, latency, stop loss, take profit, trailing stop and time stop
- stop is checked before trailing and take profit, so wide candles are handled conservatively
- reports return, max drawdown, profit factor, expectancy, exposure, fees, per-trade Sharpe and exit reasons
- `--walk-forward N` evaluates sequential out-of-sample segments

```powershell
# CSV columns: ts,open,high,low,close,volume (ISO timestamps or epoch seconds)
python -m agentictrade.ohlcv --csv candles.csv --strategy momentum --fast 5 --slow 13
python -m agentictrade.ohlcv --json candles.json --strategy breakout --lookback 30
python -m agentictrade.ohlcv --pool <geckoterminal-pool> --timeframe hour --walk-forward 3
python -m agentictrade.ohlcv --csv candles.csv --output json --save result.json
```

This engine ships with a momentum (EMA cross) and a breakout strategy. It is a research tool: a positive backtest is not a promise of future profit.

## Known limitations

- Without a Gemini key, the strict heuristic tops out below the auto-BUY threshold, so heuristic mode effectively never auto-buys. This is by design: fail-closed.
- Pending confirmations live in memory. Restarting the bot drops them.
- Tokens that exist only on the Pump.fun curve (no DexScreener quote yet) cannot pass confirmation or a manual buy, because no fresh price is available.
- Wallet quality starts neutral and only becomes meaningful after a few closed trades per wallet.
- Some older log and hard-filter messages are still in Croatian. The newer risk-engine messages are in English.
- The web API is intended for local use. If you must expose port 8080 on a LAN, set `DASHBOARD_TOKEN` and keep the rate limit enabled. Do not expose it to the internet.
- The ML model is advisory only and never opens, blocks or resizes trades. The realized replay and the OHLCV backtester are research tools, not forecasts.
- The OHLCV backtester uses close-based signals and conservative high/low exit checks; it cannot model order-book depth, MEV or partial fills.
- Walk-forward segments reset the bankroll and do not re-optimize parameters; use it to compare strategies out of sample, not to tune them automatically.
- Strategy profiles only fill environment variables that are not set explicitly; change `.env` values to override them.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This project is for research and paper trading. It is not financial advice. Memecoin trading is extremely risky and most tokens lose value. Nothing here guarantees profit; use at your own risk.