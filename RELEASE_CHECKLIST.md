# Release Checklist (Live Trading Remains Off by Default)

Use this checklist before any change that could enable real on-chain trading.

## Code and tests
- [ ] `python test_v9.py` passes (`ALL TESTS PASSED`)
- [ ] `python smoke_v10.py` prints `SMOKE OK`
- [ ] `python -m py_compile research_bot.py test_v9.py` passes
- [ ] No secrets are committed (`.env` ignored)
- [ ] `LIVE_TRADING_ENABLED=false` in `.env`

## v10 specifics
- [ ] `python replay_v10.py` produces a sensible report (or an empty book report)
- [ ] `DASHBOARD_TOKEN` set if the dashboard is reachable beyond localhost
- [ ] `API_RATE_LIMIT_PER_MIN` reviewed for your usage
- [ ] `STRATEGY_PROFILE` matches your intent; explicit env values still override it
- [ ] `AUDIT_LOG_PATH` points somewhere persistent and is included in backups
- [ ] `/api/audit` shows dry-run events but no unexpected live sends
- [ ] ML model treated as advisory only (`python -m agentictrade.ml status`)

## v11 specifics
- [ ] `python -m agentictrade.doctor` prints `Result: OK` (or every warning is understood)
- [ ] `GET /ready` returns `200` with `"database": true`
- [ ] `GET /metrics` returns Prometheus text (`agentictrade_info`, HTTP counters)
- [ ] CORS allowlist reviewed (`ALLOWED_ORIGINS`); `CORS_ALLOW_ALL` stays `false` when exposed
- [ ] `SIGINT`/`SIGTERM` stops the bot cleanly and writes a `shutdown` audit event
- [ ] `pip-audit -r requirements.txt` is clean (or findings are triaged)
- [ ] `python -m agentictrade.ohlcv --csv <sample.csv>` produces a sane report (sanity-check fees and slippage)
- [ ] Ruff and mypy pass (`ruff check .`, `mypy`)

## v12 specifics
- [ ] `python -m agentictrade.live_readiness` prints `PAPER` (exit 0) with 0 guard failures and 0 network calls
- [ ] `python -m agentictrade.live_readiness --probe` is only run against the intended cluster and reaches RPC + Jupiter
- [ ] `python -m agentictrade.ohlcv --csv <sample.csv> --stress-costs` reports a break-even multiplier
- [ ] `python -m agentictrade.ohlcv --csv <sample.csv> --compare` ranks strategies deterministically on out-of-sample metrics
- [ ] Rolling/anchored windows were re-run after changing strategy parameters (`--window-mode`, `--train-candles`, `--test-candles`)
- [ ] `LIVE_TRADING_CONFIRMED` is unset or `false` until the live section below is complete

## Paper trading validation
- [ ] Run bot in paper mode for at least 24 hours
- [ ] Review `/api/journal` for unexpected BUY/SELL decisions
- [ ] Review equity curve and max drawdown
- [ ] Verify circuit breaker behavior with a simulated losing streak
- [ ] Verify manual buy/sell and confirmation cancellation

## Risk controls
- [ ] Position size limits reviewed (`MIN_POSITION_SOL`, `MAX_POSITION_SOL`)
- [ ] Daily limits reviewed (`MAX_DAILY_STOP_LOSSES`, `MAX_DAILY_DRAWDOWN_PCT`, `MAX_DAILY_TRADES`)
- [ ] Stop/TP/trailing values intentionally configured
- [ ] Cluster guard and wallet quality thresholds reviewed

## Live trading (optional, high risk)
- [ ] Use a dedicated wallet with small balance
- [ ] Keep `SLIPPAGE_BPS` and priority fee conservative
- [ ] Test one tiny live trade manually
- [ ] Confirm transaction signature and resulting balance
- [ ] Have an emergency `/sell_all` or kill-switch plan

## Backup
- [ ] Run `backup.ps1`
- [ ] Verify backup contains `.env`, DB and log
- [ ] Test `restore.ps1` in a separate folder before relying on it