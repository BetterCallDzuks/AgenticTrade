# Changelog

All notable changes to BetterCallTrader are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions use
`MAJOR.MINOR.PATCH`.

## [Unreleased]

### Added

- **Six new console windows.** Besides `/` (Command Center) and `/analytics`,
  the bot now serves six separate pages, all reachable from the shared top nav:
  - `/risk` — Risk desk: open exposure, HHI concentration, VaR95 and expected
    shortfall, max drawdown, circuit-breaker state (trips, daily stops/trades,
    daily PnL, reset button), entry gate and last blocked entry, sizing by
    conviction, concentration bars, the effective strategy profile and
    wallet-quality / source-analytics tables.
  - `/wallets` — balance, locked SOL, realized PnL and win-rate KPIs; add /
    remove tracked whales; a `simulate whale buy` test action; wallet-quality
    scores; open positions with entry/current price and remaining size.
  - `/backtest` — realized-trade replay from SQLite: summary KPIs, equity curve
    and 30-day daily-PnL SVG charts, breakdowns by exit reason / source /
    conviction, best and worst trade, recent trades.
  - `/ml` — advisory model state and contract versions, one-click training,
    feature weights, per-mint probability lookup and the resolved LLM provider.
  - `/system` — readiness checks from `/ready`, runtime/config summary,
    `/api/metrics` tiles, raw Prometheus text with copy, and the audit trail.
  - `/settings` — every runtime knob grouped by area (sizing, entry filters,
    exits, risk limits, flow/whale exits, paper costs, wallet quality,
    execution), the strategy-profile selector, dirty-state save/discard, and a
    read-only panel for live trading, Telegram, timezone, cost/exit models and
    the LLM.
- **Shared console asset layer.** `html/webui/console.js` injects the header and
  eight-item nav into every page, owns the HR/EN switch, dashboard-token
  storage, the `api()` helper (with 401/error handling), formatters, toasts and
  the polling runtime, which pauses while the tab is hidden. Settings opts out
  of polling and warns on `beforeunload` while it has unsaved edits.
  `html/webui/theme.css` holds the console theme and components. New pages only
  define their own panels and `I18N` dictionaries.
- The two existing pages (`html/command_center.html`, `html/analytics.html`) now render
  the full nav and gained `nav.*` translations for all eight windows; the
  `file://` nav fallback maps every page to its local `.html` file.
- **Local model fallback chain.** `LLM_MODELS` is a comma-separated list of
  additional local model IDs tried in order after `LLM_MODEL`, so a local
  OpenAI-compatible server (LM Studio, Ollama, llama.cpp, vLLM) keeps answering
  when the primary model is not loaded. `doctor` and `probe` now report the
  model IDs the server actually advertises when a configured one is missing.
- **LM Studio lifecycle in `./run.sh`.** When `LLM_BASE_URL` points at
  `127.0.0.1`/`localhost`, the launcher runs `lms server start --port <port>`
  before the bot and `lms server stop` when it exits. A server that was already
  running is left up, and a missing `lms` or a remote base URL is ignored.
- **Model selection in the dashboard.** `GET /api/llm/models` lists the models
  an LM Studio / OpenAI-compatible server advertises (LM Studio REST first,
  then OpenAI `/models`) with type, parameter count, vision and loaded state.
  `POST /api/llm/select` switches between `auto`, `gemini` and a specific local
  model and persists the choice in `bot_state`, restored at startup.
  `POST /api/llm/test` JIT-loads one model with a tiny JSON completion.
- **Settings → LLM provider picker.** Provider and local-model selects with an
  Apply button; the selection survives restarts and immediately rebuilds the
  research client's fallback chain.
- **ML / AI → Local AI models.** Lists every available local model, shows
  loaded/active state, and offers **Load & test** and **Use for research** per
  model. (GGUF models are inference-only; this manages and selects them rather
  than fine-tuning in-process.)

### Changed

- **Strategy profile selector in the Command Center.** The risk panel now has a
  `conservative` / `balanced` / `aggressive` / `high_risk` dropdown with a
  `STANDARD` / `HIGH RISK` badge. Selecting a profile applies its sizing and exit
  numbers immediately (overriding a pinned `.env`), logs a `RISK MGR` line,
  refreshes the config hash and is persisted in the `bot_state` table, so the
  choice survives a restart and wins over `STRATEGY_PROFILE` (now only the
  startup default). `POST /api/config {"strategyProfile": "..."}` exposes the
  same switch and returns `400` for an unknown profile; `/api/state` reports
  `config.strategyProfile` and `config.riskMode`.
