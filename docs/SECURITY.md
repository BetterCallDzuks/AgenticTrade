# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 11.x | ✅ |
| 10.x | security fixes only |
| < 10 | ❌ |

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
private vulnerability reporting:

1. Go to <https://github.com/BetterCallDzuks/BetterCallTrader/security/advisories/new>.
2. Describe the issue, reproduction steps and impact.
3. Do not include real private keys, API tokens or wallet seed phrases.

You can expect an initial response within a few days. There is no bug bounty.

## Secrets

This repository never ships credentials. `.env` is gitignored; only
`.env.example` is committed. If a key is ever committed by accident:

- rotate the key immediately (Gemini, Telegram bot token, GitHub token, RPC key),
- if a Solana private key leaked, move the funds to a fresh wallet,
- then rewrite history or at least invalidate the leaked value.

## Default safety posture

- Paper trading is the default. `LIVE_TRADING_ENABLED=false` and the code
  requires both `LIVE_TRADING_ENABLED=true` and `LIVE_TRADING_CONFIRMED=true`
  before it can send a swap.
- The dashboard binds `127.0.0.1:8080` by default. Binding any other
  interface requires `DASHBOARD_TOKEN`; the bot refuses to start and
  `python -m bettercalltrader.doctor` exits non-zero otherwise. The only override
  is `ALLOW_INSECURE_DASHBOARD=true`, which is logged and warned about.
- Mutating API routes (`POST`/`PUT`/`PATCH`/`DELETE`) reject cross-site
  browser requests unless the origin is allowlisted (`ALLOWED_ORIGINS` or
  localhost), even when no token is configured.
- Set `DASHBOARD_TOTP_SECRET` to require a current RFC 6238 code in `X-TOTP` on
  every mutating request. Codes are compared in constant time with a ±1-step
  clock window, reads are unaffected, and an unset secret leaves behaviour
  unchanged.
- Live orders can be staged with `LIVE_TRADING_STAGE=dry_run|probe|full`.
  `dry_run` never sends, `probe` spends a one-shot `LIVE_STAGE_MAX_TRADES`
  budget on the attempt, and an unknown value fails closed; the stage cannot be
  changed from the dashboard.
- Rate limiting runs before token auth, and `X-Forwarded-For` is ignored
  unless `TRUST_PROXY_HEADERS=true`, so clients cannot spoof their address to
  bypass the limiter.
- `GET /metrics` and `GET /ready` are protected by the same token as `/api/*`.
- `python -m bettercalltrader.doctor` reports unsafe configuration (missing token,
  live mode without confirmation, broken limits, unrecognised boolean values)
  before the bot starts.
- Keep `.env` mode `0600`; `doctor` warns on POSIX when it is readable by
  group or others, and `scripts/backup.sh --no-secrets` excludes it from archives.

## Scope

In scope: the Python bot, the Command Center API, the packaged modules and the
CI workflows. Out of scope: vulnerabilities in third-party RPC providers,
DexScreener, Pump.fun, Telegram or Google APIs, and losses caused by trading
decisions or market conditions.
