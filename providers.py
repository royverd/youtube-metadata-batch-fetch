"""
providers.py

One call shape, several backends. make_describer(cfg) returns a function that
takes (prompt_text, user_content) and gives back (dict, usage) - everything
upstream of this file is provider-agnostic, so adding Ollama later is one more
class here and one more entry in config.PROVIDERS.

Both backends validate their response through schema.VideoDescription rather
than trusting the model's JSON, so a provider that honours the schema loosely
fails loudly here instead of writing half-populated rows into the database.

Deps: anthropic; google-genai only if the gemini provider is selected.
"""

import json
import time

import schema

# Free-tier requests per minute, used to self-pace so we never earn a 429.
# Only Gemini needs this - Anthropic's paid tiers are far above what a 65-video
# run asks for, and its SDK retries anyway.
GEMINI_RPM = {
    "gemini-pro-latest": 5,
    "gemini-flash-latest": 10,
    "gemini-flash-lite-latest": 15,
    "gemini-2.5-pro": 5,
    "gemini-2.5-flash": 10,
    "gemini-2.5-flash-lite": 15,
}
GEMINI_DEFAULT_RPM = 5  # unknown model: assume the strictest published tier


class ProviderError(RuntimeError):
    """Anything that killed one video but might not kill the run."""


class FatalProviderError(RuntimeError):
    """Bad key, missing model, exhausted daily quota - stop the run."""


