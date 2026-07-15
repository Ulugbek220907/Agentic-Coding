"""
Quick-add presets for providers with usable free tiers, so you don't have
to hand-type base URLs. Rate limits change often -- the `notes` field is a
snapshot, always double check the provider's own dashboard/docs before
relying on it for anything real.
"""

PROVIDER_PRESETS = {
    "SiliconFlow": {
        "provider": "openai_compatible",
        "base_url": "https://api.siliconflow.com/v1",
        "example_models": ["deepseek-ai/DeepSeek-R1", "deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-7B-Instruct"],
        "notes": "Free tier: 1,000-10,000 RPM (model dependent), requires identity verification. "
                 "Up to 1M context on some models. Older quantizations get deprecated quickly.",
    },
    "Google AI Studio (Gemini)": {
        "provider": "openai_compatible",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "example_models": ["gemini-2.5-flash", "gemini-flash-lite-latest"],
        "notes": "Free tier: 15-30 RPM, 1,500 requests/day, up to 1M context. "
                 "Prompts may be used for model training outside the EU/UK/EEA.",
    },
    "OpenRouter": {
        "provider": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "example_models": ["openai/gpt-oss-120b:free", "cohere/command-r7b-12-2024:free"],
        "notes": "Free (':free') models: 20 RPM, 50 requests/day (up to 1,000/day with a $10 deposit), "
                 "up to 262K context. High demand can cause latency spikes.",
    },
    "Mistral AI": {
        "provider": "openai_compatible",
        "base_url": "https://api.mistral.ai/v1",
        "example_models": ["codestral-2501", "devstral-small-2505"],
        "notes": "Experiment plan: ~1 RPS (~60 RPM), 1B tokens/month, 128K-256K context. "
                 "Requires consent to data training.",
    },
    "Cerebras": {
        "provider": "openai_compatible",
        "base_url": "https://api.cerebras.ai/v1",
        "example_models": ["gpt-oss-120b"],
        "notes": "Free tier: 30 RPM, 1M tokens/day, 128K context. Limited open-weight model catalog.",
    },
    "Groq": {
        "provider": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "example_models": ["openai/gpt-oss-120b", "qwen/qwen3-32b"],
        "notes": "Free tier: 30 RPM, 1,000 requests/day, 128K context. Strict tokens-per-minute ceilings.",
    },
    "GitHub Models": {
        "provider": "openai_compatible",
        "base_url": "https://models.github.ai/inference",
        "example_models": ["openai/gpt-4.1-mini", "deepseek/deepseek-r1"],
        "notes": "Low tier: 15 RPM, 150 requests/day, 8K-128K context. Uses your GitHub personal access token as the API key.",
    },
    "NVIDIA NIM": {
        "provider": "openai_compatible",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "example_models": ["nvidia/nemotron-3-super-120b", "moonshotai/kimi-k2.5"],
        "notes": "~40 RPM, ~1,000 requests/day equivalent, 128K-256K context. Prototyping only, no production SLA.",
    },
    "Cohere": {
        "provider": "openai_compatible",
        "base_url": "https://api.cohere.ai/compatibility/v1",
        "example_models": ["command-a-03-2025", "command-r-plus-08-2024"],
        "notes": "Trial API keys are free and rate-limited (fine for this app's usage). "
                 "Uses Cohere's OpenAI-compatibility layer, not all OpenAI params are supported.",
    },
    "Anthropic": {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "example_models": ["claude-sonnet-5", "claude-haiku-4-5-20251001"],
        "notes": "No free tier, but included since it's a common primary model. Pay-as-you-go API credits.",
    },
    "OpenAI": {
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "example_models": ["gpt-4.1-mini", "gpt-4.1"],
        "notes": "No standing free tier; new accounts sometimes get trial credits.",
    },
}