- **High-risk / high-reward mode.** `STRATEGY_PROFILE=high_risk` (aliases
  `high-risk`, `highrisk`, `degen`) is a new opt-in risk mode: 1.5 SOL per trade,
  4 concurrent positions, -45% stop, +250% TP1 on 35% of the position, a 35%/25%
  trailing stop armed at +60% and looser daily limits (5 stops, 25 trades, 20%
  drawdown). Unlike the other profiles it **overrides** the `.env` sizing/exit
  keys — a pinned balanced `.env` would otherwise neutralise the mode — while
  entry filters (liquidity, holders, volume, mint/freeze) are untouched. Startup
  prints a risk banner, `/api/state` reports `strategyProfile.riskMode = "high"`
  and the doctor warns that the env keys are overridden. The profile also gained
  `TP1_FRACTION`, `TRAILING_ARM_GAIN_PCT`, `MIN_SECONDS_BETWEEN_ENTRIES` and
  `STOP_LOSS_COOLDOWN_SEC` knobs.
- **The bot now runs until it is stopped.** `run_research_pipeline()` defaults to
  `cycles=None`, so once started the scan loop is uncapped and keeps cycling
  (20s apart) until `Ctrl+C` / `SIGTERM`. Previously a run stopped by itself after
  100 cycles (~35 minutes) and had to be restarted for another session. Pass a
  positive integer — e.g. `run_research_pipeline(cycles=100)` — to cap a run.
- **Project renamed** from AgenticTrade to BetterCallTrader. The Python package is
  now `bettercalltrader` (was `agentictrade`), so module entry points are
  `python -m bettercalltrader.doctor`, `bettercalltrader.ohlcv`, and so on.
  Prometheus metrics use the `bettercalltrader_*` prefix
  (`bettercalltrader_info`, `bettercalltrader_db_up`,
  `bettercalltrader_http_requests_total`, `bettercalltrader_http_request_duration_seconds`).
  Dashboard storage keys are `bettercalltrader_token` and `bettercalltrader_lang`;
  a previously saved token and language preference must be set once more.
  The Docker service, CI workflow/job names, backup directory
  (`~/BetterCallTraderBackup`) and repository URL follow the new name; old clone
  URLs redirect.
- Both dashboards now share one dark console design (same header, nav, KPI cards,
  stat tiles and data tables) and a persistent **HR / EN** switch in the header.
  Croatian stays the default. `analytics.html` was rewritten; `command_center.html`
  was rethemed in place with every element id, endpoint and behaviour preserved.
  Dashboard version badges and runtime labels now report v12.
- Documentation refreshed for the rename: README intro/project layout, the
  dashboard section (language switch, dashboard token storage, metric prefix),
  and the known-limitations note about Croatian backend messages.
- **Repository layout cleaned up.** The eight console pages moved to `html/`
  (with their `html/webui/` assets), the Python entry points and tests
  (`research_bot.py`, `replay_v10.py`, `test_v9.py`, `smoke_v10.py`) to `py/`,
  the bootstrap/backup/restore helpers to `scripts/`, and the standalone docs
  (CHANGELOG, SECURITY, RELEASE_CHECKLIST) to `docs/`. `run.sh`, `START_BOT.bat`,
  `README.md`, `LICENSE`, the packaging files and the container files stay at the
  root. The launchers and `scripts/*` resolve the project root themselves, so
  `./run.sh`, `./scripts/backup.sh` and `python py/test_v9.py` behave as before
  from any working directory.

### Fixed

- **Container image now ships every console window.** The Dockerfile copied only
  `command_center.html` and `analytics.html`, so `/risk`, `/wallets`, `/backtest`,
  `/ml`, `/system`, `/settings` and the `/webui/` assets 404'd inside the image.
  It now copies `html/` and `py/` wholesale, and starts `py/research_bot.py`.
- **HTML pages now actually send `Content-Security-Policy`.** The middleware
  only attached the policy when a response already had `Content-Type:
  text/html`, but aiohttp's `FileResponse` guesses the type during
  `prepare()` — after middleware runs — so no page (including `/` and
  `/analytics`) ever received a CSP. Page responses are now served with an
  explicit `text/html; charset=utf-8`, and the policy is applied consistently to
  all eight windows.
- **Docker Compose state layout.** Mounting individual files made Docker create
  missing host paths as *directories*, which broke SQLite and logging on a fresh
  checkout. The container now keeps all mutable state in `/app/data`
  (`PAPER_DB_PATH`, `AUDIT_LOG_PATH`, `LOG_FILE_PATH`, `ML_MODEL_PATH`), bound to
  `./data`. `data/.gitkeep` keeps that directory user-owned after a clone.
