"""
config.py

Loads and persists settings for the analysis pipeline: which provider, its key,
its model, and (where the provider has one) an effort level. Prompts on first
run, writes config.json beside this file, reuses it after.

Keys are stored per provider so switching back and forth doesn't mean
re-pasting. config.json is gitignored and nothing here ever prints a key in
full - a shoulder-surfed console is a leaked credential.

Free-tier terms move faster than this file does. Anything labelled free was free
when last checked (July 2026); anything labelled paid or trial will bill you or
run out. `python analyze.py --models` asks the provider what it actually serves,
which is the only trustworthy source for model IDs.

Deps: none.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_EFFORT = "high"

# Ordering is deliberate: genuinely free first, then trial, then paid, so the
# menu reads top-to-bottom as cheapest-first.
PROVIDERS = {
    # ---- free, no card ----
    "nvidia": {
        "label": "NVIDIA NIM - FREE, 40 req/min, no daily cap (phone verify, no card)",
        "key_url": "https://build.nvidia.com/",
        "key_hint": "nvapi-",
        "env": "NVIDIA_API_KEY",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "default_model": "nvidia/nemotron-3-ultra-550b-a55b",
        "has_effort": False,
        "rpm": 40,
        # Fallback only - --config fetches the live catalogue. Verified present
        # on 2026-07-30; NVIDIA rotates its hosted line-up.
        "models": [
            ("nvidia/nemotron-3-ultra-550b-a55b", "Nemotron 3 Ultra 550B - largest (default)"),
            ("deepseek-ai/deepseek-v4-pro", "DeepSeek V4 Pro"),
            ("z-ai/glm-5.2", "GLM 5.2"),
            ("thinkingmachines/inkling", "Inkling"),
            ("nvidia/nemotron-3-super-120b-a12b", "Nemotron 3 Super 120B - faster"),
        ],
    },
    "cloudflare": {
        "label": "Cloudflare Workers AI - FREE, 10k neurons/day hard cap",
        "key_url": "https://dash.cloudflare.com/profile/api-tokens",
        "key_hint": "",
        "env": "CLOUDFLARE_API_TOKEN",
        # Account-scoped, so the URL is a template filled from a stored value.
        "base_url_template": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        "needs": [("account_id", "Cloudflare account ID", "CLOUDFLARE_ACCOUNT_ID")],
        "default_model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "has_effort": False,
        "rpm": 30,
        "models": [
            ("@cf/meta/llama-3.3-70b-instruct-fp8-fast", "Llama 3.3 70B (default)"),
            ("@cf/meta/llama-4-scout-17b-16e-instruct", "Llama 4 Scout"),
            ("@cf/qwen/qwen2.5-coder-32b-instruct", "Qwen2.5 Coder 32B"),
        ],
    },
    "openrouter": {
        "label": "OpenRouter :free - FREE, 20/min but only 50/day (67 videos = 2 days)",
        "key_url": "https://openrouter.ai/keys",
        "key_hint": "sk-or-",
        "env": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "qwen/qwen3-235b-a22b:free",
        "has_effort": False,
        "rpm": 20,
        "models": [
            ("qwen/qwen3-235b-a22b:free", "Qwen3 235B (free)"),
            ("deepseek/deepseek-chat-v3.1:free", "DeepSeek V3.1 (free)"),
        ],
    },
    # ---- trial credits, will run out ----
    "gemini": {
        "label": "Gemini - trial; keys get restricted without billing enabled",
        "key_url": "https://aistudio.google.com/apikey",
        "key_hint": "AIza",
        "env": "GEMINI_API_KEY",
        "default_model": "gemini-flash-latest",
        "has_effort": False,
        "models": [
            ("gemini-flash-latest", "Flash (latest)"),
            ("gemini-flash-lite-latest", "Flash-Lite (latest)"),
            ("gemini-pro-latest", "Pro (latest)"),
        ],
    },
    "mistral": {
        "label": "Mistral La Plateforme - trial credits",
        "key_url": "https://console.mistral.ai/api-keys",
        "key_hint": "",
        "env": "MISTRAL_API_KEY",
        "base_url": "https://api.mistral.ai/v1",
        "default_model": "mistral-large-latest",
        "has_effort": False,
        "rpm": 30,
        "models": [
            ("mistral-large-latest", "Mistral Large  - largest"),
            ("mistral-medium-latest", "Mistral Medium - faster"),
        ],
    },
    # ---- paid ----
    "deepseek": {
        "label": "DeepSeek - PAID, but cheap",
        "key_url": "https://platform.deepseek.com/api_keys",
        "key_hint": "sk-",
        "env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "has_effort": False,
        "rpm": 30,
        "models": [
            ("deepseek-chat", "deepseek-chat     - general"),
            ("deepseek-reasoner", "deepseek-reasoner - thinking, slower"),
        ],
    },
    "groq": {
        "label": "Groq - PAID (not xAI/Grok; keys start gsk_)",
        "key_url": "https://console.groq.com/keys",
        "key_hint": "gsk_",
        "env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "has_effort": False,
        "rpm": 30,
        "models": [
            ("llama-3.3-70b-versatile", "Llama 3.3 70B"),
            ("openai/gpt-oss-120b", "GPT-OSS 120B"),
            ("moonshotai/kimi-k2-instruct", "Kimi K2"),
        ],
    },
    "xai": {
        # Not Groq. Different company, similar name, keys start xai- not gsk_.
        "label": "xAI / Grok - PAID (keys start xai-)",
        "key_url": "https://console.x.ai/",
        "key_hint": "xai-",
        "env": "XAI_API_KEY",
        "base_url": "https://api.x.ai/v1",
        "default_model": "grok-4",
        "has_effort": False,
        "rpm": 30,
        "models": [
            ("grok-4", "Grok 4      - largest"),
            ("grok-3", "Grok 3"),
            ("grok-3-mini", "Grok 3 mini - cheapest"),
        ],
    },
    "cerebras": {
        "label": "Cerebras - PAID as of Jul 2026, free tier withdrawn",
        "key_url": "https://cloud.cerebras.ai/",
        "key_hint": "csk-",
        "env": "CEREBRAS_API_KEY",
        "base_url": "https://api.cerebras.ai/v1",
        "default_model": "gpt-oss-120b",
        "has_effort": False,
        "rpm": 30,
        "models": [
            ("gpt-oss-120b", "GPT-OSS 120B - production"),
            ("zai-glm-4.7", "GLM 4.x      - preview"),
            ("gemma-4-31b", "Gemma 4 31B  - preview"),
        ],
    },
    "anthropic": {
        "label": "Anthropic - PAID; a Claude subscription does not cover this",
        "key_url": "https://console.anthropic.com/settings/keys",
        "key_hint": "sk-ant-",
        "env": "ANTHROPIC_API_KEY",
        "default_model": "claude-opus-5",
        "has_effort": True,
        "models": [
            ("claude-haiku-4-5", "Haiku 4.5  - ~$0.82 for 67 videos"),
            ("claude-sonnet-5", "Sonnet 5   - ~$1.64 for 67 videos"),
            ("claude-opus-5", "Opus 5     - ~$4.09, best judgment"),
        ],
    },
}

DEFAULT_PROVIDER = "nvidia"

DEFAULTS = {
    "provider": DEFAULT_PROVIDER,
    "keys": {},
    "models": {},
    "extras": {},
    "effort": DEFAULT_EFFORT,
}


def load():
    """Missing or corrupt config just means defaults - it is all re-enterable,
    the same stance load_existing() takes in batch_fetch."""
    cfg = {k: ({} if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                stored = json.load(f)
            cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (json.JSONDecodeError, OSError):
            print(f"  {os.path.basename(CONFIG_PATH)} unreadable - starting from defaults.")
    for k in ("keys", "models", "extras"):
        cfg.setdefault(k, {})
    return cfg


def save(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def choose_provider(current):
    print("Provider:")
    ids = list(PROVIDERS)
    for i, pid in enumerate(ids, 1):
        mark = " *" if pid == current else ""
        print(f"  {i:>2}) {PROVIDERS[pid]['label']}{mark}")
    while True:
        choice = input(f"Provider [{current}]: ").strip()
        if choice == "":
            return current
        if choice.isdigit() and 1 <= int(choice) <= len(ids):
            return ids[int(choice) - 1]
        print(f"  Enter 1-{len(ids)}, or blank to keep the current one.")


def prompt_api_key(provider, current=""):
    """Pasted keys pick up quotes and stray whitespace surprisingly often.
    Blank keeps the existing key, so --config can change the model without
    re-pasting a credential."""
    spec = PROVIDERS[provider]
    print(f"{provider} API key. Get one at {spec['key_url']}")
    while True:
        suffix = " (blank keeps current)" if current else ""
        key = input(f"Paste key{suffix}: ").strip().strip('"').strip("'")
        if not key:
            if current:
                return current
            continue
        if spec["key_hint"] and not key.startswith(spec["key_hint"]):
            # Naming collisions are real (Groq vs xAI/Grok), so when a key
            # obviously belongs to a provider we know about, name that provider
            # rather than just warning. Still soft: key formats change.
            owner = next((p for p, s in PROVIDERS.items()
                          if s["key_hint"] and key.startswith(s["key_hint"])), None)
            if owner and owner != provider:
                print(f"  That looks like a {owner} key, not {provider}. Re-enter, or blank to keep current.")
                continue
            print(f"  Warning: expected it to start with {spec['key_hint']!r}. Using it anyway.")
        return key


def live_models(cfg_for_listing):
    """Ask the provider what it serves. Hardcoded lists in this file were wrong
    for every provider tried, so they are only a fallback for when the listing
    endpoint is unreachable."""
    import providers  # local: providers does not import config, so no cycle

    try:
        names = providers.list_models(cfg_for_listing)
    except Exception:
        return []
    # list_models returns display strings on failure, real IDs on success; an
    # error line always contains a space, model IDs never do.
    ids = [n for n in names if " " not in n]
    # A catalogue endpoint lists everything the provider hosts - embedding,
    # reranking, OCR, safety and reward models included. None of those can hold
    # a chat turn, and 100 entries is not a menu, so drop them by name.
    junk = ("embed", "rerank", "guard", "safety", "reward", "clip", "parse",
            "translate", "ocr", "-vl-", "vision", "detector", "retriev",
            "topic-control", "calibration")
    usable = [m for m in ids if not any(j in m.lower() for j in junk)]
    return usable or ids


def choose_model(provider, current, available):
    spec = PROVIDERS[provider]
    if available:
        options = [(m, m) for m in available]
        source = f"{len(options)} offered by {provider}"
    else:
        options = list(spec["models"])
        source = "fallback list - could not reach the provider"

    print(f"Model ({source}):")
    for i, (mid, label) in enumerate(options, 1):
        mark = " *" if mid == current else ""
        print(f"  {i:>3}) {label}{mark}")
    while True:
        choice = input(f"Number, exact model id, or blank for [{current}]: ").strip()
        if choice == "":
            return current
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1][0]
        if not choice.isdigit():
            return choice  # trust a pasted id; a bad one fails loudly on first call
        print(f"  Enter 1-{len(options)}, an exact model id, or blank.")


def choose_effort(current):
    while True:
        choice = input(f"Effort {'/'.join(EFFORT_LEVELS)} [{current}]: ").strip().lower()
        if choice == "":
            return current
        if choice in EFFORT_LEVELS:
            return choice
        print(f"  Enter one of: {', '.join(EFFORT_LEVELS)}")


def resolve(force_prompt=False):
    """Returns a flat, ready-to-use config for the selected provider, prompting
    only for what is actually missing.

    An exported key for the provider's env var wins over the stored one so a
    throwaway key can override without editing the file - but it is never
    written to disk, because a key that arrived from the environment was not
    offered for storage.
    """
    cfg = load()
    dirty = False

    if force_prompt:
        cfg["provider"] = choose_provider(cfg["provider"])
        dirty = True

    provider = cfg["provider"]
    if provider not in PROVIDERS:
        print(f"  Stored provider {provider!r} no longer exists - using {DEFAULT_PROVIDER!r}.")
        provider = cfg["provider"] = DEFAULT_PROVIDER
        dirty = True
    spec = PROVIDERS[provider]

    env_key = os.environ.get(spec["env"], "").strip()
    key = env_key or cfg["keys"].get(provider, "")
    if not key or force_prompt:
        key = prompt_api_key(provider, key)
        cfg["keys"][provider] = key
        dirty = True
        env_key = ""  # an explicitly entered key outranks the environment
    origin = f"${spec['env']}" if env_key else os.path.basename(CONFIG_PATH)
    print(f"  Key: {key[:8]}... from {origin}")

    # Providers whose endpoint is account-scoped need one extra value.
    extras = cfg["extras"].setdefault(provider, {})
    for name, label, env_var in spec.get("needs", []):
        val = os.environ.get(env_var, "").strip() or extras.get(name, "")
        if not val or force_prompt:
            entered = input(f"{label}"
                            f"{' (blank keeps current)' if val else ''}: ").strip().strip('"').strip("'")
            val = entered or val
            extras[name] = val
            dirty = True
        if not val:
            print(f"  {label} is required for {provider}.")

    base_url = spec.get("base_url")
    if not base_url and spec.get("base_url_template"):
        try:
            base_url = spec["base_url_template"].format(**extras)
        except KeyError:
            base_url = None

    model = cfg["models"].get(provider, spec["default_model"])

    if force_prompt:
        print("  Asking the provider which models it serves...")
        available = live_models({"provider": provider, "api_key": key, "base_url": base_url})
        model = choose_model(provider, model, available)
        cfg["models"][provider] = model
        if spec["has_effort"]:
            cfg["effort"] = choose_effort(cfg["effort"])
        dirty = True
    cfg["models"].setdefault(provider, model)

    if dirty:
        save(cfg)
        print(f"  Saved to {os.path.basename(CONFIG_PATH)}")

    return {
        "provider": provider,
        "api_key": key,
        "model": model,
        # Only OpenAI-compatible providers use these; harmless elsewhere.
        "base_url": base_url,
        "rpm": spec.get("rpm"),
        # Carried for every provider but only meaningful where has_effort is
        # set; it still belongs in the cache key, since changing it changes
        # the output on providers that honour it.
        "effort": cfg["effort"] if spec["has_effort"] else "n/a",
    }
