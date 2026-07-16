"""
Quick-add presets for providers with usable free tiers, so you don't have
to hand-type base URLs. Rate limits change often -- default_rpm_limit /
default_rpd_limit are just a starting point that gets pre-filled into the
model's self-imposed rate limit fields (Model settings) when you add from
a preset; the `notes` field is a snapshot. ALWAYS double check the
provider's own dashboard/docs before relying on these for anything real,
and adjust the RPM/RPD fields in Model settings if your account's actual
tier differs (limits vary by account age, region, and billing status).
"""

PROVIDER_PRESETS = {
    "SiliconFlow": {
        "provider": "openai_compatible",
        "base_url": "https://api.siliconflow.com/v1",
        "example_models": ["deepseek-ai/DeepSeek-R1", "deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-7B-Instruct"],
        "notes": "Free tier: 1,000-10,000 RPM (model dependent), requires identity verification. "
                 "Up to 1M context on some models. Older quantizations get deprecated quickly.",
        "default_rpm_limit": 1000,
        "default_rpd_limit": None,
    },
    "Google AI Studio (Gemini)": {
        "provider": "openai_compatible",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "example_models": ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"],
        "notes": "Free tier tightened in Dec 2025 and is now split by model: Gemini 2.5 Pro is "
                 "5 RPM / 50 requests per day (trial-only, in practice); Gemini 2.5 Flash is "
                 "10 RPM / 250 RPD; Gemini 2.5 Flash-Lite is the most generous at 15 RPM / "
                 "1,000 RPD. All share a 250K-1M TPM budget. Limits apply per Google Cloud "
                 "project, not per API key -- extra keys don't add quota. Default below assumes "
                 "you're using Flash; bump it to 15/1000 if you switch to Flash-Lite, or down to "
                 "5/50 for Pro.",
        "default_rpm_limit": 10,
        "default_rpd_limit": 250,
    },
    "OpenRouter": {
        "provider": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "example_models": ["openai/gpt-oss-120b:free", "cohere/command-r7b-12-2024:free"],
        "notes": "Free (':free') models: 20 RPM, 50 requests/day (up to 1,000/day with a $10 deposit), "
                 "up to 262K context. High demand can cause latency spikes.",
        "default_rpm_limit": 20,
        "default_rpd_limit": 50,
    },
    "Mistral AI": {
        "provider": "openai_compatible",
        "base_url": "https://api.mistral.ai/v1",
        "example_models": ["codestral-2501", "devstral-small-2505"],
        "notes": "Experiment plan: ~1 RPS (~60 RPM), 1B tokens/month, 128K-256K context. "
                 "Requires consent to data training.",
        "default_rpm_limit": 60,
        "default_rpd_limit": None,
    },
    "Cerebras": {
        "provider": "openai_compatible",
        "base_url": "https://api.cerebras.ai/v1",
        "example_models": ["gpt-oss-120b"],
        "notes": "Free tier: 30 RPM, 1M tokens/day, 128K context. Limited open-weight model catalog.",
        "default_rpm_limit": 30,
        "default_rpd_limit": None,
    },
    "Groq": {
        "provider": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "example_models": ["openai/gpt-oss-120b", "qwen/qwen3-32b"],
        "notes": "Free tier: 30 RPM, 1,000 requests/day, 128K context. Strict tokens-per-minute ceilings.",
        "default_rpm_limit": 30,
        "default_rpd_limit": 1000,
    },
    "GitHub Models": {
        "provider": "openai_compatible",
        "base_url": "https://models.github.ai/inference",
        "example_models": ["openai/gpt-4.1-mini", "deepseek/deepseek-r1"],
        "notes": "Low tier: 15 RPM, 150 requests/day, 8K-128K context. Uses your GitHub personal access token as the API key.",
        "default_rpm_limit": 15,
        "default_rpd_limit": 150,
    },
    "NVIDIA NIM": {
        "provider": "openai_compatible",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "example_models": ["nvidia/nemotron-3-super-120b", "moonshotai/kimi-k2.5"],
        "notes": "~40 RPM, ~1,000 requests/day equivalent, 128K-256K context. Prototyping only, no production SLA.",
        "default_rpm_limit": 40,
        "default_rpd_limit": 1000,
    },
    "Cohere": {
        "provider": "openai_compatible",
        "base_url": "https://api.cohere.ai/compatibility/v1",
        "example_models": ["command-a-03-2025", "command-r-plus-08-2024"],
        "notes": "Trial API keys are free and rate-limited (fine for this app's usage). "
                 "Uses Cohere's OpenAI-compatibility layer, not all OpenAI params are supported.",
        "default_rpm_limit": 20,
        "default_rpd_limit": None,
    },
    "Anthropic": {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "example_models": ["claude-sonnet-5", "claude-haiku-4-5-20251001"],
        "notes": "No free tier, but included since it's a common primary model. Pay-as-you-go API credits.",
        "default_rpm_limit": None,
        "default_rpd_limit": None,
    },
    "OpenAI": {
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "example_models": ["gpt-4.1-mini", "gpt-4.1"],
        "notes": "No standing free tier; new accounts sometimes get trial credits.",
        "default_rpm_limit": None,
        "default_rpd_limit": None,
    },
}