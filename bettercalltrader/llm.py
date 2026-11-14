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
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Type

from bettercalltrader.defaults import LLM_DEFAULTS

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
PROVIDERS = ("auto", "gemini", "openai", "ollama", "none")
OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
_GEMINI_FALLBACKS = ("gemini-3.8-flash", "gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.0-flash")


class LLMError(RuntimeError):
    """Raised when a provider cannot produce a usable structured answer."""


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


def extract_json_object(text: str) -> Dict[str, Any]:
    """Pull the first JSON object out of a model reply.

    Local models routinely wrap the object in prose or a ```json fence, so the
    parser is deliberately forgiving. It never invents content: if no object can
    be decoded, :class:`LLMError` is raised.
    """
    if not text or not str(text).strip():
        raise LLMError("empty model response")
    raw = str(text).strip()
    fence = _JSON_FENCE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise LLMError(f"no JSON object in model response: {raw[:120]!r}")
        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"invalid JSON in model response: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMError("model response is not a JSON object")
    return parsed


class LLMClient:
    """Thin async client that returns a validated pydantic model."""

    def __init__(self, config: Optional[LLMConfig] = None) -> None:
        self.config = config or LLMConfig.from_env()
        self._providers = self.config.providers()
        self._gemini_client: Any = None
        self.last_provider: Optional[str] = None
        self.last_error: Optional[str] = None

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "LLMClient":
        return cls(LLMConfig.from_env(env))

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
        }

    async def complete_json(
        self,
        prompt: str,
        schema: Optional[Type[Any]] = None,
        *,
        session: Any = None,
    ) -> Any:
        """Ask each configured provider in turn for one structured answer."""
        if not self._providers:
            raise LLMError("no LLM provider configured")
        errors = []
        for spec in self._providers:
            try:
                payload_text = await self._call(spec, prompt, schema, session)
                parsed = extract_json_object(payload_text)
                self.last_provider = spec.label
                self.last_error = None
                if schema is not None and hasattr(schema, "model_validate"):
                    return schema.model_validate(parsed)
                return parsed
            except Exception as exc:  # noqa: BLE001 - try the next provider
                errors.append(f"{spec.label}: {exc}")
                self.last_error = str(exc)
        raise LLMError("; ".join(errors) or "all providers failed")

    async def complete_text(self, prompt: str, *, session: Any = None) -> str:
        """Send a plain prompt to the primary provider and return raw text.

        Used by the Command Center ``Load & test`` action: it only needs to
        prove that the model can answer, not that it returns valid JSON.
        """
        if not self._providers:
            raise LLMError("no LLM provider configured")
        spec = self._providers[0]
        text = await self._call(spec, prompt, None, session)
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
