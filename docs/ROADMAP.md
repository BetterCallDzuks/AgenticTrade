# Roadmap

This file is the single place that separates **shipped** from **candidate**
work. It is a plan, not a promise: nothing here predicts profit, and live
trading stays off until the readiness audit passes and the operator arms it
deliberately. Every released change is also recorded in
[CHANGELOG.md](CHANGELOG.md).

## Current release

| Version | Date | Theme |
|---|---|---|
| 13.0.0 | 2026-11-14 | Local-model ops, LLM reliability, staged live arming, TOTP, digest, SFT export |
| 12.0.0 | 2026-11-13 | Walk-forward evidence, cost stress, strategy comparison, readiness audit |
| 11.0.0 | 2026-11-12 | Console windows, security hardening, doctor |
| 10.0.0 | 2026-10-20 | Metrics, replay, audit trail, profiles, advisory ML |

## Shipped in 13.0.0

- `run.sh` manages LM Studio (start on boot, stop on exit, never touches an
  already-running server); dashboard can pick Gemini, `auto` or a local model.
- `LLM_MODELS` ordered fallback chain, per-provider circuit breaker, JSON repair
  plus one corrective retry, shared `bettercalltrader/prompts.py`.
- `py/eval_models.py` replays the live prompt across models with a baseline.
- `py/export_training.py` exports chat-format SFT JSONL (no in-process
  fine-tuning; GGUF cannot be trained in place).
- `LIVE_TRADING_STAGE=dry_run|probe|full` with an attempt-based probe budget and
  a fail-closed default; shown read-only on the System page.
- `DASHBOARD_TOTP_SECRET` for mutating dashboard requests only.
- Telegram `/digest` and `DIGEST_INTERVAL_SEC`.
- Paper/live replay parity test (WAL-backed) and a regime-shift strategy
  comparison test.
- `docs/MODEL_EVAL.md` baseline (two local models on four recorded cases;
  Gemini returned `503` at snapshot time, so that row is pending).

## Candidates (not scheduled)

Ordered by risk-reduction per unit of work, not by expected return.

### Safety and live readiness

1. **Persist the probe budget per day.** Today the probe counter is in memory,
   so a restart resets it. Writing attempts to `bot_state` would make a crash
   loop unable to spend more than `LIVE_STAGE_MAX_TRADES` per day.
2. **Readiness drift alert.** Re-run `bettercalltrader.live_readiness` on a
   schedule and alert when a previously `READY` environment becomes `BLOCKED`
   (key removed, cap raised, stage invalid).
3. **Signed trade intent.** Log a hash of `(mint, amount, quote, stage)` before
   the send and after the fill, so a replayed audit trail can prove what the
   bot intended versus what executed.
4. **Telegram approval for probe.** Optional two-step confirmation for the first
   probe order (button + TOTP), separate from `LIVE_TRADING_CONFIRMED`.

### Research and models

5. **Gemini baseline and eval regression gate.** The committed
   `docs/MODEL_EVAL.md` is locals-only because Gemini returned `503` at snapshot
   time; add the Gemini row when it is reachable, then fail a check when a
   prompt change drops the score below the committed baseline.
6. **Walk-forward across the strategy comparison in CI.** A tiny synthetic
   candle set proving `--compare --window-mode rolling` stays deterministic on
   every platform (currently covered by self-tests, not the matrix).
7. **Calibration report.** Compare the advisory ML probability against realized
   outcomes per bucket, instead of reporting a single accuracy number.

### Operations

8. **Dashboard stage history.** Keep the last N stage transitions
   (`dry_run → probe → full`) in the audit log and render them on the System
   page.
9. **Backup/restore verification.** `scripts/restore.sh` currently restores;
   add a post-restore replay check that refuses a snapshot whose trades do not
   reconcile.
10. **Rate-limit dashboard writes separately.** Reads and writes share one
    limiter; a write burst should not lock out the polling UI.

### Explicit non-goals

- In-process fine-tuning of GGUF models. We export data; training happens in an
  external trainer.
- Turning live trading on from the dashboard. The stage and the confirmation
  flag are environment-only by design.
- Profit claims or "win rate" marketing. Paper and replay results test rules;
  they do not forecast returns.

## How work lands

1. Change code and tests together; a feature without a regression test is not
   done.
2. Run the gates: `ruff check .`, `mypy`, `python py/test_v9.py`,
   `python py/smoke_v10.py`.
3. Update [CHANGELOG.md](CHANGELOG.md), the README and `.env.example` when a
   variable or command changes.
4. Bump the version in `pyproject.toml`, `py/research_bot.py`, the README badge
   and the test/smoke assertions in the same commit.
