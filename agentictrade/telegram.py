import asyncio
import os
import time
from typing import Any, Dict, List, Optional

import aiohttp


MAIN_BUTTONS = [
    [
        {"text": "📊 Status", "callback_data": "status"},
        {"text": "📂 Positions", "callback_data": "positions"},
    ],
    [
        {"text": "📜 Journal", "callback_data": "journal"},
        {"text": "🛑 Sell All", "callback_data": "sell_all"},
    ],
    [
        {"text": "🔄 Reset Breaker", "callback_data": "reset_breaker"},
        {"text": "❓ Help", "callback_data": "help"},
    ],
    [
        {"text": "🧠 Analyze mint", "callback_data": "prompt_analyze"},
        {"text": "🟢 Buy mint", "callback_data": "prompt_buy"},
    ],
]


class TelegramNotifier:
    def __init__(self):
        self.token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        self.chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        self.queue: List[str] = []
        self.last_update_id: int = 0
        self.input_mode: Optional[str] = None
        self.input_expires: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def enqueue(self, text: str):
        if self.enabled and text:
            self.queue.append(text)
            if len(self.queue) > 30:
                self.queue.pop(0)

    async def send_now(self, session: aiohttp.ClientSession, text: str, buttons: Optional[List[List[Dict[str, str]]]] = None):
        if not self.enabled or not text:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        try:
            async with session.post(url, json=payload, timeout=6) as resp:
                await resp.read()
        except Exception:
            pass

    async def _answer_callback(self, session: aiohttp.ClientSession, callback_id: str, text: Optional[str] = None):
        url = f"https://api.telegram.org/bot{self.token}/answerCallbackQuery"
        payload: Dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        try:
            async with session.post(url, json=payload, timeout=5) as resp:
                await resp.read()
        except Exception:
            pass

    def _clear_input_mode(self):
        self.input_mode = None
        self.input_expires = 0.0

    def _set_input_mode(self, mode: str, ttl: int = 120):
        self.input_mode = mode
        self.input_expires = time.time() + ttl

    async def _handle_text_command(
        self,
        session: aiohttp.ClientSession,
        portfolio,
        researcher,
        executor,
        cmd: str,
    ):
        if cmd in ("/status", "status"):
            w = portfolio.get_wallet_status()
            cb = portfolio.get_circuit_breaker_state(persist=False)
            eq = portfolio.get_equity_now()
            reply = (
                f"📊 <b>AgenticTrade v9.0 Status</b>\n"
                f"💰 Balans: <b>{w['balance_sol']:.3f} SOL</b> | Equity: <b>{eq:.3f} SOL</b>\n"
                f"📈 Realizirani PnL: <b>{w['realized_pnl_sol']:+.3f} SOL</b>\n"
                f"📂 Otvorene pozicije: {w['open_positions']}/{portfolio.max_open_positions}\n"
                f"🛡️ Breaker: {'🚨 TRIPPED' if cb['tripped'] else '✅ OK'}"
            )
            await self.send_now(session, reply, MAIN_BUTTONS)
        elif cmd in ("/positions", "positions"):
            positions = [p for p in portfolio.get_all_positions() if p["status"] == "OPEN"]
            if not positions:
                await self.send_now(session, "📂 Nema otvorenih pozicija.", MAIN_BUTTONS)
            else:
                lines = ["📂 <b>Otvorene pozicije:</b>"]
                for p in positions:
                    entry = p.get("entryPriceUsd") or 0.0
                    cur = p.get("currentPriceUsd") or entry
                    pnl = ((cur / entry) - 1.0) * 100.0 if entry else 0.0
                    lines.append(f"• ${p.get('symbol')} | {pnl:+.1f}% | ulog {p.get('investedSol', 0):.3f} SOL")
                await self.send_now(session, "\n".join(lines), MAIN_BUTTONS)
        elif cmd in ("/journal", "journal"):
            rows = portfolio.get_journal(limit=5)
            if not rows:
                await self.send_now(session, "📜 Journal je prazan.", MAIN_BUTTONS)
            else:
                lines = ["📜 <b>Zadnjih 5 journal zapisa:</b>"]
                for r in rows:
                    lines.append(f"• {r.get('action')} ${r.get('symbol') or '?'} — {r.get('rule')}")
                await self.send_now(session, "\n".join(lines), MAIN_BUTTONS)
        elif cmd in ("/sell_all", "sell_all"):
            open_pos = [p for p in portfolio.get_all_positions() if p["status"] == "OPEN"]
            for p in open_pos:
                portfolio.close_position_manually(p["mint"], exit_reason="TELEGRAM_EXIT")
            await self.send_now(session, f"📱 Zatvoreno {len(open_pos)} otvorenih pozicija putem Telegrama.", MAIN_BUTTONS)
        elif cmd in ("/reset_breaker", "reset_breaker"):
            portfolio.reset_circuit_breaker()
            await self.send_now(session, "🔄 Circuit breaker ručno resetiran putem Telegrama.", MAIN_BUTTONS)
        elif cmd.startswith("/analyze ") or cmd.startswith("analyze "):
            mint = cmd.split(maxsplit=1)[1].strip()
            await self._analyze_mint(session, portfolio, researcher, mint)
        elif cmd.startswith("/buy ") or cmd.startswith("buy "):
            mint = cmd.split(maxsplit=1)[1].strip()
            await self._buy_mint(session, portfolio, researcher, executor, mint)
        elif cmd in ("/help", "help", "/start"):
            await self.send_now(
                session,
                "🤖 <b>Dostupne komande i tipke:</b>\n"
                "Koristi tipke ispod ili pošalji mint adresu nakon što pritisneš tipku za analizu/kupnju.\n"
                "/status /positions /journal /analyze <mint> /buy <mint> /sell_all /reset_breaker",
                MAIN_BUTTONS,
            )
        else:
            await self.send_now(session, "⚠️ Nepoznata komanda. Koristi tipke ispod.", MAIN_BUTTONS)

    async def _analyze_mint(self, session, portfolio, researcher, mint: str):
        if not researcher or not mint:
            await self.send_now(session, "⚠️ Analyze nije dostupan (nema researcher agenta).", MAIN_BUTTONS)
            return
        try:
            market, security = await asyncio.gather(
                researcher.get_market_data(session, mint),
                researcher.get_security_data(session, mint),
            )
            if not market:
                await self.send_now(session, f"⚠️ Token {mint[:8]}... nije pronađen.", MAIN_BUTTONS)
                return
            verdict = await researcher.ai_evaluate_token(market, security)
            await self.send_now(
                session,
                f"🧠 <b>${market.get('symbol')}</b>: {verdict.decision}\n"
                f"Risk: {verdict.risk_score}/100 | Hype: {verdict.hype_score}/100\n"
                f"{verdict.summary_hr[:300]}",
                MAIN_BUTTONS,
            )
        except Exception as e:
            await self.send_now(session, f"⚠️ Analyze greška: {e}", MAIN_BUTTONS)

    async def _buy_mint(self, session, portfolio, researcher, executor, mint: str):
        if not researcher or not mint:
            await self.send_now(session, "⚠️ Buy nije dostupan (nema researcher agenta).", MAIN_BUTTONS)
            return
        try:
            market = await researcher.get_market_data(session, mint)
            if not market:
                await self.send_now(session, f"⚠️ Nema quote za {mint[:8]}...", MAIN_BUTTONS)
                return
            result = portfolio.request_entry(market, None, None, conviction="manual", source="TELEGRAM", skip_confirm=True)
            if result["status"] == "OPENED" and executor is not None:
                try:
                    from research_bot import _dry_run_after_open
                    await _dry_run_after_open(session, executor, portfolio, mint, market)
                except Exception:
                    pass
            await self.send_now(session, f"📱 Manual buy rezultat: {result.get('status')} ({result.get('rule')})", MAIN_BUTTONS)
        except Exception as e:
            await self.send_now(session, f"⚠️ Buy greška: {e}", MAIN_BUTTONS)

    async def flush_and_poll(
        self,
        session: aiohttp.ClientSession,
        portfolio,
        researcher=None,
        executor=None,
    ):
        if not self.enabled:
            return
        while self.queue:
            msg = self.queue.pop(0)
            await self.send_now(session, msg, MAIN_BUTTONS)

        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {"offset": self.last_update_id + 1, "timeout": 1}
        try:
            async with session.get(url, params=params, timeout=5) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                for upd in data.get("result", []):
                    self.last_update_id = max(self.last_update_id, int(upd.get("update_id", 0)))

                    # Callback query from inline buttons
                    cb = upd.get("callback_query")
                    if cb:
                        cb_id = cb.get("id")
                        cb_data = (cb.get("data") or "").strip()
                        if str(cb.get("from", {}).get("id", "")) != self.chat_id and str(cb.get("message", {}).get("chat", {}).get("id", "")) != self.chat_id:
                            await self._answer_callback(session, cb_id, "Unauthorized")
                            continue
                        await self._answer_callback(session, cb_id)

                        if cb_data == "status":
                            await self._handle_text_command(session, portfolio, researcher, executor, "/status")
                        elif cb_data == "positions":
                            await self._handle_text_command(session, portfolio, researcher, executor, "/positions")
                        elif cb_data == "journal":
                            await self._handle_text_command(session, portfolio, researcher, executor, "/journal")
                        elif cb_data == "sell_all":
                            await self._handle_text_command(session, portfolio, researcher, executor, "/sell_all")
                        elif cb_data == "reset_breaker":
                            await self._handle_text_command(session, portfolio, researcher, executor, "/reset_breaker")
                        elif cb_data == "help":
                            await self._handle_text_command(session, portfolio, researcher, executor, "/help")
                        elif cb_data == "prompt_analyze":
                            self._set_input_mode("analyze")
                            await self.send_now(session, "🧠 Pošalji mint adresu tokena za analizu.", MAIN_BUTTONS)
                        elif cb_data == "prompt_buy":
                            self._set_input_mode("buy")
                            await self.send_now(session, "🟢 Pošalji mint adresu tokena za paper buy.", MAIN_BUTTONS)
                        else:
                            await self.send_now(session, "⚠️ Nepoznata tipka.", MAIN_BUTTONS)
                        continue

                    msg = upd.get("message") or {}
                    if str(msg.get("chat", {}).get("id", "")) != self.chat_id:
                        continue
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue

                    # Handle pending button input first
                    if self.input_mode and time.time() <= self.input_expires and not text.startswith("/"):
                        mode = self.input_mode
                        self._clear_input_mode()
                        if mode == "analyze":
                            await self._analyze_mint(session, portfolio, researcher, text)
                        elif mode == "buy":
                            await self._buy_mint(session, portfolio, researcher, executor, text)
                        continue
                    self._clear_input_mode()

                    cmd = text.lower()
                    await self._handle_text_command(session, portfolio, researcher, executor, cmd)
        except Exception:
            pass