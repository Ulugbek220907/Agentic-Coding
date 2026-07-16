"""
Provider adapters.

Each adapter takes a normalized request (messages + tool schema) and returns
a normalized response:

    {
        "content": str | None,          # assistant's plain text, if any
        "tool_calls": [                  # list of tool calls the model wants to make
            {"id": str, "name": str, "arguments": dict}
        ],
        "raw": <original json response>,
    }

This lets core/agent.py and core/failover.py work with any provider without
caring whether it's OpenAI-style function calling or Anthropic's tool_use
blocks.

Only the `requests` library is required -- no vendor SDKs -- so this works
against ANY OpenAI-compatible endpoint (OpenAI, Groq, DeepSeek, Together,
OpenRouter, Mistral, local Ollama / LM Studio servers, etc.) plus native
Anthropic.
"""
from __future__ import annotations

import json
import requests
from typing import List, Dict, Any

from .config import ModelConfig


class ProviderError(Exception):
    """Raised for any failure calling a model: timeout, HTTP error, bad response."""
    def __init__(self, message: str, retryable: bool = True, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable  # False for e.g. bad API key -- don't bother retrying
        self.retry_after = retry_after  # seconds, if the provider told us explicitly (Retry-After header)


def _parse_retry_after(resp) -> float | None:
    """Parse a Retry-After header (seconds, or an HTTP-date) if present.
    Providers that actually send this (many do on 429) let us wait exactly
    as long as needed instead of guessing with a flat cooldown."""
    header = resp.headers.get("Retry-After")
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        dt = parsedate_to_datetime(header)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def _tools_to_openai_schema(tools: List[dict]) -> List[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in tools
    ]


# --- Generic internal message format used by core/agent.py ---
#
#   {"role": "system", "content": str}
#   {"role": "user", "content": str}
#   {"role": "assistant", "content": str|None, "tool_calls": [{"id","name","arguments"}]}
#   {"role": "tool_result", "tool_call_id": str, "name": str, "content": str}
#
# Each provider adapter converts this generic history into whatever shape
# its API expects, so the SAME conversation can be replayed against a
# completely different provider if the router fails over mid-task.

def _to_openai_messages(history: List[dict]) -> List[dict]:
    out = []
    for m in history:
        role = m["role"]
        if role in ("system", "user"):
            out.append({"role": role, "content": m["content"]})
        elif role == "assistant":
            msg = {"role": "assistant", "content": m.get("content")}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                    }
                    for tc in m["tool_calls"]
                ]
            out.append(msg)
        elif role == "tool_result":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
    return out


def call_openai_compatible(model: ModelConfig, history: List[dict], tools: List[dict]) -> Dict[str, Any]:
    url = model.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {model.api_key}",
        "Content-Type": "application/json",
        **model.extra_headers,
    }
    payload = {
        "model": model.model_id,
        "messages": _to_openai_messages(history),
        "temperature": 0.2,
    }
    if tools:
        payload["tools"] = _tools_to_openai_schema(tools)
        payload["tool_choice"] = "auto"

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=model.timeout_seconds)
    except requests.exceptions.Timeout:
        raise ProviderError(f"{model.name}: request timed out", retryable=True)
    except requests.exceptions.ConnectionError as e:
        raise ProviderError(f"{model.name}: connection failed ({e})", retryable=True)

    if resp.status_code == 401:
        raise ProviderError(f"{model.name}: invalid API key (401)", retryable=False)
    if resp.status_code == 429:
        raise ProviderError(f"{model.name}: rate limited (429)", retryable=True, retry_after=_parse_retry_after(resp))
    if resp.status_code >= 500:
        raise ProviderError(f"{model.name}: server error ({resp.status_code})", retryable=True)
    if resp.status_code >= 400:
        raise ProviderError(f"{model.name}: request error ({resp.status_code}): {resp.text[:300]}", retryable=False)

    data = resp.json()
    try:
        choice = data["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise ProviderError(f"{model.name}: unexpected response shape ({e})", retryable=True)

    tool_calls = []
    for tc in choice.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"]["arguments"])
        except (KeyError, json.JSONDecodeError):
            args = {}
        tool_calls.append({"id": tc.get("id", ""), "name": tc["function"]["name"], "arguments": args})

    return {"content": choice.get("content"), "tool_calls": tool_calls, "raw": data}


def _to_anthropic_messages(history: List[dict]):
    """Returns (system_prompt, messages) in Anthropic's shape."""
    system_prompt = ""
    conv = []
    for m in history:
        role = m["role"]
        if role == "system":
            system_prompt += m["content"] + "\n"
        elif role == "user":
            conv.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["arguments"]})
            conv.append({"role": "assistant", "content": blocks})
        elif role == "tool_result":
            conv.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}],
            })
    return system_prompt.strip(), conv


def call_anthropic(model: ModelConfig, history: List[dict], tools: List[dict]) -> Dict[str, Any]:
    url = model.base_url.rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": model.api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        **model.extra_headers,
    }

    system_prompt, conv = _to_anthropic_messages(history)

    payload = {
        "model": model.model_id,
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": conv,
    }
    if tools:
        payload["tools"] = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in tools
        ]

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=model.timeout_seconds)
    except requests.exceptions.Timeout:
        raise ProviderError(f"{model.name}: request timed out", retryable=True)
    except requests.exceptions.ConnectionError as e:
        raise ProviderError(f"{model.name}: connection failed ({e})", retryable=True)

    if resp.status_code == 401:
        raise ProviderError(f"{model.name}: invalid API key (401)", retryable=False)
    if resp.status_code == 429:
        raise ProviderError(f"{model.name}: rate limited (429)", retryable=True, retry_after=_parse_retry_after(resp))
    if resp.status_code >= 500:
        raise ProviderError(f"{model.name}: server error ({resp.status_code})", retryable=True)
    if resp.status_code >= 400:
        raise ProviderError(f"{model.name}: request error ({resp.status_code}): {resp.text[:300]}", retryable=False)

    data = resp.json()
    content_text = None
    tool_calls = []
    for block in data.get("content", []):
        if block["type"] == "text":
            content_text = (content_text or "") + block["text"]
        elif block["type"] == "tool_use":
            tool_calls.append({"id": block["id"], "name": block["name"], "arguments": block.get("input", {})})

    return {"content": content_text, "tool_calls": tool_calls, "raw": data}


PROVIDER_FUNCS = {
    "openai_compatible": call_openai_compatible,
    "anthropic": call_anthropic,
}


def call_model(model: ModelConfig, messages: List[dict], tools: List[dict]) -> Dict[str, Any]:
    func = PROVIDER_FUNCS.get(model.provider)
    if not func:
        raise ProviderError(f"Unknown provider type: {model.provider}", retryable=False)
    return func(model, messages, tools)