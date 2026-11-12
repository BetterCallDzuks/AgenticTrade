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

1. Go to <https://github.com/BetterCallDzuks/AgenticTrade/security/advisories/new>.
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
- The dashboard binds `0.0.0.0:8080` for historical reasons. Set
  `COMMAND_CENTER_HOST=127.0.0.1` for loopback-only access, and always set
  `DASHBOARD_TOKEN` when the port is reachable from other machines.
- `GET /metrics` and `GET /ready` are protected by the same token as `/api/*`.
- `python -m agentictrade.doctor` reports unsafe configuration (missing token,
  live mode without confirmation, broken limits) before the bot starts.

## Scope

In scope: the Python bot, the Command Center API, the packaged modules and the
CI workflows. Out of scope: vulnerabilities in third-party RPC providers,
DexScreener, Pump.fun, Telegram or Google APIs, and losses caused by trading
decisions or market conditions.