def _validate(raw_text):
    """Model JSON in, plain dict out. Pydantic is the gate: a missing field or
    an out-of-range level raises here rather than silently storing a null."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ProviderError(f"response was not JSON: {e}") from e
    return schema.VideoDescription.model_validate(data).model_dump()


class AnthropicDescriber:
    # Opus 5 thinks by default and max_tokens bounds thinking plus response
    # together. The description is ~1.5k tokens; the rest is room for a long
    # deliberation so the JSON can't be truncated mid-object.
    MAX_TOKENS = 16000

    def __init__(self, cfg):
        import anthropic

        self._anthropic = anthropic
        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=cfg["api_key"], max_retries=5)
        self.schema = schema.json_schema()

    def __call__(self, prompt_text, user_content):
        a = self._anthropic
        try:
            resp = self.client.messages.create(
                model=self.cfg["model"],
                max_tokens=self.MAX_TOKENS,
                output_config={
                    "effort": self.cfg.get("effort", "high"),
                    "format": {"type": "json_schema", "schema": self.schema},
                },
                # Identical across every call and ahead of the transcript, so it
                # caches; only the per-video suffix is billed at full rate.
                system=[{
                    "type": "text",
                    "text": prompt_text,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_content}],
            )
        except a.AuthenticationError as e:
            raise FatalProviderError("API key rejected") from e
        except a.NotFoundError as e:
            raise FatalProviderError(f"model {self.cfg['model']} unavailable on this key") from e
        except a.RateLimitError as e:
            raise FatalProviderError("rate limited past the retry budget") from e
        except (a.APIStatusError, a.APIConnectionError) as e:
            raise ProviderError(f"{type(e).__name__}") from e

        if resp.stop_reason == "refusal":
            raise ProviderError("model declined this transcript")

        # Adaptive thinking puts thinking blocks first; the schema guarantees
        # the text block parses.
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise ProviderError("no text block in response")

        u = resp.usage
        return _validate(text), {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }


GEMINI_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   "{model}:generateContent")

# Gemini's responseSchema is an OpenAPI subset, not full JSON Schema: no $ref,
# no $defs, no additionalProperties, and integer enums are unreliable. So the
# Pydantic schema gets flattened down to what it accepts, and the 1-5 range on
# the axes is enforced by Pydantic on the way back instead of by the API.
_GEMINI_TYPES = {"string": "STRING", "integer": "INTEGER", "number": "NUMBER",
                 "boolean": "BOOLEAN", "array": "ARRAY", "object": "OBJECT"}


def _to_gemini_schema(node, defs):
    """Inline $refs and drop everything Gemini's subset doesn't accept."""
    if "$ref" in node:
        return _to_gemini_schema(defs[node["$ref"].rsplit("/", 1)[-1]], defs)

    out = {}
    jtype = node.get("type")
    if jtype in _GEMINI_TYPES:
        out["type"] = _GEMINI_TYPES[jtype]

    # String enums survive; integer enums are dropped and left to validation.
    if node.get("enum") and jtype == "string":
        out["enum"] = node["enum"]
        out["format"] = "enum"

    if jtype == "object":
        out["properties"] = {
            k: _to_gemini_schema(v, defs) for k, v in node.get("properties", {}).items()
        }
        if node.get("required"):
            out["required"] = list(node["required"])
    elif jtype == "array":
        out["items"] = _to_gemini_schema(node.get("items", {"type": "string"}), defs)
    return out


def gemini_schema():
    full = schema.json_schema()
    return _to_gemini_schema(full, full.get("$defs", {}))


class GeminiDescriber:
    """Free-tier Gemini over plain REST - the same call shape as the documented
    curl, so there is no SDK version to track. Self-paces to the model's
    published RPM: the free tier has no burst headroom, and eating a 429 costs
    more wall-clock than just waiting would have."""

    def __init__(self, cfg):
        import requests

        self._requests = requests
        self.cfg = cfg
        self.url = GEMINI_ENDPOINT.format(model=cfg["model"])
        self.schema = gemini_schema()
        rpm = GEMINI_RPM.get(cfg["model"], GEMINI_DEFAULT_RPM)
        self.min_interval = 60.0 / rpm
        self._last_call = 0.0

    def _pace(self):
        wait = self.min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def __call__(self, prompt_text, user_content):
        self._pace()
        body = {
            "systemInstruction": {"parts": [{"text": prompt_text}]},
            "contents": [{"parts": [{"text": user_content}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": self.schema,
            },
        }
        try:
            r = self._requests.post(
                self.url,
                headers={"Content-Type": "application/json",
                         "X-goog-api-key": self.cfg["api_key"]},
                json=body,
                timeout=180,
            )
        except self._requests.RequestException as e:
            raise ProviderError(f"{type(e).__name__}") from e

        if r.status_code != 200:
            detail = r.text[:200].replace("\n", " ")
            if r.status_code in (401, 403):
                raise FatalProviderError(f"key rejected ({r.status_code}): {detail}")
            if r.status_code == 404:
                raise FatalProviderError(f"model {self.cfg['model']} not found: {detail}")
            if r.status_code == 429:
                raise FatalProviderError("quota exhausted - resets daily, progress is saved")
            if r.status_code == 400:
                # Usually a malformed schema, which is our bug, not a bad video.
                raise FatalProviderError(f"request rejected (400): {detail}")
            raise ProviderError(f"HTTP {r.status_code}: {detail}")

        data = r.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            reason = data.get("promptFeedback", {}).get("blockReason", "no content")
            raise ProviderError(f"empty response ({reason})") from e

        um = data.get("usageMetadata", {})
        return _validate(text), {
            "input_tokens": um.get("promptTokenCount", 0),
            "output_tokens": um.get("candidatesTokenCount", 0),
            "cache_read": 0,
            "cache_write": 0,
        }


class OpenAICompatDescriber:
    """Anything speaking the OpenAI chat-completions shape: Cerebras, Groq,
    OpenRouter, a local vLLM. Only base_url and model differ, which is the
    point - when a provider restricts your key you move by editing config,
    not code.

    Structured output goes through json_object mode with the schema pasted
    into the system prompt, rather than the strict json_schema parameter.
    Strict mode support is inconsistent across these providers and silently
    varies by model; the Pydantic gate on the way back catches what looser
    enforcement lets through, so the weaker mode costs nothing but retries.
    """

    def __init__(self, cfg):
        import requests

        self._requests = requests
        self.cfg = cfg
        self.url = cfg["base_url"].rstrip("/") + "/chat/completions"
        self.schema_text = json.dumps(schema.json_schema(), indent=1)
        rpm = cfg.get("rpm") or 0
        self.min_interval = (60.0 / rpm) if rpm else 0.0
        self._last_call = 0.0

    def _pace(self):
        if not self.min_interval:
            return
        wait = self.min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def __call__(self, prompt_text, user_content):
        self._pace()
        system = (
            f"{prompt_text}\n\n"
            "Reply with a single JSON object and nothing else - no prose, no code fence. "
            "It must validate against this JSON Schema:\n"
            f"{self.schema_text}"
        )
        try:
            r = self._requests.post(
                self.url,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.cfg['api_key']}"},
                json={
                    "model": self.cfg["model"],
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user_content}],
                    "response_format": {"type": "json_object"},
                    "max_tokens": 8000,
                },
                timeout=300,
            )
        except self._requests.RequestException as e:
            raise ProviderError(f"{type(e).__name__}") from e

        if r.status_code != 200:
            detail = r.text[:200].replace("\n", " ")
            if r.status_code in (401, 403):
                raise FatalProviderError(f"key rejected ({r.status_code}): {detail}")
            if r.status_code == 404:
                raise FatalProviderError(f"model {self.cfg['model']} not found: {detail}")
            if r.status_code == 429:
                raise FatalProviderError("rate limited or daily quota spent - progress is saved")
            raise ProviderError(f"HTTP {r.status_code}: {detail}")

        data = r.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise ProviderError("no content in response") from e

        # Some models still fence the JSON despite json_object mode.
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1].removeprefix("json").strip()

        u = data.get("usage", {})
        return _validate(text), {
            "input_tokens": u.get("prompt_tokens", 0),
            "output_tokens": u.get("completion_tokens", 0),
            "cache_read": 0,
            "cache_write": 0,
        }


# Only providers with their own wire format need an entry. Anything else is
# assumed OpenAI-compatible and picked up from its base_url, so adding a
# provider means editing config.PROVIDERS and nothing here.
DESCRIBERS = {
    "anthropic": AnthropicDescriber,
    "gemini": GeminiDescriber,
}


def list_models(cfg):
    """What the provider actually serves this key, right now. Returns display
    lines rather than raising - a failure here is informational, not fatal."""
    import requests

    provider = cfg["provider"]
    if provider == "anthropic":
        url, headers = "https://api.anthropic.com/v1/models", {
            "x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01"}
        pick = lambda d: [m["id"] for m in d.get("data", [])]
    elif provider == "gemini":
        url, headers = ("https://generativelanguage.googleapis.com/v1beta/models",
                        {"X-goog-api-key": cfg["api_key"]})
        pick = lambda d: [m["name"].removeprefix("models/") for m in d.get("models", [])
                          if "generateContent" in m.get("supportedGenerationMethods", [])]
    elif cfg.get("base_url"):
        url, headers = (cfg["base_url"].rstrip("/") + "/models",
                        {"Authorization": f"Bearer {cfg['api_key']}"})
        pick = lambda d: [m["id"] for m in d.get("data", [])]
    else:
        return [f"no model listing for provider {provider!r}"]

    try:
        r = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as e:
        return [f"could not reach {provider}: {type(e).__name__}"]
    if r.status_code != 200:
        return [f"{provider} returned HTTP {r.status_code}: {r.text[:120]}"]
    names = sorted(pick(r.json()))
    return names or [f"{provider} listed no usable models"]


def make_describer(cfg):
    cls = DESCRIBERS.get(cfg["provider"])
    if cls is None:
        if not cfg.get("base_url"):
            raise FatalProviderError(
                f"provider {cfg['provider']!r} has no adapter and no base_url")
        cls = OpenAICompatDescriber
    return cls(cfg)
