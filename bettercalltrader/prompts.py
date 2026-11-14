"""Prompts shared by the live research loop and the offline model evaluation.

Keeping the research prompt in one place means the evaluation harness measures
the *same* request the bot sends in production. If this text drifts from the
live prompt, a model's eval score stops predicting its live behaviour.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

TEXT_TEST_PROMPT = "Reply with the single word OK and nothing else."

RESEARCH_INSTRUCTIONS = """\
PRAVILA EVALUACIJE (v9.0 — budi strog, kapital je prioritet):
1. Analiziraj 'top10_holder_pct' i 'is_bundled'. Ako je top10_holder_pct < 20% i is_bundled == false, to je izvrstan 'green_flag'. Bundle = SKIP.
2. Ako postoji 'whale_signal' (Smart Money je kupio), to je snažan bikovski signal (+20 Hype Score), ali nije dovoljno samo za BUY ako je cijena već u 5m dumpu.
3. Analiziraj zamah (buys vs sells u 5m i 1h) i omjer likvidnosti/FDV-a. Omjer ispod 5% na većem FDV-u je red_flag.
4. Provjeri ima li token X (Twitter), Telegram ili web stranicu. Bez socijala + rana bonding krivulja = najviše WATCH, ne BUY.
5. NEMOJ davati BUY ako: 5m cijena već pada (price_change_m5 negativan i sells >= buys), ili je token već ekstremno ispumpan u 1h (>400%) i počinje korekcija.
6. BUY samo uz visoku konfluenciju: čist ugovor, zdravi holderi, kupovni pritisak, prihvatljiv liq/FDV. Inače WATCH ili SKIP.
7. Budi strog i precizan! Napiši konkretne brojke u 'summary_hr'."""


def build_research_prompt(market: Mapping[str, Any], security: Mapping[str, Any]) -> str:
    """The single research prompt: market + on-chain forensics + rules."""
    return f"""
        Ti si elitni kvantitativni i on-chain AI analitičar za Solana memecoine.
        Procijeni sljedeći token na temelju stvarnih on-chain, Smart Money i holder podataka:

        TRŽIŠNI PODACI (DexScreener / Whale Tracker):
        {json.dumps(market, indent=2)}

        ON-CHAIN FORENZIKA TOP HOLDERA & BUNDLE ANALIZA:
        {json.dumps(security, indent=2)}

        {RESEARCH_INSTRUCTIONS}
        """