- **Container healthcheck.** Compose now polls the authenticated `/ready`
  endpoint with `DASHBOARD_TOKEN`, so `docker compose ps` reports real health.
- **Secrets no longer leak into the container.** The GitHub token used by this
  checkout's git credential helper moved out of `.env` to
  `~/.config/git/bettercalltrader-token` (mode 0600). `.env` is loaded into the
  bot process and passed to the container, so it should only hold app config.
  A new `.dockerignore` also keeps `.env`, `.git`, `.venv`, databases and logs
  out of the build context.
- `LOG_FILE_PATH` is now configurable; relative values resolve against the
  project directory instead of the caller's cwd. `doctor` reports an existing
  `.env` with no keys as OK instead of "no readable file".
- `START_BOT.bat` was still branded "Solana Agentic Trading Bot v9.0" and now
  prefers `.venv\Scripts\python.exe` when the virtualenv exists.
- Backup/restore scripts (bash and PowerShell) now include the Compose `data/`
  directory, and the WAL consistency warning checks both locations.
- CI no longer runs the test workflow twice per pull request (`push` is limited
  to `main`; `pull_request` still covers branches).

## [12.0.0] - 2026-11-13

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

### Added

- `bettercalltrader/exits.py`: one shared `ExitPolicy` for the paper book
  (`classify_tick`) and the OHLCV backtester (`intrabar`). It owns the effective
  stop ladder, TP1 fraction, final target and the event-driven rules, so the two
  engines can no longer drift.
- `bettercalltrader/llm.py`: provider-neutral LLM client. Gemini and any
  OpenAI-compatible server (Ollama, llama.cpp, LM Studio, vLLM) share one
  call path, with `LLM_PROVIDER=auto`, provider fallback and a token-free
  `probe()` used by `doctor`.
- Depth-aware impact in `CostModel` (`impact_exponent`, `depth_used_fraction`,
  `replay_round_trip(..., depth_decay=True)`): an exit pays for the liquidity
  the entry consumed.
- Partial take profit in the backtester: `tp1_fraction`, `final_take_profit_pct`
  and the `--tp1-fraction` / `--final-take-profit` flags. Trade rows carry
  `tp1Hit`, `tp1Price`, `tp1ProceedsSol` and `legs`.
- `max_participation_pct` / `--max-participation`: a fill cannot exceed a share
  of candle volume, so thin candles produce smaller positions instead of
  fantasy fills. Summaries report `partialEntries`, `requestedEntries`,
  `tp1Exits`, `ambiguousBars` and `costsPaidSol`.
- ML model contract versioning: `MODEL_SCHEMA_VERSION`, `FEATURE_SET_VERSION`,
  `LABEL_SOURCE`, `data_fingerprint()`, `model_status()` and
  `train_if_stale()`. `load_model()` fails closed on a stale contract.
