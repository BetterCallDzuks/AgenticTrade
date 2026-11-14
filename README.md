# Solana Memecoin Multi-Agent Trading Bot: BetterCallTrader v13.0

[![CI](https://github.com/BetterCallDzuks/BetterCallTrader/actions/workflows/tests.yml/badge.svg)](https://github.com/BetterCallDzuks/BetterCallTrader/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-13.0.0-e3a452.svg)](https://github.com/BetterCallDzuks/BetterCallTrader)

A multi-agent system for discovering, checking and **paper-trading** Solana memecoins. It combines the Pump.fun WebSocket, the DexScreener API, RugCheck security checks, an optional AI analyst (Google Gemini or any local OpenAI-compatible model), a SQLite paper portfolio and a Jupiter/Jito dry-run executor, all behind a local web Command Center and an analytics dashboard.

> **Formerly AgenticTrade.** The project was renamed in v12; the Python package,
> Prometheus metric prefix and dashboard storage keys are now `bettercalltrader`.
> Old clone URLs redirect, but a saved dashboard token (`?token=`) and the HR/EN
> preference have to be set once more.

> **Paper trading only by default.** `LIVE_TRADING_ENABLED=false` stays off, and nothing in v12 turns it on (the dashboard cannot either). This software does not promise or predict profit. Memecoins are extremely risky. Replay and paper results are a way to test rules, not a forecast.

---

## Contents

- [Quick start](#quick-start)
- [Using the bot](#using-the-bot)
- [Install / run (Linux / Ubuntu)](#install--run-linux--ubuntu)
- [Install / run (Windows PowerShell)](#install--run-windows-powershell)
- [Project layout](#project-layout)
- [Agents](#agents)
- [API](#api-camelcase)
- [v13.0 progress](#v130-progress)
- [Roadmap](docs/ROADMAP.md)
- [OHLCV backtesting](#ohlcv-backtesting)
- [Backup and restore](#backup-and-restore)
- [Docker](#docker-optional)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)

## Quick start

The shortest path from a clean checkout to a running **paper-trading** bot. No
wallet, private key or SOL is needed for paper mode.

### 1. Prerequisites

- **Python 3.11+** with `venv` support (`python3 -m venv --help` must work).
- Internet access for DexScreener and Pump.fun discovery, and optionally for an
  LLM provider.
- Optional: a `GEMINI_API_KEY` or a local OpenAI-compatible server. With no LLM
  the bot runs a strict heuristic that deliberately never auto-buys.

### 2. Install

Linux / macOS:

```bash
git clone https://github.com/BetterCallDzuks/BetterCallTrader.git
cd BetterCallTrader
cp .env.example .env
$EDITOR .env
./run.sh
```

`run.sh` creates `.venv/`, installs the pinned dependencies, warns if `.env` is
missing, runs the pre-flight doctor, and starts the bot.

Windows PowerShell:

```powershell
git clone https://github.com/BetterCallDzuks/BetterCallTrader.git
cd BetterCallTrader
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
.\.venv\Scripts\python.exe -m bettercalltrader.doctor
.\.venv\Scripts\python.exe py\research_bot.py
```

Both paths install the same pins. Re-running `./run.sh` (or `scripts/bootstrap.sh`) is
safe and only reinstalls when `requirements.txt` changes.

### 3. Configure

`cp .env.example .env` and edit it. These are the only values that change
behaviour on day one:

| Variable | Default | What to do |
|---|---|---|
| `LIVE_TRADING_ENABLED` | `false` | **Leave it `false`.** Paper mode cannot send an on-chain order. |
| `GEMINI_API_KEY` | empty | Optional. Enables the Gemini analyst. |
| `LLM_BASE_URL` + `LLM_MODEL` / `LLM_MODELS` | empty | Optional local alternative: Ollama, llama.cpp, LM Studio, vLLM; `LLM_MODELS` adds ordered fallbacks. |
| `INITIAL_PAPER_SOL` | `10.0` | Paper bankroll. |
| `TRADE_SIZE_SOL` | `0.5` | Paper size per entry. |
| `STRATEGY_PROFILE` | `balanced` | `conservative`, `balanced`, `aggressive` or `high_risk` (high-risk/high-reward mode). |
| `DASHBOARD_TOKEN` | empty | Only needed if the dashboard binds beyond `127.0.0.1`. |
| `DASHBOARD_TOTP_SECRET` | empty | Optional 2FA for dashboard writes (`X-TOTP`); reads stay token-only. |
| `DIGEST_INTERVAL_SEC` | `0` | Push the Telegram digest every N seconds (`0` = only on `/digest`). |
| `LIVE_TRADING_STAGE` | `full` | When live is armed: `dry_run` → `probe` → `full`. Env-only. |

Everything else in `.env.example` is grouped by topic with comments; the
defaults are a complete, working paper configuration. Explicit environment
values always win over `STRATEGY_PROFILE` defaults. `.env` is read at startup,
so restart after editing it.

### 4. Verify before the first run

```bash
python -m bettercalltrader.doctor          # must print: Result: OK
python -m bettercalltrader.live_readiness  # must print: Status: PAPER
```

Optional but quick:

```bash
python py/test_v9.py     # prints ALL TESTS PASSED
python py/smoke_v10.py   # starts the real API on an ephemeral port, prints SMOKE OK
```

### 5. Run it

```bash
./run.sh
# or, with the venv active:
python py/research_bot.py
```

Open **http://127.0.0.1:8080** — the bot also tries to open a browser. Once started
the bot **runs until you stop it**: it keeps scanning in 20-second cycles with no
cycle cap, while endpoint monitoring runs every `POSITION_MONITOR_SEC` (4s) and
pending confirmations every 2s. On `Ctrl+C`/`SIGTERM` it shuts down cleanly and
writes a `shutdown` audit event. To cap a run, call
`run_research_pipeline(cycles=N)` from your own launcher.

Watch the first cycles before changing anything: discovery, filters, AI verdict
and risk checks are all visible in the terminal, the dashboard and
`bot_history.log`.

### 6. Stop it

Press `Ctrl+C` in the terminal (or send `SIGTERM`). Open positions are paper
positions and remain in `paper_portfolio.db` for the next run.

---

## Using the bot

### What a run does

1. **Discovery** — scans the latest DexScreener candidates (3 per cycle) and
   listens to Pump.fun WebSocket events (new tokens, migrations, tracked-whale
   trades) for 8 seconds per cycle.
2. **Research and gates** — free hard filters run first (liquidity, volume,
   holder concentration, chase/dump checks, freshness). Only survivors reach the
   LLM or heuristic analyst, which returns BUY / WATCH / SKIP with risk and hype
   scores.
3. **Entry confirmation** — an automatic BUY is not filled immediately. The bot
   watches the price for `ENTRY_CONFIRM_SEC` (20s) and cancels if it drops more
   than `ENTRY_CONFIRM_MAX_DROP_PCT` (4%). Pending entries are persisted and
   survive a restart within `ENTRY_CONFIRM_RECOVERY_MAX_SEC` (900s).
4. **Paper fill** — the position is opened in SQLite using the shared cost model
   (fee, slippage, impact, priority fee, failed-transaction probability).
5. **Monitoring** — every 4s the position is marked to market and the shared
   exit ladder runs (see [Exit order](#whats-in-v90-still-active)).
6. **Execution dry-run** — with live trading off, the execution agent returns a
   `DRY_RUN_READY` (Pump.fun curve) or `DRY_RUN_QUOTED` (Jupiter) plan and
   records it in the audit log. No order leaves the machine.

### The Command Center

Open **http://127.0.0.1:8080**:

- **`/`** — KPI cards (balance, locked SOL, open positions, PnL, win rate), the
  four agent cards (Discovery, Research, Risk, Execution), open positions with a
  sell button, pending confirmations, the circuit-breaker banner, a runtime
  config panel, the decision journal, the equity chart and the audit feed.
- **`/analytics`** — equity curve, portfolio risk ledger, concentration (HHI),
  exit anatomy, daily PnL, ML weights, audit feed and recent trades.
- **`/risk`** — Risk desk: open exposure, HHI concentration, VaR/expected
  shortfall, max drawdown, circuit-breaker state with a reset button, entry
  gate and last blocked entry, sizing by conviction, live concentration bars,
  the effective strategy profile and wallet-quality/source tables.
- **`/wallets`** — Wallets: balance/locked/PnL KPIs, add/remove tracked whales,
  a `simulate whale buy` test button, wallet-quality scores and open positions.
- **`/backtest`** — Backtest lab: replays the realized paper trades from SQLite
  into summary KPIs, an equity curve, 30-day daily PnL bars, breakdowns by exit
  reason / source / conviction, best and worst trade, and the recent trade list.
- **`/ml`** — ML / AI lab: advisory model state and contract versions, training
  button, feature weights, per-mint probability lookup and the LLM provider
  description.
- **`/system`** — System / ops: readiness checks, runtime/config summary,
  `/api/metrics` tiles, raw Prometheus text with copy, and the audit trail.
- **`/settings`** — Settings: every runtime knob grouped by area, the strategy
  profile selector, dirty-state save/discard, and a read-only panel for live
  trading, Telegram, timezone, cost/exit models and the LLM.
- **`/metrics`** — Prometheus text exposition (honours `DASHBOARD_TOKEN`).
- **`/ready`** — dependency readiness (database, AI, Telegram, auth, live mode).
- **`/api/state`** — everything the dashboard polls, as JSON.

All eight windows share one dark console layout — identical header, top nav and
a language switch (**HR / EN**, Croatian by default) — and are separate pages,
not tabs. The choice is stored in `localStorage` (`bettercalltrader_lang`) and
carries across every page; each page has its own translations for its panels
and tables. Messages produced by the Python bot (log lines, AI summaries, exit
reasons) stay in the language it emitted them in.

The pages are plain static HTML served by the bot (`risk.html`, `wallets.html`,
`backtest.html`, `ml.html`, `system.html`, `settings.html`); the shared header,
polling, token handling and formatters live in `html/webui/console.js`, and the
console styling in `html/webui/theme.css`. Only `/api/*`, `/metrics` and `/ready`
require the dashboard token — pages themselves are static and safe to refresh.

When `DASHBOARD_TOKEN` is set, open the page once as `/?token=…`. Every page
stores it as `bettercalltrader_token` and sends it as `X-Auth-Token` on every
request; the **Token** button in the shared header replaces or clears it.
Prometheus metrics use the `bettercalltrader_*` prefix (`bettercalltrader_info`,
`bettercalltrader_db_up`, `bettercalltrader_http_requests_total`).

Set `DASHBOARD_TOTP_SECRET` (any base32 RFC 6238 secret) to add a second factor
to **state-changing requests only** (`POST`/`PUT`/`PATCH`/`DELETE`). Reads stay
token-only — a code that rotates every 30 seconds would break polling — while a
write must also carry the current 6-digit code in `X-TOTP` (the console prompts
for it on a `totp_required` / `invalid_totp` 401). `GET /api/state` reports
`security.totpEnabled` so the UI can show whether the gate is armed.

### Manual controls

| Action | Dashboard | API |
|---|---|---|
| Analyze a mint | paste mint → analyze | `POST /api/analyze` `{"mint": "..."}` |
| Paper buy | paste mint → BUY | `POST /api/buy` `{"mint": "..."}` |
| Close a position | sell 100% on the row | `POST /api/sell` `{"mint": "..."}` |
| Cancel a pending entry | `cancel` on the confirmation | `POST /api/confirmations/cancel` `{"mint": "..."}` |
| Reset the circuit breaker | banner button (`/`), reset button (`/risk`) | `POST /api/breaker/reset` |
| Change runtime knobs | config panel (`/`), full editor (`/settings`) | `POST /api/config` |
| Switch risk profile | **Strategy profile** dropdown (`/`, `/settings`) | `POST /api/config` `{"strategyProfile": "high_risk"}` |
| Track a whale | add form (`/wallets`) | `POST /api/wallets/add` `{"address": "...", "label": "..."}` |
| Untrack a whale | remove on the row (`/wallets`) | `POST /api/wallets/remove` `{"address": "..."}` |
| Fire a demo whale signal | `simulate whale buy` (`/wallets`) | `POST /api/simulate_whale_buy` |
| Train the advisory model | train button (`/ml`) | `POST /api/ml/train` |

Manual buys are fail-closed: without a fresh DexScreener quote the API returns
`400` and journals a `BLOCK`/`STALE_PRICE` decision instead of guessing a price.
`/api/config` can change sizing and risk settings at runtime but **cannot**
enable live trading; that only comes from `.env` and a restart.

The **Strategy profile** dropdown in the risk panel switches
`conservative`/`balanced`/`aggressive`/`high_risk` live. Selecting one applies
that profile's sizing and exit numbers immediately (even when `.env` pins the
keys — an explicit click is an explicit instruction), updates the `STANDARD` /
`HIGH RISK` badge next to it, refreshes the config hash and writes a
`RISK MGR` log line. The choice is stored in the `bot_state` table, so it
**survives a restart and wins over `STRATEGY_PROFILE`**; `STRATEGY_PROFILE` is
only the startup default. To let `.env` win again, select the profile you want
once (that selection is now stored) or delete the row:
`sqlite3 paper_portfolio.db "DELETE FROM bot_state WHERE key='strategy_profile'"`.

### Strategy profiles

`STRATEGY_PROFILE` fills the risk defaults for keys you have not set explicitly:

| Profile | Trade size | Max open | Stop | TP1 | Daily stops | Daily drawdown |
|---|---|---|---|---|---|---|
| `conservative` | 0.25 SOL | 2 | -18% | +60% | 1 | 3% |
| `balanced` | 0.5 SOL | 2 | -25% | +80% | 2 | 5% |
| `aggressive` | 0.75 SOL | 3 | -35% | +120% | 3 | 8% |
| `high_risk` | 1.5 SOL | 4 | -45% | +250% | 5 | 20% |

Explicit env values always win at **startup** — **except** for `high_risk`, which
is an explicit opt-in risk mode and therefore overrides the env sizing/exit keys.
Otherwise a `.env` that pins the balanced numbers would silently make the mode a
no-op. Switching the profile from the Command Center applies the profile numbers
regardless of `.env` (see [The Command Center](#the-command-center)).
`high_risk` also sells only 35% at TP1 (`TP1_FRACTION=0.35`), arms the
trailing stop at +60% and uses a wider 35% trail (25% after TP1), so the
remaining position can run. Aliases: `high-risk`, `highrisk`, `degen`.

The mode changes **how much is bet and how it is exited, not what is bought**:
liquidity, holder concentration, volume and mint/freeze filters are unchanged.
It is still paper trading unless `LIVE_TRADING_ENABLED=true` with the live
guards, which this profile does not touch. Startup prints a
`🔥 [RISK MODE] HIGH RISK / HIGH REWARD` banner, the dashboard reports
`strategyProfile.riskMode = "high"`, and `python -m bettercalltrader.doctor`
warns that the `.env` sizing/exit keys are being overridden.

The most useful knobs to tune after a few paper sessions are `TRADE_SIZE_SOL`,
`MAX_OPEN_POSITIONS`, `STOP_LOSS_PCT`, `TP1_PCT`, `TP1_FRACTION`,
`FINAL_TAKE_PROFIT_PCT`, `MAX_DAILY_TRADES`, `MAX_DAILY_DRAWDOWN_PCT`,
`MIN_LIQUIDITY_USD` and `MAX_TOP10_HOLDER_PCT`.

### Where to look when something happens

| Artifact | What it contains |
|---|---|
| terminal | live cycle log, AI/agent decisions and errors |
| `bot_history.log` | persistent text log |
| `bot_audit.jsonl` | JSONL events: `dry_run_buy`, `live_buy_blocked`, `live_tx_sent`, `live_tx_rpc_error`, `shutdown`, WS failures |
| `paper_portfolio.db` | SQLite paper book (schema `10.1.0`): positions, trades, journal, equity, pending confirmations |
| `GET /api/journal`, `/api/equity`, `/api/portfolio_risk`, `/api/backtest` | decision and performance views |
| `python py/replay_v10.py` | realized-trade replay report |

`JSON_LOGS=true` switches the file logger to structured JSON. `/api/audit`
returns the tail of the audit trail.

### Research before risking money

These commands are local, deterministic and never place an order. See
[OHLCV backtesting](#ohlcv-backtesting) for flags and examples.

```bash
# Backtest one parameter set
python -m bettercalltrader.ohlcv --csv examples/candles_sample.csv --strategy momentum

# Select parameters on train slices, then measure them out of sample
python -m bettercalltrader.ohlcv --csv candles.csv --optimize --objective sharpe

# Ask how much worse costs can get before the edge disappears
python -m bettercalltrader.ohlcv --csv candles.csv --stress-costs

# Rank momentum vs breakout on the same windows and costs
python -m bettercalltrader.ohlcv --csv candles.csv --compare
```

A positive research result is evidence about a rule, not a forecast. Treat
`--compare` as "which candidate held up best", never as "this will profit".

### Before arming live trading

Live trading is a deliberate, separate step:

1. Keep `LIVE_TRADING_ENABLED=false` until paper results and the readiness audit
   are clean.
2. Install the optional signing package: `pip install solders`.
3. Run `python -m bettercalltrader.live_readiness --probe` (or add `--env-file`).
   While live trading is still off it prints `PAPER`, and the probe must reach
   your RPC and Jupiter.
4. Set `SOLANA_RPC_URL`, `SOLANA_PRIVATE_KEY`, `LIVE_TRADING_ENABLED=true`,
   `LIVE_TRADING_CONFIRMED=true` and a small `MAX_LIVE_TRADE_SOL`.
5. Arm the stage, not the whole wallet: start with `LIVE_TRADING_STAGE=dry_run`
   (every live order is planned and audited but never sent), then `probe` with
   `LIVE_STAGE_MAX_TRADES=1` (one real attempt, then `BLOCKED`), and only then
   `full`. The budget is spent on the attempt, not on a successful fill, it is
   persisted per UTC day in `bot_state` (a restart does not reset it), and an
   unknown stage fails closed to `dry_run`. The stage is environment-only, so it
   cannot be widened from the browser.
6. Re-run the readiness audit. It must print `READY` with zero failed guard
   scenarios and a matching RPC. `READY` means the guards are armed, not that a
   trade is safe.
7. Read [RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) and start with the smallest
   size you can afford to lose.

### Operating checklists

**Every session**

- `python -m bettercalltrader.doctor` → `Result: OK`.
- Open `/ready` and `/metrics`; confirm the DB and AI are healthy.
- Watch the first cycle in the terminal and the journal.
- Review `bot_audit.jsonl` for unexpected `live_buy_blocked` or error events.

**Weekly**

- Run `./scripts/backup.sh` (or `.\scripts\backup.ps1`).
- `chmod 600 .env` and rotate `DASHBOARD_TOKEN` if it was ever shared.
- Review closed trades and exit reasons in `/analytics`.
- Re-run the research commands after changing strategy parameters.

**After pulling changes**

- Re-run `./run.sh` (it reinstalls only when pins change) and the full gates:
  `ruff check .`, `mypy`, `python py/test_v9.py`, `python py/smoke_v10.py`.

---

## What's in v9.0 (still active)

v9.0 keeps the v8 risk engine and adds time-based and step-up protection plus Telegram control:

- **Time-Stop**: closes a position if it has not reached the configured minimum profit after the configured hold time.
- **Step-Up Trailing**: raises the stop floor at configured profit milestones before TP1.
- **Telegram notifications and commands**: `/status`, `/positions`, `/journal`, `/digest`, `/sell_all`, `/reset_breaker`, `/help`.
- **Mint/freeze authority fix**: RugCheck `mintAuthority: null` is correctly treated as revoked.
- **v8 risk engine retained**: flow exits, whale exits, circuit breaker, confirmation before entry, realistic paper fills, dynamic sizing, cluster guard, wallet quality, decision journal, equity curve, stale-price protection, breakeven after TP1.

### Exit order (checked every monitor tick, 4s)

1. **WHALE_EXIT** 🐳: the entry whale sold this token.
2. **TAKE_PROFIT_1** 🎯: +80% from the entry fill sells `TP1_FRACTION` (50% by default).
3. **FAST_INVALIDATION** ⚡: -12% within the first 90s.
4. **STOP_LOSS** 🛑: -25%.
5. **TIME_STOP** ⏱️: no configured pump after the configured hold time.
6. **FLOW_EXIT** 💧: fresh flow, sells >= 3, sells > buys, and sells >= buys x 1.3.
7. **TRAILING_STOP** 📉 / **BREAKEVEN_STOP** 🛡️ / **STEP_UP_STOP** 🪜: stop = max(trailing stop, breakeven floor, step-up floors).
8. **TAKE_PROFIT** 🚀: optional `FINAL_TAKE_PROFIT_PCT` hard target for the remaining moonbag (0 disables it).

The same rules live in `bettercalltrader/exits.py` (`ExitPolicy`) and drive both
the paper book (`classify_tick`) and the OHLCV backtester (`intrabar`), so a
research run measures the policy the bot actually trades.

---

## Install / run (Linux / Ubuntu)

Requires **Python 3.11+** and the `venv` module:

```bash
sudo apt update && sudo apt install -y python3 python3-venv
python3 --version   # must be 3.11 or newer
```

> Do not `pip install` into the system interpreter. Debian/Ubuntu ship an
> externally managed Python (PEP 668) and the install will fail with
> `error: externally-managed-environment`. Always use the project virtualenv
> below.

### One-command start

```bash
cd ~/BetterCallTrader
./run.sh
```

`run.sh` is idempotent and does the whole setup on each run:

1. calls `scripts/bootstrap.sh` — checks for Python 3.11+ (override with
   `PYTHON_BIN=python3.12 ./run.sh`), creates `.venv/` on first run
   (`VENV_DIR=...` to change the location), installs `requirements.txt` and
   only reinstalls when that file changes,
2. warns if `.env` is missing,
3. runs `python -m bettercalltrader.doctor` and aborts on any `FAIL`,
4. starts the bot, printing the Command Center URL.

`scripts/bootstrap.sh` is the single setup path shared with CI, so a green pipeline
means the same install a developer gets locally (`./scripts/bootstrap.sh --dev` adds
ruff, mypy and pip-audit).

Useful flags: `./run.sh --skip-doctor`, `./run.sh --help`.

### Manual start

```bash
cd ~/BetterCallTrader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # runtime deps
pip install -r requirements-dev.txt      # optional: ruff, mypy, pip-audit

cp .env.example .env
$EDITOR .env                             # set GEMINI_API_KEY / DASHBOARD_TOKEN
                                         # keep LIVE_TRADING_ENABLED=false

python -m bettercalltrader.doctor            # must print: Result: OK
python py/research_bot.py
```

Open **http://localhost:8080** — the header shows **v13.0**. The dashboard binds
loopback (`127.0.0.1`) by default. To reach it from another machine, set
`COMMAND_CENTER_HOST=0.0.0.0` **and** a `DASHBOARD_TOKEN`: the bot refuses to
start on a non-loopback bind without a token (override with
`ALLOW_INSECURE_DASHBOARD=true` only on trusted networks).

Optional checks:

```bash
python py/test_v9.py        # prints ALL TESTS PASSED
python py/smoke_v10.py      # prints SMOKE OK
ruff check . && mypy     # lint + types
```

Optional backup and restore (the `.ps1` scripts are Windows-only):

```bash
./scripts/backup.sh                 # -> ~/BetterCallTraderBackup/<timestamp>/
./scripts/restore.sh                # newest backup, asks for confirmation
./scripts/restore.sh -y <backup-dir>
```

`scripts/backup.sh` also captures the SQLite `-wal`/`-shm` sidecars so a snapshot taken
while the bot runs is not torn. Pass a destination root as the first argument
to write elsewhere: `./scripts/backup.sh /media/usb/at-backups`.

Every later shell must activate the environment again
(`source .venv/bin/activate`), or call `.venv/bin/python py/research_bot.py`
directly.

---

## Install / run (Windows PowerShell)

Requires **Python 3.11+** (install from python.org and tick *Add python.exe to
PATH*) and PowerShell 5.1+. The bot always runs inside a project virtualenv, so
the install never touches the system interpreter.

```powershell
git clone https://github.com/BetterCallDzuks/BetterCallTrader.git
cd BetterCallTrader
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If `Activate.ps1` is blocked by the execution policy, either allow it for the
current user (`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`) or skip
activation and call the interpreter directly:
`.\.venv\Scripts\python.exe -m pip install -r requirements.txt`.

1. Create `.env` from `.env.example` and keep `LIVE_TRADING_ENABLED=false`:

```powershell
Copy-Item .env.example .env
notepad .env
```

2. Run the pre-flight checks and fix every `FAIL` line before starting:

```powershell
python -m bettercalltrader.doctor
```

The doctor validates numbers, live-trading guards, the dashboard token, the
database schema, the audit log path, the timezone and (optionally) whether the
port is free. It is read-only and never sends an order.

Before changing `LIVE_TRADING_ENABLED`, run the live-execution readiness audit.
It drives the real `execute_buy` guards with a session that refuses every
request, so it verifies the fail-closed paths without a key or a socket:

```powershell
python -m bettercalltrader.live_readiness
python -m bettercalltrader.live_readiness --json
python -m bettercalltrader.live_readiness --probe   # opt-in network probes
```

It prints `PAPER` (exit 0), `READY` (exit 0, live is armed and the guards
passed) or `BLOCKED` (exit 1) with the failing configuration keys. It never
places an order and never prints the private key.

3. Run the bot and open the dashboard:

```powershell
python py/research_bot.py
```

Open **http://127.0.0.1:8080** — the header shows **v13.0**. `START_BOT.bat` is
a convenience wrapper that runs `py/research_bot.py` from its own folder, using
`.venv\Scripts\python.exe` when the virtualenv exists and `python` otherwise.

4. Optional checks:

```powershell
python py/test_v9.py
python py/smoke_v10.py
```

The first runs local self-tests and prints `ALL TESTS PASSED`; the second starts
the real API on an ephemeral port and prints `SMOKE OK`.

5. Optional backup and restore:

```powershell
.\scripts\backup.ps1
.\scripts\restore.ps1
```

---

## Project layout

```
BetterCallTrader/
├── py/
│   ├── research_bot.py   # Python multi-agent bot + local web/API server (http://localhost:8080)
│   ├── replay_v10.py     # v10 realized-trade replay report
│   ├── test_v9.py        # local self-tests (v9 – v12)
│   └── smoke_v10.py      # end-to-end API smoke test (real aiohttp app, temp DB)
├── html/
│   ├── command_center.html  # / : Web Command Center, live-synced with the bot
│   ├── analytics.html       # /analytics : equity, risk, exits, ML
│   ├── risk.html            # /risk : circuit breaker, entry gate, sizing, concentration
│   ├── wallets.html         # /wallets : tracked whales, wallet quality, open positions
│   ├── backtest.html        # /backtest : realized-trade replay, equity curve, breakdowns
│   ├── ml.html              # /ml : advisory model status, training, per-mint prediction
│   ├── system.html          # /system : readiness, metrics, Prometheus text, audit trail
│   ├── settings.html        # /settings : runtime knob editor + strategy profile
│   └── webui/
│       ├── console.js       # shared console runtime: header/nav, i18n, token, api(), formatters
│       └── theme.css        # shared console theme and components
├── scripts/
│   ├── bootstrap.sh      # shared setup: venv + pinned deps (used by run.sh and CI)
│   ├── backup.sh         # backup helper for DB, .env and log (Linux/macOS, incl. WAL sidecars)
│   ├── restore.sh        # restore helper from a backup folder (Linux/macOS)
│   ├── backup.ps1        # backup helper (Windows)
│   └── restore.ps1       # restore helper (Windows)
├── docs/
│   ├── CHANGELOG.md      # version history
│   ├── SECURITY.md       # vulnerability reporting and safety posture
│   └── RELEASE_CHECKLIST.md  # pre-release/live-trading checklist
├── run.sh                # Linux/macOS launcher: bootstrap + doctor + start
├── START_BOT.bat         # Windows launcher
├── requirements.txt      # pinned runtime dependencies
├── requirements-dev.txt  # Ruff, mypy, pip-audit
├── pyproject.toml        # Ruff/mypy/pytest configuration
├── Dockerfile            # optional container image
├── docker-compose.yml    # optional container orchestration
├── examples/             # sample candle data for the OHLCV backtester
├── .github/workflows/tests.yml
├── .github/workflows/codeql.yml
├── .github/dependabot.yml
├── bettercalltrader/     # modular package imported by py/research_bot.py
│   ├── config.py         # env helpers (env_bool, env_float, ...)
│   ├── http.py           # retry/backoff helpers
│   ├── models.py         # Pydantic models
│   ├── telegram.py       # Telegram notifier + inline buttons
│   ├── defaults.py       # risk/cost/exit defaults and their validation (v11)
│   ├── migrations.py     # schema_version / migrations (v10)
│   ├── metrics.py        # metrics collection (v10)
│   ├── observability.py  # JSON/audit logging + alert throttle (v10)
│   ├── risk_analytics.py # VaR, drawdown, profit factor, HHI (v10)
│   ├── backtest.py       # realized replay report (v10)
│   ├── security.py       # optional token auth + rate limit + origin policy (v10/v11)
│   ├── strategies.py     # risk profiles (v10)
│   ├── ml.py             # advisory logistic model (v10)
│   ├── price_feed.py     # TTL price cache (v10)
│   ├── prometheus.py     # metrics registry and text exposition (v11)
│   ├── shutdown.py       # SIGINT/SIGTERM controller (v11)
│   ├── ohlcv.py          # candle backtester + strategies + CLI (v11)
│   ├── validate.py       # environment validation rules (v11)
│   ├── doctor.py         # read-only pre-flight CLI (v11)
│   ├── timeutils.py      # UTC/local day helpers (v11)
│   ├── fills.py          # paper fill model + exit metadata (v11)
│   ├── costs.py          # shared cost model: fees, slippage, depth impact (v11/v12)
│   ├── exits.py          # one ExitPolicy for the paper book and the backtester (v12)
│   ├── llm.py            # provider-neutral LLM client (Gemini / OpenAI-compatible) (v12)
│   ├── optimizer.py      # walk-forward param search + rolling stability (v12)
│   ├── cost_stress.py    # cost multiplier sweep + break-even (v12)
│   ├── compare.py        # out-of-sample strategy ranking (v12)
│   └── live_readiness.py # read-only live-execution guard audit (v12)
├── .env                  # your local config and keys (never share it)
├── .env.example          # template with every setting
├── paper_portfolio.db    # SQLite paper book (created automatically)
├── bot_history.log       # persistent file log
├── README.md
└── .gitignore
```

## Agents

1. **Discovery** (`DiscoveryAgent`, `PumpFunStreamAgent`): Pump.fun WebSocket (new tokens, migrations, tracked-whale trades) and DexScreener scans. Also records whale sells, which feed `WHALE_EXIT`.
2. **Research** (`ResearchAgent`): free hard filters, then an LLM (Gemini or a local OpenAI-compatible model, or a strict heuristic when none is configured) returns BUY / WATCH / SKIP with risk and hype scores.

### Local AI models

`bettercalltrader/llm.py` treats every backend the same way. `LLM_PROVIDER=auto`
(default) uses Gemini when `GEMINI_API_KEY` is set, otherwise a local server
when `LLM_BASE_URL` is set, otherwise heuristics. An explicitly configured
provider falls back to the other configured provider before heuristics.

```bash
# Ollama
ollama serve
ollama pull llama3.1:8b
LLM_PROVIDER=ollama LLM_BASE_URL=http://127.0.0.1:11434/v1 LLM_MODEL=llama3.1:8b ./run.sh

# LM Studio (start the server in the app or with `lms server start`)
LLM_PROVIDER=openai LLM_BASE_URL=http://127.0.0.1:1234/v1 \
  LLM_MODEL=qwen2.5-coder-7b-instruct \
  LLM_MODELS=qwen2.5-coder-14b-instruct-uncensored,qwen3.5-0.8b-uncensored ./run.sh

# llama.cpp server, vLLM or any OpenAI-compatible gateway
LLM_PROVIDER=openai LLM_BASE_URL=http://127.0.0.1:8081/v1 LLM_MODEL=local-model ./run.sh
```

`LLM_MODELS` is an optional comma-separated list of local model IDs tried in
order after `LLM_MODEL`, so the bot keeps working when the primary model is not
loaded. Model IDs are exactly what `GET /v1/models` advertises (for LM Studio:
`qwen2.5-coder-7b-instruct`, `qwen2.5-coder-14b-instruct-uncensored`, …).

`LLM_TIMEOUT_SEC`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS` and
`LLM_FALLBACK_TO_HEURISTIC` tune the call. `doctor` probes the configured
endpoint and reports a missing model or an unreachable server without spending
tokens; `GET /api/state` exposes the resolved provider chain and whether the AI
is local under `config.llm`.

`./run.sh` manages LM Studio for you: when `LLM_BASE_URL` points at
`127.0.0.1`/`localhost` on LM Studio's default port (`1234`), it runs
`lms server start` before the bot and `lms server stop` when the bot exits. A
server that was already running is left untouched. Set `LMS_AUTOSTART=true` to
manage a custom local port, or `LMS_AUTOSTART=false` to disable it (the `auto`
default never touches Ollama/llama.cpp/vLLM ports).

The dashboard lets you switch the active model without editing `.env`:

- **Settings → LLM provider** — choose `auto`, `gemini`, or a specific local
  model; the choice is persisted in SQLite and re-applied on startup.
- **ML / AI → Local AI models** — list every model the server advertises, see
  which ones are loaded, **Load & test** (JIT-loads and sends a tiny JSON
  prompt), and **Use for research** to pin that model at the front of the chain.

Selection endpoints: `GET /api/llm/models`, `POST /api/llm/select`
(`{provider, model}`), `POST /api/llm/test` (`{model}`).

**Reliability.** Each provider has its own circuit breaker: after
`LLM_BREAKER_THRESHOLD` consecutive failures (default 3) it is skipped for
`LLM_BREAKER_COOLDOWN_SEC` (default 60) instead of being retried on every
research call. A malformed JSON reply gets one corrective retry on the same
model (`LLM_JSON_RETRY=false` to disable), and the parser repairs trailing
commas, comments, Python literals, fences, truncated tails and two concatenated
objects before giving up. `GET /api/llm/status` reports calls, failures,
skipped calls, average latency and breaker state per provider; the same numbers
are exported as `bettercalltrader_llm_*` Prometheus gauges.

**Memory.** `LMS_MAX_LOADED` (default `1`) keeps only the selected model
resident: pinning a local model unloads the others (`LMS_ENFORCE_ON_SELECT=false`
to opt out). `GET /api/llm/loaded` lists the resident models and
`POST /api/llm/unload` (`{model}` or `{all: true}`) ejects them — the same
actions as the **Unload** / **Unload all** buttons in ML / AI.

**Measure before pinning.** `py/eval_models.py` replays the live research prompt
over the recorded scans for every candidate model and reports JSON validity,
latency and the decision mix. It never trades and never writes to the database:

```bash
python py/eval_models.py --limit 10                     # every model in LLM_MODEL(S)
python py/eval_models.py --baseline gemini --out docs/MODEL_EVAL.md
```

A snapshot from this checkout is committed at
[docs/MODEL_EVAL.md](docs/MODEL_EVAL.md): on the four most recent recorded
cases the 7B local model parsed 4/4 raw replies and agreed with 100% of the
recorded decisions, while the 0.8B model failed raw JSON on 3/4 (the live client
would repair and retry; the report measures the worse case). Gemini returned
`503 UNAVAILABLE` when the snapshot was taken, so that baseline is pending —
rerun the command above when it is reachable. Regenerate and diff the report
after any prompt or model change instead of trusting a single run.
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
| `GET /api/ml/status` | ML model contract state (`ok`/`stale`/`missing`), reasons and summary |
| `GET /api/ml/predict?mint=...` | advisory buy/skip probability; `409` with staleness reasons instead of scoring a stale model |
| `POST /api/ml/train` | retrain the advisory model from closed paper trades |
| `GET /api/llm/models` | local server model list (LM Studio REST, then OpenAI `/models`), loaded flags and active selection |
| `POST /api/llm/select` | choose the research LLM: `{provider: auto\|gemini\|local, model}`; persisted in `bot_state` |
| `POST /api/llm/test` | JIT-load and test one local model `{model}` with a tiny JSON completion |
| `GET /api/llm/status` | per-provider health: calls, failures, skipped calls, latency, breaker state |
| `GET /api/llm/loaded` | models currently resident in LM Studio (`lms ps`) |
| `POST /api/llm/unload` | unload one model `{model}` or all of them `{all: true}` |
| `GET /risk` `/wallets` `/backtest` `/ml` `/system` `/settings` | console windows (static pages, no token required) |
| `POST /api/wallets/add` / `POST /api/wallets/remove` | track / untrack a whale `{address, label}` |
| `POST /api/simulate_whale_buy` | fire a demo whale signal through the normal pipeline |
| `GET /analytics` | dedicated analytics dashboard page |
| `POST /api/breaker/reset` | manual breaker reset |
| `POST /api/confirmations/cancel` | cancel a pending entry `{mint}` |
| `POST /api/config` | change runtime settings (never changes live trading) |
| `POST /api/buy` / `POST /api/sell` | manual paper buy / manual close |

## Modular package (`bettercalltrader/`)

The project is being migrated incrementally from the original monolith into a small package:

- `bettercalltrader/config.py` — environment variable helpers
- `bettercalltrader/http.py` — retry/backoff helpers for HTTP calls
- `bettercalltrader/models.py` — Pydantic models such as `TokenAnalysisDecision`
- `bettercalltrader/telegram.py` — Telegram notifier and inline buttons
- `bettercalltrader/migrations.py` — schema version and migrations (v10)
- `bettercalltrader/metrics.py` — aggregate metrics (v10)
- `bettercalltrader/observability.py` — file/JSON logger, JSONL audit trail, alert throttle (v10)
- `bettercalltrader/risk_analytics.py` — drawdown, VaR, expected shortfall, profit factor, HHI (v10)
- `bettercalltrader/backtest.py` — realized-trade replay report (v10)
- `bettercalltrader/security.py` — optional dashboard token and rate limiter (v10)
- `bettercalltrader/strategies.py` — conservative/balanced/aggressive/`high_risk` risk profiles (v10, high-risk mode v12)
- `bettercalltrader/ml.py` — dependency-free logistic advisory model (v10)
- `bettercalltrader/price_feed.py` — TTL price cache shared by the scan and monitor loops (v10)
- `bettercalltrader/prometheus.py` — dependency-free counters/gauges/histograms and text exposition (v11)
- `bettercalltrader/shutdown.py` — cross-platform SIGINT/SIGTERM controller (v11)
- `bettercalltrader/ohlcv.py` — event-driven OHLCV backtester, strategy library and walk-forward splits (v11)
- `bettercalltrader/exits.py` — one shared exit policy for the paper book and the backtester (v11)
- `bettercalltrader/costs.py` — one shared transaction-cost model with depth decay (v11)
- `bettercalltrader/llm.py` — provider-neutral LLM client: Gemini or local OpenAI-compatible servers (v11)
- `bettercalltrader/optimizer.py` — walk-forward parameter grid search with fixed/anchored/rolling windows, stability scoring and baseline uplift (v12)
- `bettercalltrader/cost_stress.py` — cost multiplier sweep and interpolated break-even (v12)
- `bettercalltrader/compare.py` — optimize, cost-stress and rank several strategies in one report (v12)
- `bettercalltrader/live_readiness.py` — read-only live-execution readiness audit with a no-network guard harness (v12)

`py/research_bot.py` remains the main entry point and imports these components. The goal is to make future upgrades, tests and maintenance easier without changing trading behavior.

## Continuous integration

GitHub Actions workflow is located at:

```text
.github/workflows/tests.yml
```

It runs on every push and pull request and has four jobs:

- **lint**: `bash -n` over the shell entry points, Ruff (`E4`, `E7`, `E9`, `F`) plus mypy over the `bettercalltrader` package
- **test**: Python 3.11 and 3.12 on Ubuntu and Windows; runs `python py/test_v9.py` and `python py/smoke_v10.py`
- **launcher**: boots the app through `./run.sh` on Linux and asserts the dashboard answers on its port
- **audit**: `pip-audit` against the pinned runtime dependencies

Every job installs through `scripts/bootstrap.sh`, the same script `./run.sh` uses, so
CI exercises the identical venv + pinned-dependency path that a local checkout
does. Dependencies are fully pinned in `requirements.txt`; tooling lives in
`requirements-dev.txt`. Optional local hooks are configured in
`.pre-commit-config.yaml`.

## Docker (optional)

Build and run locally:

```powershell
docker compose up --build
```

The container must bind `0.0.0.0` for the published port to work, so
`docker-compose.yml` **requires** `DASHBOARD_TOKEN` — compose refuses to start
without it (`${DASHBOARD_TOKEN:?…}`). Export it before building:

```bash
export DASHBOARD_TOKEN="$(openssl rand -hex 24)"
docker compose up --build
```

The image keeps mutable state in `/app/data`, which compose bind-mounts to
`./data` (created automatically, so a fresh checkout works without
pre-creating files):

- `data/paper_portfolio.db` — paper book
- `data/bot_history.log` — text log
- `data/bot_audit.jsonl` — audit trail
- `data/ml_model.json` — advisory ML model

`.env` is optional for the container (the exported token is enough); use it
for `GEMINI_API_KEY` or other overrides. `./scripts/backup.sh` and `.\scripts\backup.ps1`
include `./data` automatically. Live trading remains disabled unless
explicitly configured in `.env`.

The image runs as the unprivileged `bct` user (UID/GID 1000 by default), and
the compose service drops every capability, sets `no-new-privileges`, mounts
the root filesystem read-only and gives `/tmp` a tmpfs. Only `/app/data` is
writable. If your host user is not UID 1000, rebuild with matching IDs, or fix
the bind mount once:

```bash
docker compose build --build-arg BCT_UID="$(id -u)" --build-arg BCT_GID="$(id -g)"
sudo chown -R 1000:1000 data   # or the UID/GID used above
```

## Backup and restore

Create a timestamped backup:

```powershell
.\scripts\backup.ps1
```

```bash
./scripts/backup.sh            # Linux/macOS, also grabs SQLite -wal/-shm sidecars
```

Restore from the latest backup:

```powershell
.\scripts\restore.ps1
```

```bash
./scripts/restore.sh           # add -y to skip the confirmation prompt
```

Or restore from a specific folder:

```powershell
.\scripts\restore.ps1 -BackupPath "C:\Users\LiveTony\Desktop\BetterCallTraderBackup\20260101_120000"
```

```bash
./scripts/restore.sh ~/BetterCallTraderBackup/20260101_120000
```

The restore script asks for confirmation before overwriting local files.

Both helpers cover the two supported layouts: files in the project root (a
local `python py/research_bot.py` run) and the Docker Compose `data/` directory.
Backups store the container state as `data/` inside the timestamped folder.

## Telegram bot (buttons and digest)

The Telegram integration now uses inline buttons instead of slash commands:

- Status, positions, journal, digest, sell all, reset breaker, help
- Analyze mint / buy mint prompt buttons, followed by sending the mint address
- Button callbacks are handled through Telegram `callback_query` updates

Slash commands are still supported for compatibility. `/digest` (or the **🧾
Digest** button) sends a compact snapshot: balance/equity, open positions,
realized PnL, circuit-breaker state, today's trades/PnL/losing stops, per-provider
LLM health and the first three open positions. Set `DIGEST_INTERVAL_SEC` to push
the same digest periodically from the confirmation loop (`0` disables it).

## v10.0 progress

The repository now includes the full v10 layer:

**Data & risk**
- `schema_version` table and migration helper (`bettercalltrader/migrations.py`), current schema `10.1.0`
- aggregate metrics (`bettercalltrader/metrics.py`) and a shared risk-analytics library
- portfolio risk now reports HHI concentration, top position weight, open exposure %, max drawdown, VaR95, expected shortfall, profit factor, expectancy and average win/loss
- realized-trade backtest report (`bettercalltrader/backtest.py`, `py/replay_v10.py`, `GET /api/backtest`)

**Execution safety**
- `LIVE_TRADING_CONFIRMED` and `MAX_LIVE_TRADE_SOL` guards in `SolanaExecutionAgent.execute_buy`
- JSONL audit trail for dry-runs, blocked live calls, sent transactions and RPC errors
- deduplicated Telegram alerts for circuit breaker and stream failures

**Operations**
- optional `DASHBOARD_TOKEN` (sent as `X-Auth-Token` or `?token=`) and per-IP rate limiting
- structured JSON logging option and `/api/audit`
- strategy profiles: `STRATEGY_PROFILE=conservative|balanced|aggressive|high_risk`; env values win except for the opt-in `high_risk` risk mode, which overrides them
- TTL price cache to cut duplicate DexScreener calls (`PRICE_CACHE_ENABLED`, `PRICE_CACHE_TTL_SEC`)

**Analytics**
- dependency-free logistic advisory model (`bettercalltrader/ml.py`); it never changes sizing or trade decisions
- dedicated analytics dashboard at `/analytics` (equity curve, risk ledger, concentration, exit anatomy, daily PnL, ML weights, audit feed, recent trades)

These additions are intentionally non-breaking: v9 behavior remains the default. All new security features are opt-in.

## v11.0 progress

- **Reproducible builds**: exact pins in `requirements.txt`, separate dev tooling, `pyproject.toml` with Ruff/mypy settings, and a CI matrix (Ubuntu + Windows, Python 3.11 + 3.12).
- **Research**: event-driven OHLCV backtester (`bettercalltrader/ohlcv.py`) with CSV/JSON/GeckoTerminal loaders, momentum and breakout strategies, fee/slippage/latency modelling, walk-forward splits and walk-forward parameter optimization (`--optimize`, `bettercalltrader/optimizer.py`). `--compare` runs several strategies through the same optimizer and cost stress and ranks them in one artifact (`bettercalltrader/compare.py`). See `examples/candles_sample.csv`.
- **Observability**: dependency-free Prometheus registry, `GET /metrics` (counters, latency histogram, DB-derived gauges, breaker state, uptime) and `GET /ready` for dependency checks. Both honour `DASHBOARD_TOKEN`.
- **Graceful shutdown**: `SIGINT`/`SIGTERM` stop new cycles, cancel monitor/confirmation tasks, close the aiohttp runner and append a `shutdown` audit event.
- **Safer browser surface**: CORS is now an allowlist (`ALLOWED_ORIGINS`, localhost on any port by default) instead of `*`; responses carry `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy` and a CSP for HTML pages. `CORS_ALLOW_ALL=true` restores the old behaviour.
- **Dependency auditing**: `pip-audit` runs in CI on every push.
- **Config validation**: `bettercalltrader/validate.py` checks numeric ranges, live-trading guards, token strength, profiles and timezones; `python -m bettercalltrader.doctor` adds package, database-schema, audit-path and port checks and exits non-zero on any `FAIL`.
- **Bind control**: `COMMAND_CENTER_HOST` (default `127.0.0.1`) and `COMMAND_CENTER_PORT` (default `8080`) keep the dashboard loopback-only out of the box. A non-loopback bind without `DASHBOARD_TOKEN` is refused at startup (doctor exits non-zero); `ALLOW_INSECURE_DASHBOARD=true` is the explicit escape hatch. `X-Forwarded-For` is only honoured when `TRUST_PROXY_HEADERS=true`. `PAPER_DB_PATH` selects the portfolio database.
- **Supply chain**: Dependabot (pip + Actions), CodeQL (`security-and-quality`) and a `docs/SECURITY.md` policy.
- **Incremental extraction**: time helpers and the paper fill/exit model now live in `bettercalltrader/timeutils.py` and `bettercalltrader/fills.py`, re-exported by `py/research_bot.py` so existing imports keep working.
- **Crash-safe confirmations**: pending entries are written to the `pending_confirmations` table (schema `10.1.0`) and restored on startup. Entries older than `ENTRY_CONFIRM_RECOVERY_MAX_SEC` (default 900s) are dropped as `CONFIRM_EXPIRED` instead of being resumed with a stale signal.
- **Pump.fun curve pricing**: with `PUMPFUN_CURVE_ENABLED=true`, a token that has no DexScreener pair yet is quoted from the Pump.fun bonding curve (`frontend-api-v3.pump.fun/coins/<mint>`), so it can pass entry confirmation and manual buys. Quotes are cached for `PUMPFUN_CURVE_CACHE_SEC` (5s) and misses for `PUMPFUN_CURVE_MISS_CACHE_SEC` (60s).

```powershell
# scrape metrics locally
curl.exe -H "X-Auth-Token: $env:DASHBOARD_TOKEN" http://127.0.0.1:8080/metrics

# override the browser allowlist (empty = localhost only)
$env:ALLOWED_ORIGINS="https://my-dashboard.example.com"

# validate configuration before starting the bot
python -m bettercalltrader.doctor
```

### Verifying v10

```powershell
$env:PYTHONIOENCODING='utf-8'
python py/test_v9.py      # unit + integration self-tests
python py/smoke_v10.py    # starts the real API on port 18080 and checks every endpoint
python py/replay_v10.py   # realized-trade replay report
python -m bettercalltrader.ml status
python -m bettercalltrader.ohlcv --help   # candle backtester CLI
```

## Roadmap

[`docs/ROADMAP.md`](docs/ROADMAP.md) separates shipped work from candidate work
(probe-budget persistence, readiness drift alerts, a committed model-eval
baseline, backup reconciliation) and lists the explicit non-goals. It is a
plan, not a promise of returns.

## v13.0 progress

v13 hardens the two places where the bot can lose money without placing a bad
trade: a malformed model reply and an over-eager live arm. Paper trading is
still the default.

- **Local model operations.** `run.sh` starts LM Studio and stops it on exit
  (leaving an already-running server alone), the header can pick Gemini, `auto`
  or a specific local model, and **ML / AI** lists, loads, tests and pins local
  models. `LLM_MODELS` adds an ordered fallback chain after `LLM_MODEL`.
- **LLM reliability.** Each provider gets its own circuit breaker with a
  half-open probe; malformed JSON is repaired first and retried once. The
  research prompt lives in `bettercalltrader/prompts.py`, so the live bot and
  `py/eval_models.py` send byte-identical requests and can be compared.
- **Staged go-live arming.** `LIVE_TRADING_STAGE=dry_run` plans and audits every
  order without a socket, `probe` spends a one-shot `LIVE_STAGE_MAX_TRADES`
  budget and then blocks, and `full` is the old behaviour. An unknown value
  fails closed, the switch is environment-only, and the **System** page shows
  the active stage (read-only) next to the live-trading pill.
- **Two-factor auth for writes.** `DASHBOARD_TOTP_SECRET` requires a current
  RFC 6238 code in `X-TOTP` on `POST`/`PUT`/`PATCH`/`DELETE`; `GET` polling stays
  token-only. The console prompts and retries once.
- **Telegram digest.** `/digest` (and the **🧾 Digest** button) reports balance,
  open positions, realized PnL, breaker state, today's trades, per-provider LLM
  health and the top positions; `DIGEST_INTERVAL_SEC` pushes it on a schedule.
- **Fine-tune data export.** `py/export_training.py` turns recorded verdicts into
  chat-format JSONL using the exact live prompt and schema, with label/decision/
  outcome/dedup filters, a validation split and an alpaca format. GGUF weights
  cannot be trained in-process, so the bot exports data instead of pretending.
- **Parity and comparison tests.** A replay from a WAL-backed snapshot must
  rebuild exactly the book the live path recorded, and the strategy comparison
  test proves fixed and rolling windows diverge on a regime shift.

### Evidence layer (v12.0)

v12 turned the research and execution-readiness layers from "one backtest" into
evidence that is harder to fool, without changing the paper-trading default:

- **Rolling and anchored walk-forward windows** (`--window-mode rolling|anchored`,
  `--train-candles`, `--test-candles`) with a per-window stability score, so a
  parameter set must survive more than one lucky split.
- **Cost stress and break-even** (`--stress-costs`, `--cost-multipliers`): sweeps
  fees, slippage, impact and failed-transaction probability and reports how much
  worse execution can get before the edge disappears.
- **Out-of-sample strategy comparison** (`--compare`, `--compare-strategies`):
  runs momentum and breakout through the same optimizer and cost sweep, then
  ranks them by objective, break-even, stability and name in one report
  (`bettercalltrader/compare.py`).
- **Live-execution readiness audit** (`python -m bettercalltrader.live_readiness
  [--json] [--probe]`): drives the real `execute_buy` guards against a session
  that refuses every request, proving `DRY_RUN_READY`, `BLOCKED` and `ERROR`
  paths with zero network calls. Reports `PAPER`/`READY`/`BLOCKED`, never prints
  the private key, and only touches the network with `--probe`.
- **Consistent cost model everywhere**: the paper book, the OHLCV backtester,
  cost stress, the optimizer, the comparison and the ML labels all share
  `COST_MODEL_VERSION` and `bettercalltrader/costs.py`, so a research result is
  measured against the same round-trip cost the bot actually pays.

None of this predicts profit. It makes the failures cheaper: you can reject a
strategy, or refuse to arm the live path, before it costs anything.

## OHLCV backtesting

`bettercalltrader/ohlcv.py` is a real candle backtester, separate from the realized-trade replay:

- loads CSV, JSON, or GeckoTerminal USD candles
- long-only, one position at a time
- signal at candle close, fill at a later candle open (no look-ahead)
- configurable AMM fee, entry/exit slippage, priority fee, failed-transaction probability, liquidity impact, latency, stop loss, take profit, trailing stop and time stop
- one shared `CostModel` (`bettercalltrader/costs.py`) for the paper book, the backtester and the ML labels, so a round trip costs the same everywhere
- depth-aware impact: the exit pays for the share of the pool the entry consumed (`--impact-exponent`, default linear; `0.5` for the square-root AMM curve)
- one shared `ExitPolicy` (`bettercalltrader/exits.py`): take profit is partial (`--tp1-fraction`, 50% by default) and the remainder runs on the same stop ladder as the paper book
- optional volume-participation cap (`--max-participation`): a thin candle fills a smaller position instead of a fantasy fill
- gap-aware exits: a stop gaps to `min(open, stop)`, take profit to `max(open, tp)`
- trailing stops use the peak before the current candle; the entry candle cannot exit the position it opened
- if a stop and take profit share a candle, the stop wins and the trade is flagged `ambiguousBar`
- reports return, max drawdown, profit factor, expectancy, exposure, fees, total costs, per-trade Sharpe, exit reasons and a `configHash`
- `--walk-forward N` evaluates sequential out-of-sample segments with one fixed parameter set
- `--optimize` does the other half: it grid-searches the parameters on each train slice and trades only the winner on the untouched test slice, then reports the out-of-sample aggregate against the unoptimized `--fast/--slow/--lookback/--threshold` baseline (`bettercalltrader/optimizer.py`)

```powershell
# CSV columns: ts,open,high,low,close,volume (ISO timestamps or epoch seconds)
python -m bettercalltrader.ohlcv --csv candles.csv --strategy momentum --fast 5 --slow 13
python -m bettercalltrader.ohlcv --json candles.json --strategy breakout --lookback 30
python -m bettercalltrader.ohlcv --pool <geckoterminal-pool> --timeframe hour --walk-forward 3
python -m bettercalltrader.ohlcv --csv candles.csv --output json --save result.json

# Partial TP1 (50%), a 200% moonbag target and a 5% volume cap
python -m bettercalltrader.ohlcv --csv candles.csv --take-profit 80 --tp1-fraction 0.5 \
  --final-take-profit 200 --max-participation 0.05

# Exit the whole position at TP1, and opt into the paper book's 900s time stop
python -m bettercalltrader.ohlcv --csv candles.csv --tp1-fraction 1.0 --time-stop

# Walk-forward optimization: 3 segments, maximize Sharpe, custom grids
python -m bettercalltrader.ohlcv --csv candles.csv --optimize --objective sharpe \
  --fast-grid 5,9,13 --slow-grid 13,21,34 --min-trades 5

python -m bettercalltrader.ohlcv --csv candles.csv --strategy breakout --optimize \
  --lookback-grid 10,20,30,55 --threshold-grid 0.0,0.5,1.0 --output json --save opt.json
```

`--optimize` reuses `--walk-forward` for the segment count (default 3 when it is
0) and `--train-ratio` for the split. Objectives: `pnl`, `return_pct`,
`profit_factor`, `expectancy`, `sharpe`. A candidate is only eligible when it
produced at least `--min-trades` trades on the train slice; a slice with no
eligible candidate is reported as skipped instead of being filled with an
untested winner.

### Window modes and stability

`--window-mode` controls how the optimizer slices the series:

- `fixed` (default) — `segments` equal blocks, each cut at `--train-ratio`.
- `anchored` — the train window always starts at the first candle and grows; the
  test window steps forward (`--train-candles`, `--test-candles`).
- `rolling` — a constant-size train window slides forward with the test window.

`--train-candles 0` / `--test-candles 0` derive sizes from the series length
(`max(60, n/3)` and `max(20, n/8)`). Every report carries a `stability` block:

- `dominantSharePct` — how often the most-picked parameter set won
- `profitableSharePct` — share of traded windows with positive test PnL
- `paramSets` — every picked set with its window count and share
- `bestWindowPnlSol` / `medianWindowPnlSol` / `worstWindowPnlSol` / `pnlStdevSol`
- `consistency` — `stable` when both shares are at least 60%, `unstable` when
  either is below 40%, otherwise `mixed`

```bash
python -m bettercalltrader.ohlcv --csv candles.csv --optimize \
  --window-mode rolling --train-candles 240 --test-candles 90 --objective sharpe
python -m bettercalltrader.ohlcv --csv candles.csv --optimize --window-mode anchored
```

`--window-mode`, `--train-candles` and `--test-candles` only apply to
`--optimize` and `--compare` (and the candle sizes only to
`anchored`/`rolling`); the CLI errors instead of silently ignoring them.

### Cost stress and break-even

`--stress-costs` replays one parameter set across a sweep of cost multipliers
and reports where the edge disappears. Multiplier `0` removes every modelled
cost (gross edge), `1` is the normal cost level, and larger values stress a
busier pool or a worse slot:

```bash
python -m bettercalltrader.ohlcv --csv candles.csv --stress-costs
python -m bettercalltrader.ohlcv --csv candles.csv --stress-costs \
  --cost-multipliers 0,0.5,1,2,4,8 --cost-baseline 1
python -m bettercalltrader.ohlcv --strategy breakout --csv candles.csv --stress-costs --output json
```

The report scales the AMM fee, entry/exit slippage, impact coefficient and
priority fee linearly, clamps `failed_tx_prob` to `[0, 1]`, and interpolates the
**break-even multiplier** between the adjacent points where net PnL crosses
zero. `headroomPct` is the margin between the baseline and break-even: negative
means the current cost assumptions already erase the edge. A strategy that is
already unprofitable with zero costs is reported as `unprofitable-at-zero-cost`,
never given a break-even. `--stress-costs` is mutually exclusive with
`--optimize` and `--compare` and does not combine with `--walk-forward`.

### Strategy comparison

`--compare` answers "which of these strategies actually holds up?" instead of
"which parameter set won one backtest". Every strategy is run through the same
walk-forward optimizer (so params are always picked on train slices) and the
same cost sweep, then ranked on out-of-sample, cost-adjusted evidence:

```bash
python -m bettercalltrader.ohlcv --csv candles.csv --compare
python -m bettercalltrader.ohlcv --csv candles.csv --compare \
  --compare-strategies momentum,breakout --objective sharpe \
  --window-mode rolling --train-candles 240 --test-candles 90
python -m bettercalltrader.ohlcv --csv candles.csv --compare --output json --save compare.json
```

The ranking key is fixed and deterministic: the chosen `--objective` on the
out-of-sample aggregate first, then the break-even cost multiplier (a wider
margin wins ties, a strategy with no break-even ranks last), then stability
(`stable` > `mixed` > `unstable`), then the strategy name. Each row shows net
PnL, return, trades, profit factor, max drawdown, stability, break-even and the
uplift over that strategy's unoptimized parameters. `--compare` is mutually
exclusive with `--optimize` and `--stress-costs`; it reuses the optimizer's
grid, window, `--min-trades` and `--objective` flags. `--compare-strategies`
defaults to `momentum,breakout` and rejects unknown names. A comparison is a
research artifact, not a recommendation: two strategies can both lose money and
still be ranked.

This engine ships with a momentum (EMA cross) and a breakout strategy. It is a research tool: a positive backtest is not a promise of future profit.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `error: externally-managed-environment` on `pip install` | You are installing into the distro Python (PEP 668) | Use the project venv: `./run.sh` or `.venv/bin/python -m pip install -r requirements.txt`. Never `pip install` as root. |
| `ModuleNotFoundError: aiohttp` / `pydantic` / `dotenv` | The venv is not active, or dependencies are missing | `source .venv/bin/activate` (Windows: `.\.venv\Scripts\Activate.ps1`), then `pip install -r requirements.txt`. Re-run `./run.sh` if unsure. |
| `./run.sh: Permission denied` | The script is not executable | `chmod +x run.sh scripts/*.sh` |
| `Activate.ps1 cannot be loaded` (Windows) | PowerShell execution policy | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or call `.\.venv\Scripts\python.exe` directly. |
| Doctor prints `FAIL` for `live_trading_confirmed` / `SOLANA_PRIVATE_KEY` | `LIVE_TRADING_ENABLED=true` without a complete live configuration | Set `LIVE_TRADING_ENABLED=false` for paper mode, or finish the live checklist in [RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) and re-run `python -m bettercalltrader.live_readiness`. |
| `python -m bettercalltrader.live_readiness` prints `BLOCKED` | A configuration key is missing or a guard failed | The report lists each failing key. Fix it, then re-run. A `blocked` guard is a bug, not a warning — do not arm live trading. |
| Doctor warns the dashboard token is empty on a non-loopback bind | The bot refuses to expose an unauthenticated trading API | Bind `127.0.0.1` (default), or set `DASHBOARD_TOKEN` (and open `/?token=...` once). `ALLOW_INSECURE_DASHBOARD=true` is the explicit, unsafe opt-out. |
| Dashboard returns `401` / `403` | `DASHBOARD_TOKEN` is set and the browser has no token cookie | Open `http://127.0.0.1:8080/?token=<value>` once; the cookie is then reused. |
| A write returns `401` with `totp_required` / `invalid_totp` | `DASHBOARD_TOTP_SECRET` is set and the request had no current code | Supply the 6-digit code in `X-TOTP`; the dashboard prompts on failure. Reads (`GET`) never need it. |
| Live order returns `DRY_RUN_STAGED` | `LIVE_TRADING_STAGE=dry_run` | Move to `probe` (with `LIVE_STAGE_MAX_TRADES`) and then `full` once the audit trail looks right. |
| Live order returns `BLOCKED` with "probe stage budget exhausted" | The probe allowance for today (UTC) was spent | Raise `LIVE_STAGE_MAX_TRADES` or switch to `LIVE_TRADING_STAGE=full` — deliberately an environment-only decision. The count is persisted in `bot_state`, so restarting does not refill it; a new UTC day does. |
| Port `8080` already in use | Another process (often a previous bot) holds the port | Stop it, or set `COMMAND_CENTER_PORT=8081` in `.env` and restart. The bot falls back to terminal-only mode when the port is busy. |
| The bot appears to stop on its own | The process was killed, or it received `Ctrl+C` / `SIGTERM` | Check `bot_history.log` and the `shutdown` entry in `bot_audit.jsonl`. The bot has no cycle cap and runs until stopped; open paper positions persist in `paper_portfolio.db`. |
| No automatic buys, only `SKIP`/`WATCH` | No LLM configured; the strict heuristic stays below the auto-BUY threshold | Set `GEMINI_API_KEY`, or `LLM_BASE_URL` + `LLM_MODEL`/`LLM_MODELS` for a local server. This is fail-closed by design. |
| Manual buy returns `400` and journals `BLOCK`/`STALE_PRICE` | DexScreener has no fresh quote for that mint | Wait for a DexScreener pair (or enable `PUMPFUN_CURVE_ENABLED=true` for bonding-curve tokens) and retry. The bot never guesses a price. |
| Journal/audit shows `NO_ROUTE` | No Jupiter route or no Pump.fun route for the amount | The token is too new, too illiquid or the amount is too small. Try later or a different mint. |
| `live_buy_blocked` in `bot_audit.jsonl` | A live guard refused the order (unconfirmed, over `MAX_LIVE_TRADE_SOL`, missing key) | This is the guard working. Check the `reason` field and fix the configuration; do not bypass the guard. |
| `solders` missing / signing fails | Live signing needs the optional package | `pip install solders` inside the venv. Paper mode does not need it. |
| Edited `.env` but nothing changed | `.env` is read once at startup | Restart the bot. `python -m bettercalltrader.doctor` shows the values it will use. |
| Dashboard shows stale or empty data | The bot process stopped, or the browser cached an old page | Check the terminal/`bot_history.log`, reload with `Ctrl+F5`, and confirm `GET /ready` returns `200`. |

If a run behaves oddly, capture `python -m bettercalltrader.doctor`, the last
terminal lines, and the tail of `bot_audit.jsonl` before restarting — those three
answer most questions.

## Known limitations

- Without an LLM provider (no Gemini key and no `LLM_BASE_URL`), the strict heuristic tops out below the auto-BUY threshold, so heuristic mode effectively never auto-buys. This is by design: fail-closed.
- A local model needs enough context for the JSON prompt and a server that supports `POST /v1/chat/completions`; small models can return malformed JSON, which falls back to heuristics (or the next configured provider).
- An ML model is refused when its schema, feature set, label source or `COST_MODEL_VERSION` no longer matches; retrain it (`POST /api/ml/train`) instead of trusting a stale probability.
- Pending confirmations are restored from SQLite after a restart, but only within `ENTRY_CONFIRM_RECOVERY_MAX_SEC` (default 900s). Older entries are dropped as expired.
- Pump.fun curve quotes come from a public HTTP endpoint and can lag a fast-moving curve; a quote older than `STALE_PRICE_SEC` still fails closed.
- Wallet quality starts neutral and only becomes meaningful after a few closed trades per wallet.
- Some older log and hard-filter messages are still in Croatian. The newer risk-engine messages are in English; the dashboard chrome itself is bilingual (HR/EN switch).
- The web API is intended for local use and now binds `127.0.0.1` by default. A non-loopback bind requires `DASHBOARD_TOKEN` or startup is refused; `ALLOW_INSECURE_DASHBOARD=true` is an explicit opt-out. Keep the rate limit enabled and do not expose it to the internet.
- The ML model is advisory only and never opens, blocks or resizes trades. The realized replay and the OHLCV backtester are research tools, not forecasts.
- The OHLCV backtester uses close-based signals and gap-aware high/low exit checks; it cannot model order-book depth, MEV or partial fills. When a candle contains both a stop and take profit, the stop is used and the trade is marked `ambiguousBar`.
- The OHLCV engine sells `tp1_fraction` at take profit and trails the rest on the stop ladder, matching the paper book; the same `ExitPolicy` drives both.
- Walk-forward segments reset the bankroll. `--walk-forward` compares one fixed parameter set out of sample; `--optimize` re-selects parameters per window, so it measures the selection process, not hindsight. Neither guarantees future profit.
- Cost stress assumes costs scale linearly with the multiplier and interpolates break-even between sweep points; it does not re-derive liquidity, MEV or congestion from the market. Treat the break-even multiplier as a sensitivity estimate, not a precise threshold.
- Strategy comparison ranks only the strategies you pass it on one candle series. It cannot tell you a strategy is good in absolute terms, only which candidate held up best under the same windows and costs; the ranking is deterministic but not statistically significant.
- The live-execution readiness audit proves the fail-closed guards and configuration are in place; it cannot prove a venue will fill, that a transaction will land, or that a live run is safe. `READY` means "armed with working guards", not "recommended".
- Strategy profiles only fill environment variables that are not set explicitly; change `.env` values to override them.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This project is for research and paper trading. It is not financial advice. Memecoin trading is extremely risky and most tokens lose value. Nothing here guarantees profit; use at your own risk.