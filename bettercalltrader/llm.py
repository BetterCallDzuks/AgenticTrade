"""Provider-neutral LLM access for the research agent.

The bot used to talk to Gemini directly. That made the research step depend on
one paid, remote API. This module keeps the same call shape but behind a small
provider interface, so a locally hosted model is a first-class option:

- **Gemini** — ``GEMINI_API_KEY`` (the historical path, unchanged behaviour).
- **Any OpenAI-compatible server** — Ollama (``http://127.0.0.1:11434/v1``),
  llama.cpp ``server``, LM Studio, vLLM, text-generation-webui, or a remote
  OpenAI-compatible gateway. Set ``LLM_BASE_URL`` and ``LLM_MODEL``;
  ``LLM_MODELS`` adds a comma-separated fallback list tried in order.

Selection is explicit but forgiving:

1. ``LLM_PROVIDER`` = ``gemini`` | ``openai`` | ``ollama`` | ``none`` | ``auto``
2. ``auto`` (default) picks Gemini when ``GEMINI_API_KEY`` is present, then a
   local server when ``LLM_BASE_URL`` is set, otherwise heuristics.
3. If the primary provider fails, the remaining configured providers are tried
   in order before the caller falls back to its heuristic scorer.

Everything is optional: with no provider configured, :attr:`LLMClient.available`
is false and the bot runs exactly as it does today.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Type

from bettercalltrader.defaults import LLM_DEFAULTS

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
PROVIDERS = ("auto", "gemini", "openai", "ollama", "none")
OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
_GEMINI_FALLBACKS = ("gemini-3.8-flash", "gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.0-flash")

# A provider that fails this many times in a row is skipped for the cooldown
# window instead of being retried on every research call. Cooldown is short so a
# restarted local server is picked up again without a bot restart.
DEFAULT_BREAKER_THRESHOLD = 3
DEFAULT_BREAKER_COOLDOWN_SEC = 60.0


class LLMError(RuntimeError):
    """Raised when a provider cannot produce a usable structured answer."""


class JSONParseError(LLMError):
    """The provider answered, but the reply was not a usable JSON object.

    Separate from :class:`LLMError` so the caller can retry with a corrective
    prompt (cheap, the server is warm) without retrying transport failures.
    """


def _env(env: Optional[Mapping[str, str]], key: str, default: str = "") -> str:
    source = os.environ if env is None else env
    value = source.get(key)
    return str(value).strip() if value is not None else default


def _env_float(env: Optional[Mapping[str, str]], key: str, default: float) -> float:
    try:
        return float(_env(env, key) or default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(env: Optional[Mapping[str, str]], key: str, default: int) -> int:
    try:
        return int(float(_env(env, key) or default))
    except (TypeError, ValueError):
        return int(default)


def _env_bool(env: Optional[Mapping[str, str]], key: str, default: bool) -> bool:
    raw = _env(env, key)
    if not raw:
        return bool(default)
    return raw.lower() in ("1", "true", "yes", "on")


def _split_models(*values: str) -> tuple[str, ...]:
    """Merge comma/space separated model names, keeping order and dropping duplicates."""
    names: List[str] = []
    for value in values:
        for part in re.split(r"[,\s]+", value or ""):
            part = part.strip()
            if part and part not in names:
                names.append(part)
    return tuple(names)


@dataclass(frozen=True)
class LLMConfig:
    """Resolved provider settings."""

    provider: str = "auto"
    model: str = ""
    # Per-backend names, because a Gemini name never exists on a local server.
    local_model: str = ""
    # Optional ordered fallback list for the local server (LLM_MODELS).
    local_models: tuple[str, ...] = ()
    gemini_model: str = DEFAULT_GEMINI_MODEL
    base_url: str = ""
    api_key: str = ""
    gemini_api_key: str = ""
    timeout_sec: float = float(LLM_DEFAULTS["timeout_sec"])
    temperature: float = float(LLM_DEFAULTS["temperature"])
    max_tokens: int = int(LLM_DEFAULTS["max_tokens"])
    fallback_to_heuristic: bool = True

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "LLMConfig":
        provider = (_env(env, "LLM_PROVIDER", str(LLM_DEFAULTS["provider"])) or "auto").lower()
        if provider in ("local", "openai-compatible", "llama", "llamacpp"):
            provider = "openai"
        if provider not in PROVIDERS:
            provider = "auto"
        base_url = _env(env, "LLM_BASE_URL", str(LLM_DEFAULTS["base_url"]))
        gemini_key = _env(env, "GEMINI_API_KEY")
        local_models = _split_models(_env(env, "LLM_MODEL"), _env(env, "LLM_MODELS"))
        local_model = local_models[0] if local_models else ""
        gemini_model = _env(env, "GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        model = local_model or gemini_model
        return cls(
            provider=provider,
            model=model,
            local_model=local_model,
            local_models=local_models,
            gemini_model=gemini_model,
            base_url=base_url,
            api_key=_env(env, "LLM_API_KEY"),
            gemini_api_key=gemini_key,
            timeout_sec=max(1.0, _env_float(env, "LLM_TIMEOUT_SEC", float(LLM_DEFAULTS["timeout_sec"]))),
            temperature=_env_float(env, "LLM_TEMPERATURE", float(LLM_DEFAULTS["temperature"])),
            max_tokens=max(64, _env_int(env, "LLM_MAX_TOKENS", int(LLM_DEFAULTS["max_tokens"]))),
            fallback_to_heuristic=_env_bool(env, "LLM_FALLBACK_TO_HEURISTIC", True),
        )

    def _local_specs(self) -> List["ProviderSpec"]:
        """One spec per local model, primary first (``LLM_MODEL`` then ``LLM_MODELS``)."""
        base_url = (self.base_url or OLLAMA_BASE_URL).rstrip("/")
        names = list(self.local_models) or ([self.local_model] if self.local_model else [])
        if not names:
            names = [self.model or DEFAULT_GEMINI_MODEL]
        return [
            ProviderSpec(kind="openai", model=name, base_url=base_url, api_key=self.api_key)
            for name in names
        ]

    def providers(self) -> List["ProviderSpec"]:
        """Return the providers to try, primary first.

        ``auto`` never returns more than one provider implicitly, but an
        explicit Gemini choice still falls back to a configured local server
        (and vice versa) so a dead API does not disable research entirely.
        Every local model in ``LLM_MODELS`` is appended as its own fallback.
        """
        local_chain = self._local_specs()
        gemini = ProviderSpec(
            kind="gemini", model=self.gemini_model or self.model or DEFAULT_GEMINI_MODEL,
            gemini_api_key=self.gemini_api_key,
        )

        if self.provider == "none":
            return []
        if self.provider == "gemini":
            chain = [gemini]
            if self.base_url:
                chain.extend(local_chain)
            return chain
        if self.provider in ("openai", "ollama"):
            chain = list(local_chain)
            if self.gemini_api_key:
                chain.append(gemini)
            return chain
        # auto
        if self.gemini_api_key:
            chain = [gemini]
            if self.base_url:
                chain.extend(local_chain)
            return chain
        if self.base_url:
            return local_chain
        return []


@dataclass(frozen=True)
class ProviderSpec:
    kind: str  # "gemini" | "openai"
    model: str
    base_url: str = ""
    api_key: str = ""
    gemini_api_key: str = ""

    @property
    def label(self) -> str:
        if self.kind == "gemini":
            return f"gemini:{self.model}"
        return f"local:{self.model}"


@dataclass
class ProviderHealth:
    """Per-provider call counters and circuit-breaker state.

    Kept in memory only: a restart should give every provider a clean slate,
    because the usual cause of an open breaker is a local server that has just
    been restarted.
    """

    provider: str
    calls: int = 0
    successes: int = 0
    failures: int = 0
    skipped: int = 0
    consecutive_failures: int = 0
    parse_failures: int = 0
    total_ms: float = 0.0
    last_error: str = ""
    open_until: float = 0.0

    def is_open(self, now: float) -> bool:
        return self.open_until > now

    def as_dict(self, now: float) -> Dict[str, Any]:
        remaining = max(0.0, self.open_until - now)
        return {
            "provider": self.provider,
            "calls": self.calls,
            "successes": self.successes,
            "failures": self.failures,
            "skipped": self.skipped,
            "consecutiveFailures": self.consecutive_failures,
            "parseFailures": self.parse_failures,
            "averageLatencyMs": round(self.total_ms / self.calls, 1) if self.calls else None,
            "lastError": self.last_error or None,
            "open": self.is_open(now),
            "openForMs": int(remaining * 1000) if remaining else 0,
        }


def system_prompt(schema: Optional[Type[Any]] = None) -> str:
    """Instruction shared by every provider to get one JSON object back."""
    base = (
        "You are a strict quantitative risk analyst. Answer with a single JSON "
        "object and nothing else: no prose, no markdown fences."
    )
    if schema is not None and hasattr(schema, "model_json_schema"):
        try:
            schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
            return f"{base} The object must validate against this JSON Schema: {schema_json}"
        except (TypeError, ValueError):
            pass
    return base


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_JSON_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\"'])//[^\n]*", re.DOTALL)
_JSON_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_JSON_UNQUOTED_KEY = re.compile(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:")
_JSON_LITERALS = (
    (re.compile(r"\bTrue\b"), "true"),
    (re.compile(r"\bFalse\b"), "false"),
    (re.compile(r"\bNone\b"), "null"),
    (re.compile(r"\bNaN\b"), "null"),
    (re.compile(r"\bInfinity\b"), "null"),
)


def _brace_walk(raw: str) -> tuple[List[str], bool]:
    """Return the unclosed ``{``/``[`` stack and whether a string is open."""
    stack: List[str] = []
    quote = ""
    escaped = False
    for char in raw:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if stack:
                stack.pop()
    return stack, bool(quote)


def repair_json_text(text: str) -> str:
    """Best-effort textual repair of a model reply before parsing it.

    This only removes noise the model added around a valid document (comments,
    trailing commas, Python literals, a truncated tail). It never invents keys
    or values: if the result still does not parse, the caller raises.
    """
    raw = str(text or "").strip()
    if not raw:
        return raw
    fence = _JSON_FENCE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    raw = raw.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    raw = _JSON_COMMENT.sub("", raw)
    if '"' not in raw and "'" in raw:
        raw = raw.replace("'", '"')
    for pattern, replacement in _JSON_LITERALS:
        raw = pattern.sub(replacement, raw)
    raw = _JSON_UNQUOTED_KEY.sub(r'\1"\2":', raw)
    raw = _JSON_TRAILING_COMMA.sub(r"\1", raw)
    stack, open_string = _brace_walk(raw)
    if open_string:
        raw += '"'
        stack, _ = _brace_walk(raw)
    if stack:
        # Re-walk without caring about quotes: a truncated tail is common in
        # local models that hit max_tokens mid-object.
        closers = {"{": "}", "[": "]"}
        raw = raw.rstrip().rstrip(",") + "".join(closers[op] for op in reversed(stack))
    return raw


def parse_model_json(text: str) -> tuple[Dict[str, Any], str]:
    """Return ``(object, mode)`` where mode is ``"raw"`` or ``"repaired"``."""
    if not text or not str(text).strip():
        raise JSONParseError("empty model response")
    raw = str(text).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        fence = _JSON_FENCE.search(raw)
        candidate = fence.group(1).strip() if fence else raw
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            # Two objects in one reply (``{...}{...}``) is a common local-model
            # failure: take the first complete one and ignore the tail.
            target = candidate if candidate.startswith("{") else ""
            if not target and "{" in raw:
                target = raw[raw.find("{") :]
            if target:
                try:
                    first, _end = json.JSONDecoder().raw_decode(target)
                except json.JSONDecodeError:
                    first = None
                if isinstance(first, dict):
                    return first, "raw"
            repaired = repair_json_text(raw)
            try:
                parsed = json.loads(repaired)
            except json.JSONDecodeError as exc:
                raise JSONParseError(f"invalid JSON in model response: {exc}") from exc
            if not isinstance(parsed, dict):
                raise JSONParseError("model response is not a JSON object")
            return parsed, "repaired"
    if not isinstance(parsed, dict):
        raise JSONParseError("model response is not a JSON object")
    return parsed, "raw"


def extract_json_object(text: str) -> Dict[str, Any]:
    """Pull the first JSON object out of a model reply.

    Local models routinely wrap the object in prose or a ```json fence, so the
    parser is deliberately forgiving. It never invents content: if no object can
    be decoded, :class:`LLMError` is raised.
    """
    return parse_model_json(text)[0]


JSON_RETRY_SUFFIX = (
    "Your previous answer was rejected: it was not a single valid JSON object. "
    "Answer again with ONLY the JSON object, no prose and no markdown fence."
)


class LLMClient:
    """Thin async client that returns a validated pydantic model.

    Each provider has an independent circuit breaker. Three consecutive failures
    open it for a minute, so a stopped local server costs one failed request
    instead of one per research call, while a restarted server is picked up
    again after the cooldown without restarting the bot.
    """

    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        *,
        breaker_threshold: Optional[int] = None,
        breaker_cooldown_sec: Optional[float] = None,
        json_retry: Optional[bool] = None,
        clock: Any = None,
    ) -> None:
        self.config = config or LLMConfig.from_env()
        self._providers = self.config.providers()
        self._gemini_client: Any = None
        self.last_provider: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_parse_mode: Optional[str] = None
        self.last_latency_ms: float = 0.0
        self.breaker_threshold = max(
            1,
            int(
                breaker_threshold
                if breaker_threshold is not None
                else _env_int(None, "LLM_BREAKER_THRESHOLD", DEFAULT_BREAKER_THRESHOLD)
            ),
        )
        self.breaker_cooldown_sec = max(
            0.0,
            float(
                breaker_cooldown_sec
                if breaker_cooldown_sec is not None
                else _env_float(None, "LLM_BREAKER_COOLDOWN_SEC", DEFAULT_BREAKER_COOLDOWN_SEC)
            ),
        )
        self.json_retry = bool(json_retry) if json_retry is not None else _env_bool(None, "LLM_JSON_RETRY", True)
        self._clock = clock or time.monotonic
        self._health: Dict[str, ProviderHealth] = {}
        self.retries = 0

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "LLMClient":
        return cls(LLMConfig.from_env(env))

    def _health_for(self, spec: ProviderSpec) -> ProviderHealth:
        entry = self._health.get(spec.label)
        if entry is None:
            entry = ProviderHealth(provider=spec.label)
            self._health[spec.label] = entry
        return entry

    def health(self) -> List[Dict[str, Any]]:
        """Snapshot every configured provider for the dashboard and /metrics."""
        now = self._clock()
        return [self._health_for(spec).as_dict(now) for spec in self._providers]

    def _ordered_providers(self) -> List[ProviderSpec]:
        """Configured order, minus providers whose breaker is open.

        When every provider is open the first one is still attempted: that is
        the half-open probe that detects recovery, and without it a single-model
        setup would never retry until the cooldown expired.
        """
        now = self._clock()
        healthy: List[ProviderSpec] = []
        cooling: List[ProviderSpec] = []
        for spec in self._providers:
            entry = self._health_for(spec)
            if entry.is_open(now):
                entry.skipped += 1
                cooling.append(spec)
            else:
                healthy.append(spec)
        if healthy:
            return healthy
        return cooling[:1]

    def _record_success(self, spec: ProviderSpec, elapsed_ms: float) -> None:
        entry = self._health_for(spec)
        entry.calls += 1
        entry.successes += 1
        entry.consecutive_failures = 0
        entry.total_ms += elapsed_ms
        entry.open_until = 0.0

    def _record_failure(self, spec: ProviderSpec, exc: BaseException, elapsed_ms: float) -> None:
        entry = self._health_for(spec)
        entry.calls += 1
        entry.failures += 1
        entry.consecutive_failures += 1
        entry.total_ms += elapsed_ms
        entry.last_error = str(exc)[:300]
        if isinstance(exc, JSONParseError):
            entry.parse_failures += 1
        if entry.consecutive_failures >= self.breaker_threshold:
            entry.open_until = self._clock() + self.breaker_cooldown_sec

    @property
    def available(self) -> bool:
        return bool(self._providers)

    @property
    def model_name(self) -> str:
        return self._providers[0].model if self._providers else ""

    @property
    def label(self) -> str:
        return self._providers[0].label if self._providers else "heuristic"

    @property
    def is_local(self) -> bool:
        return bool(self._providers) and self._providers[0].kind == "openai"

    def describe(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "label": self.label,
            "model": self.model_name,
            "local": self.is_local,
            "chain": [spec.label for spec in self._providers],
            "fallbackToHeuristic": self.config.fallback_to_heuristic,
            "breaker": {
                "threshold": self.breaker_threshold,
                "cooldownSec": self.breaker_cooldown_sec,
                "jsonRetry": self.json_retry,
            },
        }

    async def complete_json(
        self,
        prompt: str,
        schema: Optional[Type[Any]] = None,
        *,
        session: Any = None,
    ) -> Any:
        """Ask each configured provider in turn for one structured answer.

        A malformed reply gets one corrective retry on the same provider before
        the breaker counts a failure; a transport error moves straight on.
        """
        if not self._providers:
            raise LLMError("no LLM provider configured")
        errors: List[str] = []
        for spec in self._ordered_providers():
            attempt_prompt = prompt
            attempts = 2 if self.json_retry else 1
            for attempt in range(attempts):
                started = self._clock()
                try:
                    payload_text = await self._call(spec, attempt_prompt, schema, session)
                    parsed, _mode = parse_model_json(payload_text)
                    if schema is not None and hasattr(schema, "model_validate"):
                        result: Any = schema.model_validate(parsed)
                    else:
                        result = parsed
                except Exception as exc:  # noqa: BLE001 - provider-specific failures
                    elapsed = (self._clock() - started) * 1000.0
                    retryable = isinstance(exc, JSONParseError) or type(exc).__name__ == "ValidationError"
                    if retryable and attempt + 1 < attempts:
                        self.retries += 1
                        attempt_prompt = f"{prompt}\n\n{JSON_RETRY_SUFFIX}"
                        continue
                    self._record_failure(spec, exc, elapsed)
                    errors.append(f"{spec.label}: {exc}")
                    self.last_error = str(exc)
                    break
                elapsed = (self._clock() - started) * 1000.0
                self._record_success(spec, elapsed)
                self.last_provider = spec.label
                self.last_error = None
                self.last_parse_mode = _mode
                self.last_latency_ms = elapsed
                return result
        raise LLMError("; ".join(errors) or "all providers failed")

    async def complete_text(self, prompt: str, *, session: Any = None, use_breaker: bool = False) -> str:
        """Send a plain prompt to the primary provider and return raw text.

        Used by the Command Center ``Load & test`` action: it only needs to
        prove that the model can answer, not that it returns valid JSON. An
        explicit test bypasses the breaker so the user can probe a provider the
        bot has marked unhealthy.
        """
        if not self._providers:
            raise LLMError("no LLM provider configured")
        spec = self._ordered_providers()[0] if use_breaker else self._providers[0]
        started = self._clock()
        try:
            text = await self._call(spec, prompt, None, session)
        except Exception:
            self._record_failure(spec, LLMError("text completion failed"), (self._clock() - started) * 1000.0)
            raise
        self._record_success(spec, (self._clock() - started) * 1000.0)
        self.last_provider = spec.label
        self.last_error = None
        return text

    async def _call(self, spec: ProviderSpec, prompt: str, schema: Optional[Type[Any]], session: Any) -> str:
        if spec.kind == "gemini":
            return await self._call_gemini(spec, prompt, schema)
        return await self._call_openai(spec, prompt, schema, session)

    async def _call_gemini(self, spec: ProviderSpec, prompt: str, schema: Optional[Type[Any]]) -> str:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise LLMError("google-genai is not installed") from exc

        if self._gemini_client is None:
            self._gemini_client = genai.Client(api_key=spec.gemini_api_key)
        client = self._gemini_client
        candidates = [spec.model] + [model for model in _GEMINI_FALLBACKS if model != spec.model]
        last: Optional[Exception] = None
        for model in candidates:
            try:
                config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=self.config.temperature,
                    max_output_tokens=self.config.max_tokens,
                )
                response = await asyncio.to_thread(
                    client.models.generate_content, model=model, contents=prompt, config=config
                )
                return getattr(response, "text", "") or ""
            except Exception as exc:  # noqa: BLE001
                last = exc
                text = str(exc)
                if "NOT_FOUND" in text or "404" in text or "not found" in text.lower():
                    continue
                raise
        raise LLMError(f"gemini failed: {last}")

    async def _call_openai(
        self, spec: ProviderSpec, prompt: str, schema: Optional[Type[Any]], session: Any
    ) -> str:
        payload = {
            "model": spec.model,
            "messages": [
                {"role": "system", "content": system_prompt(schema)},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if spec.api_key:
            headers["Authorization"] = f"Bearer {spec.api_key}"
        url = f"{spec.base_url}/chat/completions"
        timeout = self.config.timeout_sec
        owns_session = session is None
        if owns_session:
            import aiohttp

            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))

        async def send(data: Dict[str, Any]) -> tuple[int, str]:
            async with session.post(url, json=data, headers=headers) as response:
                return response.status, await response.text()

        try:
            status, body = await send(payload)
            # Older local servers reject ``response_format``; retry once without
            # it rather than failing the whole research step.
            if status in (400, 422) and "response_format" in payload:
                status, body = await send({key: value for key, value in payload.items() if key != "response_format"})
            if status >= 400:
                raise LLMError(f"HTTP {status}: {body[:200]}")
        finally:
            if owns_session:
                await session.close()
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMError(f"provider returned non-JSON: {body[:200]}") from exc
        return _openai_content(data)


def _openai_content(data: Mapping[str, Any]) -> str:
    """Extract the assistant text from an OpenAI-compatible response."""
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"provider returned no choices: {str(data)[:200]}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # some servers return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, Mapping))
    if not content:
        content = choices[0].get("text") or ""
    if not content:
        raise LLMError("provider returned an empty message")
    return str(content)


def probe(config: Optional[LLMConfig] = None, timeout: float = 2.0) -> Dict[str, Any]:
    """Check provider reachability without spending tokens (used by doctor)."""
    cfg = config or LLMConfig.from_env()
    providers = cfg.providers()
    if not providers:
        return {"state": "unconfigured", "detail": "no provider configured; heuristics only", "providers": []}
    results = []
    for spec in providers:
        if spec.kind == "gemini":
            results.append(
                {
                    "provider": spec.label,
                    "state": "ok" if spec.gemini_api_key else "missing-key",
                    "detail": "GEMINI_API_KEY is set" if spec.gemini_api_key else "GEMINI_API_KEY is empty",
                }
            )
            continue
        url = f"{spec.base_url}/models"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        if spec.api_key:
            request.add_header("Authorization", f"Bearer {spec.api_key}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(4096).decode("utf-8", "replace")
            models = _model_ids(body)
            state = "ok" if (not models or spec.model in models) else "model-missing"
            if state == "ok":
                detail = f"{url} reachable"
            else:
                advertised = ", ".join(list(models)[:5])
                detail = f"{url} reachable; {spec.model!r} not advertised (have: {advertised})"
            results.append({"provider": spec.label, "state": state, "detail": detail})
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            results.append({"provider": spec.label, "state": "unreachable", "detail": f"{url}: {exc}"})
    state = "ok" if any(item["state"] == "ok" for item in results) else results[0]["state"]
    return {
        "state": state,
        "detail": "; ".join(f"{item['provider']}: {item['state']}" for item in results),
        "providers": results,
    }


def _model_ids(body: str) -> Sequence[str]:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return ()
    items = data.get("data") if isinstance(data, dict) else None
    if isinstance(items, list):
        return [str(item.get("id")) for item in items if isinstance(item, Mapping) and item.get("id")]
    models = data.get("models") if isinstance(data, dict) else None
    if isinstance(models, list):
        return [str(item.get("name") or item.get("model")) for item in models if isinstance(item, Mapping)]
    return ()


def _parse_models(body: str, source: str) -> Optional[List[Dict[str, Any]]]:
    """Normalise an LM Studio REST or OpenAI model list into dicts."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, Mapping):
        return None
    if source == "lmstudio":
        items = data.get("models")
        if not isinstance(items, list):
            return None
        models: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            key = item.get("key") or item.get("id")
            if not key:
                continue
            capabilities = item.get("capabilities")
            vision = bool(capabilities.get("vision")) if isinstance(capabilities, Mapping) else False
            models.append(
                {
                    "id": str(key),
                    "type": str(item.get("type") or "llm"),
                    "loaded": bool(item.get("loaded_instances")),
                    "params": item.get("params_string"),
                    "publisher": item.get("publisher"),
                    "vision": vision,
                }
            )
        return models
    if not isinstance(data.get("data"), list) and not isinstance(data.get("models"), list):
        return None
    return [
        {"id": model_id, "type": "llm", "loaded": None, "params": None, "publisher": None, "vision": False}
        for model_id in _model_ids(body)
        if model_id
    ]


async def fetch_models(
    base_url: str,
    api_key: str = "",
    session: Any = None,
    timeout: float = 2.0,
) -> Dict[str, Any]:
    """List models from a local OpenAI-compatible server.

    Prefers LM Studio's REST endpoint (``/api/v1/models``) because it reports
    the model type and whether an instance is loaded, then falls back to the
    plain OpenAI ``/models`` endpoint. Never raises: an unreachable server is a
    normal state for the dashboard, not an error.
    """
    base = (base_url or OLLAMA_BASE_URL).rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    owns_session = session is None
    if owns_session:
        import aiohttp

        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))
    result: Dict[str, Any] = {"reachable": False, "baseUrl": base, "source": "", "models": [], "detail": ""}
    try:
        for url, source in ((f"{root}/api/v1/models", "lmstudio"), (f"{base}/models", "openai")):
            try:
                async with session.get(url, headers=headers) as response:
                    body = await response.text()
                    if response.status >= 400:
                        result["detail"] = f"{url}: HTTP {response.status}"
                        continue
                parsed = _parse_models(body, source)
                if parsed is not None:
                    result.update(
                        {"reachable": True, "source": source, "models": parsed, "detail": f"{url} reachable"}
                    )
                    return result
            except Exception as exc:  # noqa: BLE001 - try the next endpoint
                result["detail"] = f"{url}: {exc}"
        return result
    finally:
        if owns_session:
            await session.close()
