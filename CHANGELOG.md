# Changelog

All notable changes to AgenticTrade are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions use
`MAJOR.MINOR.PATCH`.

## [Unreleased]

### Security

- The dashboard now binds `127.0.0.1` by default. A non-loopback bind with an
  empty `DASHBOARD_TOKEN` raises `SystemExit` instead of exposing an
  unauthenticated trading API; `ALLOW_INSECURE_DASHBOARD=true` is the explicit
  opt-out (doctor warns, never silently ignores it).
- Mutating API routes (`POST`/`PUT`/`PATCH`/`DELETE`) reject cross-site
  browser requests via `Sec-Fetch-Site`/`Origin`, even without a token.
- Rate limiting runs before token auth, and `X-Forwarded-For` is trusted only
  when `TRUST_PROXY_HEADERS=true`.
- Boolean env vars parsed with `env_bool()` are validated at startup: a typo
  (`ture`, `flase`) is now a hard `FAIL` in `doctor` instead of silently
  meaning `false`.
- `backup.sh --no-secrets` omits `.env`, and backups that contain it print a
  warning.
- `doctor` warns when `.env` is group/world-readable (POSIX) and tells you to
  `chmod 600` it.
- `docker-compose.yml` requires `DASHBOARD_TOKEN` (`${DASHBOARD_TOKEN:?…}`)
  because the container must bind `0.0.0.0`.

### Fixed

- `WHALE_EXIT` now requires the sell to come from the whale the position
  followed, after entry, within `WHALE_EVENT_TTL_SEC` (default 1800s).
- Every sell leg pays `PAPER_EXIT_SLIPPAGE_BPS`; TP1, time-stop, trailing and
  manual exits previously booked at the lower entry rate.
- Quotes with a future timestamp are treated as stale (clock skew) instead of
  passing the freshness check.
- `LIVE_TRADING_ENABLED` and `LIVE_TRADING_CONFIRMED` use the shared
  `env_bool()` parser, matching `validate.py` semantics.

## [11.0.0] - 2026-11-12

### Added

- `agentictrade/prometheus.py`: dependency-free counters, gauges and
  histograms with Prometheus text 0.0.4 exposition.
- `GET /metrics` (HTTP counters and latency, trades, win rate, net PnL, open
  positions, wallet balance, breaker state, uptime, build info) and
  `GET /ready` (database, AI, Telegram, auth and live-mode checks). Both honour
  `DASHBOARD_TOKEN` and the rate limiter.
- `agentictrade/shutdown.py`: cross-platform `SIGINT`/`SIGTERM` controller.
  Shutdown stops new cycles, cancels monitor/confirmation tasks, closes the
  aiohttp runner and writes a `shutdown` audit event.
- `agentictrade/ohlcv.py`: event-driven OHLCV backtester with CSV/JSON/
  GeckoTerminal loaders, EMA-cross and breakout strategies, fee/slippage/
  latency modelling, stop/take-profit/trailing/time exits, walk-forward splits
  and a Markdown/JSON CLI (`python -m agentictrade.ohlcv`).
- `agentictrade/validate.py` and `agentictrade/doctor.py`: configuration
  validation plus a read-only pre-flight CLI (`python -m agentictrade.doctor`).
- `COMMAND_CENTER_HOST`, `COMMAND_CENTER_PORT` and `PAPER_DB_PATH` environment
  variables.
- Security headers on every response (`X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, and a CSP for
  HTML pages).
- `SECURITY.md`, `CHANGELOG.md`, Dependabot and CodeQL workflows.
- `requirements-dev.txt`, `pyproject.toml`, `.pre-commit-config.yaml` and a
  Ruff/mypy/audit/test-matrix CI pipeline.

### Changed

- CORS is now an origin allowlist (`ALLOWED_ORIGINS`). Localhost on any port is
  accepted by default; `CORS_ALLOW_ALL=true` restores the old wildcard.
- Runtime dependencies are pinned exactly; `aiohttp` and `python-dotenv` were
  bumped to clear known advisories.
- `PaperTradingManager` honours `PAPER_DB_PATH`.
- CI runs Ruff, mypy, `pip-audit` and the test suite on Ubuntu and Windows with
  Python 3.11 and 3.12.

### Fixed

- Prometheus histogram buckets are exposed as cumulative counts.
- `aiohttp` timeout arguments use `aiohttp.ClientTimeout`, avoiding implicit
  session timeout confusion.
- Telegram update handling no longer shadows the `message` payload.

## [10.0.0] - 2026-10-20

### Added

- `schema_version` table and migration helper (`agentictrade/migrations.py`).
- Aggregate metrics (`agentictrade/metrics.py`), risk analytics (HHI, top
  weight, exposure, drawdown, VaR95, expected shortfall, profit factor,
  expectancy) and a realized-trade replay (`replay_v10.py`, `GET /api/backtest`).
- JSONL audit trail, deduplicated Telegram alerts, optional `DASHBOARD_TOKEN`
  auth and per-IP rate limiting.
- Strategy profiles (`STRATEGY_PROFILE`) and an advisory ML model
  (`agentictrade/ml.py`).
- Shared TTL price cache (`agentictrade/price_feed.py`).

## [9.0.0] - 2026-09-30

### Added

- Pump.fun WebSocket discovery, DexScreener scans, RugCheck security checks and
  optional Gemini research.
- SQLite paper portfolio with stop loss, TP1, trailing stop, fast invalidation,
  step-up stops, time stops, flow and whale exits.
- Local Command Center (`command_center.html`) and analytics dashboard.
- Dry-run Jupiter/PumpPortal executor with live-trading guards.