- New environment knobs: `TP1_FRACTION`, `FINAL_TAKE_PROFIT_PCT`,
  `TRAILING_ARM_GAIN_PCT`, `PAPER_IMPACT_EXPONENT`, `LLM_PROVIDER`,
  `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_TIMEOUT_SEC`,
  `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `LLM_FALLBACK_TO_HEURISTIC`.
- Pending entry confirmations are persisted in SQLite (`pending_confirmations`)
  and restored after a restart. `ENTRY_CONFIRM_RECOVERY_MAX_SEC` (default 900)
  bounds how old a restored signal may be; older rows are cancelled as
  `CONFIRM_EXPIRED`. Schema version is now `10.1.0`.
- Pump.fun curve pricing: `ResearchAgent.fetch_pumpfun_curve_quote()` reads
  `frontend-api-v3.pump.fun/coins/<mint>` when DexScreener has no Solana pair,
  so pre-graduation tokens can pass entry confirmation and manual buys.
  `PUMPFUN_CURVE_ENABLED` (default true), `PUMPFUN_CURVE_CACHE_SEC` (5s) and
  `PUMPFUN_CURVE_MISS_CACHE_SEC` (60s) control the fallback.
- Walk-forward parameter optimization: `bettercalltrader/optimizer.py` grid-searches
  the strategy parameters on each train slice and trades only the winning set on
  the untouched test slice. The CLI exposes it as `--optimize` with
  `--objective` (`pnl`, `return_pct`, `profit_factor`, `expectancy`, `sharpe`),
  `--min-trades` and `--fast-grid` / `--slow-grid` / `--lookback-grid` /
  `--threshold-grid`. Candidates that never traded on the train slice are
  ineligible, a slice with no eligible candidate is reported as skipped, and
  the report compares the out-of-sample aggregate against the unoptimized
  `--fast/--slow/--lookback/--threshold` baseline (`upliftSol`).
- Rolling walk-forward and stability scoring: `--window-mode anchored` uses an
  expanding train window, `--window-mode rolling` slides a constant-size train
  window, and `--train-candles`/`--test-candles` size both. Every optimization
  report gains a `stability` block (dominant parameter-set share, profitable
  window share, per-window PnL spread and a `stable`/`mixed`/`unstable`
  label), so one lucky window cannot carry the result unnoticed.
- Cost stress and break-even analysis: `bettercalltrader/cost_stress.py` replays one
  strategy across a multiplier sweep and reports the cost level at which the
  edge disappears. `--stress-costs`, `--cost-multipliers` and `--cost-baseline`
  expose it in the CLI; the report includes the gross edge (costs removed), the
  interpolated `breakEvenMultiplier`, `headroomPct` and per-point fees/costs.
- Strategy comparison: `bettercalltrader/compare.py` runs several strategies through
  the same walk-forward optimizer and cost sweep and ranks them on
  out-of-sample, cost-adjusted evidence. `--compare` (with
  `--compare-strategies`, default `momentum,breakout`) emits one report with
  per-strategy aggregate, stability, break-even and uplift, plus a deterministic
  `ranking` and `winner`. The ranking key is objective score, then break-even
  multiplier, then stability, then strategy name.
- Live-execution readiness audit: `bettercalltrader/live_readiness.py` and
  `python -m bettercalltrader.live_readiness [--json] [--probe]`. It reports
  `PAPER`/`READY`/`BLOCKED` (exit 0/0/1), drives the real
  `SolanaExecutionAgent.execute_buy` against a session that refuses every
  request, and verifies paper `DRY_RUN_READY`, live-unconfirmed `BLOCKED`,
  live-over-cap `BLOCKED` and live-without-key `ERROR` with zero network calls.
  The private key is never printed; `--probe` is the only path that uses the
  network (`getHealth` plus a Jupiter quote). `doctor` gains a
  `live_readiness` summary line.

### Changed

- `python -m bettercalltrader.ohlcv --optimize` reuses the `--walk-forward` segment
  count (default 3 when it is 0) and `--train-ratio`; the plain
  `--walk-forward` path still evaluates one fixed parameter set.
- `--window-mode` (fixed/anchored/rolling) and `--train-candles`/`--test-candles`
  are optimizer-only; the CLI rejects them for `--walk-forward`,
  `--stress-costs` and plain backtests instead of ignoring them.
- `--compare` is a fourth mode in the same mutually-exclusive group as
  `--optimize` and `--stress-costs`; it reuses the optimizer's grid, window and
  `--min-trades`/`--objective` flags and rejects unknown strategy names.
- `--stress-costs` is a third mode, mutually exclusive with `--optimize`: it
  sweeps the whole series instead of the walk-forward windows and refuses to
  combine with `--walk-forward` rather than silently ignoring it.
- `SolanaExecutionAgent` accepts `live_trading_confirmed` and
  `max_live_trade_sol` as constructor arguments (env fallback unchanged), and
  an explicit empty `private_key_b58` now means "no key" instead of silently
  reading `SOLANA_PRIVATE_KEY`. This lets the readiness harness exercise the
  guards without loading a real key.
- `ResearchAgent.get_market_data()` now falls back to a Pump.fun bonding-curve
  quote when DexScreener has no pair, instead of returning `None`. Freshness,
  hard filters and the fail-closed `STALE_PRICE` path are unchanged.
- Take profit is now a *partial* exit by default (`tp1_fraction=0.5`); the
  remainder runs on the same stop ladder as the paper book. Set
  `tp1_fraction=1.0` for the old full-exit behaviour.
- `COST_MODEL_VERSION` is `"2"` (impact exponent and depth decay). Every model
  trained under v1 is reported stale until retrained.
- `BacktestConfig.exit_policy()` merges the flat CLI overrides into the shared
  policy; the fingerprint covers `exitPolicy`, both cost models and the
  strategy. The second-based time stop is opt-in in backtests (`--time-stop`)
  because it is tuned for minute data.
- `GET /api/state` exposes `exitPolicy` and `llm` (resolved provider chain,
  local/remote) and the runtime-editable config gains `tp1Fraction`,
  `finalTakeProfitPct`, `trailingArmGainPct` and `paperImpactExponent`.
- `GET /api/ml/status` reports the contract state and reasons; stale models are
  refused on predict instead of scored quietly.
- `doctor` now probes the configured LLM endpoint, and `validate.py` checks the
  new LLM/exit/cost keys.
- One `CostModel` (`bettercalltrader/costs.py`) now prices every simulated fill:
  the paper book, the OHLCV backtester and the ML label source. It models the
  AMM fee, priority fee, expected failed-transaction cost, spread/impact and
  pool depth, and `replay_round_trip()` is the reference for parity tests.
- `bettercalltrader/defaults.py` is the single source for the balanced strategy
  values and the shared cost defaults. The paper book logs and exposes a
  `configHash`; the OHLCV result reports the same hash plus
  `costModelVersion`.
- `PAPER_AMM_FEE_BPS`, `PAPER_PRIORITY_FEE_SOL` and
  `PAPER_FAILED_TX_PROB` are validated by `doctor` and documented in
  `.env.example`.

### Fixed

- `WHALE_EXIT` now requires the sell to come from the whale the position
  followed, after entry, within `WHALE_EVENT_TTL_SEC` (default 1800s).
- Every sell leg pays `PAPER_EXIT_SLIPPAGE_BPS`; TP1, time-stop, trailing and
  manual exits previously booked at the lower entry rate.
- Quotes with a future timestamp are treated as stale (clock skew) instead of
  passing the freshness check.
- `LIVE_TRADING_ENABLED` and `LIVE_TRADING_CONFIRMED` use the shared
  `env_bool()` parser, matching `validate.py` semantics.
- OHLCV exits are gap-aware: a stop fills at `min(open, stop)` and take profit
  at `max(open, tp)` instead of the stop/target price when the candle gapped
  through it.
- Trailing stops use the peak *before* the current candle, so a candle cannot
  arm and trigger its own trailing stop.
- The entry candle no longer exits the position it just opened; its high still
  updates the peak for the next candle.
- When a candle contains both a stop/trailing stop and take profit, the stop is
  used and the trade is flagged `ambiguousBar` instead of silently assuming an
  intrabar order.
- The backtester now actually applies the hard stop-loss level. Before, only the
  trailing/step-up ladder was checked, so a position without an armed trailing
  stop could ride through its stop.
- A gap through a trailing stop fills at the open (never better than the stop),
  and the fill price is recorded as the mid for cost reconciliation.
- The paper book sells `intent.fraction` at TP1 instead of a hard-coded 50%, so
  `TP1_FRACTION` and the backtester stay in sync.

## [11.0.0] - 2026-11-12

### Added

- `bettercalltrader/prometheus.py`: dependency-free counters, gauges and
  histograms with Prometheus text 0.0.4 exposition.
- `GET /metrics` (HTTP counters and latency, trades, win rate, net PnL, open
  positions, wallet balance, breaker state, uptime, build info) and
  `GET /ready` (database, AI, Telegram, auth and live-mode checks). Both honour
  `DASHBOARD_TOKEN` and the rate limiter.
- `bettercalltrader/shutdown.py`: cross-platform `SIGINT`/`SIGTERM` controller.
  Shutdown stops new cycles, cancels monitor/confirmation tasks, closes the
  aiohttp runner and writes a `shutdown` audit event.
- `bettercalltrader/ohlcv.py`: event-driven OHLCV backtester with CSV/JSON/
  GeckoTerminal loaders, EMA-cross and breakout strategies, fee/slippage/
  latency modelling, stop/take-profit/trailing/time exits, walk-forward splits
  and a Markdown/JSON CLI (`python -m bettercalltrader.ohlcv`).
- `bettercalltrader/validate.py` and `bettercalltrader/doctor.py`: configuration
  validation plus a read-only pre-flight CLI (`python -m bettercalltrader.doctor`).
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

- `schema_version` table and migration helper (`bettercalltrader/migrations.py`).
- Aggregate metrics (`bettercalltrader/metrics.py`), risk analytics (HHI, top
  weight, exposure, drawdown, VaR95, expected shortfall, profit factor,
  expectancy) and a realized-trade replay (`replay_v10.py`, `GET /api/backtest`).
- JSONL audit trail, deduplicated Telegram alerts, optional `DASHBOARD_TOKEN`
  auth and per-IP rate limiting.
- Strategy profiles (`STRATEGY_PROFILE`) and an advisory ML model
  (`bettercalltrader/ml.py`).
- Shared TTL price cache (`bettercalltrader/price_feed.py`).

## [9.0.0] - 2026-09-30

### Added

- Pump.fun WebSocket discovery, DexScreener scans, RugCheck security checks and
  optional Gemini research.
- SQLite paper portfolio with stop loss, TP1, trailing stop, fast invalidation,
  step-up stops, time stops, flow and whale exits.
- Local Command Center (`command_center.html`) and analytics dashboard.
- Dry-run Jupiter/PumpPortal executor with live-trading guards.
