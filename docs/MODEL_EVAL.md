# Local model evaluation

- Generated: 2026-11-14 19:22:44
- Cases per model: 4 (from `paper_portfolio.db`, real recorded payloads)
- Prompt: `bettercalltrader.prompts.build_research_prompt` (the live research prompt)
- JSON retry: disabled, so the table measures raw reliability
- Temperature: 0.0

| Model | OK | JSON invalid | Schema invalid | Repaired | p50 ms | p95 ms | Agreement | BUY wins | Score |
|---|---|---|---|---|---|---|---|---|---|
| `local:qwen3.5-0.8b-uncensored` | 1/4 | 0 | 0 | 0 | 4,186 | 4,186 | 0.0% | 0% of 1 | **29.7** |
| `local:qwen2.5-coder-7b-instruct` | 4/4 | 0 | 0 | 0 | 29,805 | 38,805 | 100.0% | — | **90.0** |

Score = 45% JSON validity + 45% schema validity + 10% speed (p50 latency, full marks under 1.5s, zero at 15s).

Decision mix:

| Model | BUY | WATCH | SKIP | avg risk | avg hype |
|---|---|---|---|---|---|
| `local:qwen3.5-0.8b-uncensored` | 1 | 0 | 0 | 25.0 | 40.0 |
| `local:qwen2.5-coder-7b-instruct` | 0 | 0 | 4 | 85.0 | 15.0 |

## Failure samples

**`local:qwen3.5-0.8b-uncensored`**
- `EHVpQJ76sEKq…`: local:qwen3.5-0.8b-uncensored: invalid JSON in model response: Extra data: line 1 column 4 (char 3)
- `Cf9C7D54ikZV…`: local:qwen3.5-0.8b-uncensored: invalid JSON in model response: Extra data: line 1 column 4 (char 3)
- `GbeRhdFYmEcK…`: local:qwen3.5-0.8b-uncensored: invalid JSON in model response: Extra data: line 1 column 4 (char 3)


_Agreement compares the model with the decision the bot recorded for that scan; it is not ground truth. BUY wins only covers tokens that were also traded._

## Baseline context

- Host: LM Studio on the same machine as the bot (`LLM_BASE_URL=http://127.0.0.1:1234/v1`).
  Local models are cold-loaded per candidate, so the first request of each model
  includes load time (`p95` > `p50`).
- Cases: the four most recent recorded scans that already carry a verdict, read
  from `paper_portfolio.db`; no synthetic data.
- **Gemini was unavailable when this snapshot was taken.** `gemini-3.8-flash`
  returned `503 UNAVAILABLE` ("experiencing high demand") on every case in two
  separate runs, and the `gemini-flash-latest` alias timed out, so the table is
  locals-only. Re-run `python py/eval_models.py --limit 4 --baseline gemini
  --out docs/MODEL_EVAL.md` and overwrite this file once it is reachable.
- Raw JSON validity is measured with repair and retry disabled on purpose. The
  live client repairs malformed JSON first and retries once, so production
  reliability for the 0.8B model is higher than this table suggests; the table
  is the worse case.
