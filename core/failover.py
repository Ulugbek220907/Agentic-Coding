"""
ModelRouter: tries configured models in priority order and automatically
fails over to the next one if a model errors out, times out, or is
unreachable. This is the piece that satisfies "if a model is not responding,
automatically switch to another model".

Behavior:
- Models are tried in priority order (lowest `priority` number first).
- A model that fails with a *retryable* error (timeout, connection error,
  429, 5xx) is skipped for the rest of this call, and the router moves to
  the next model.
- A model that fails with a *non-retryable* error (bad API key, malformed
  request) is also skipped, but logged distinctly so you know to fix the
  config rather than expect it to recover.
- If ALL models fail, AllModelsFailedError is raised with a summary.
- The router remembers which model last succeeded (`self.last_good_model`)
  and always tries that one first on the next call -- so once a model
  comes back healthy, you don't keep paying the cost of retrying dead ones
  every single turn. Failed models are given a cooldown before retry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Callable, Optional, Dict, Any

from .config import ModelConfig
from .providers import call_model, ProviderError

COOLDOWN_SECONDS = 60  # how long to avoid a model after it fails


class AllModelsFailedError(Exception):
    def __init__(self, attempts: List[str]):
        super().__init__("All configured models failed:\n" + "\n".join(attempts))
        self.attempts = attempts


@dataclass
class CallResult:
    model_used: ModelConfig
    response: Dict[str, Any]
    attempts: List[str]  # log of models tried (including failures) for this call


class ModelRouter:
    def __init__(self, models: List[ModelConfig], on_event: Optional[Callable[[str, dict], None]] = None):
        """
        models: list of ModelConfig, already sorted by priority (see
                ConfigManager.sorted_models()).
        on_event: optional callback(event_name, data) fired on model_switch,
                  model_failed, model_success -- hook this up to your UI to
                  show "Switched to backup model: X" style notifications.
        """
        self.models = models
        self.on_event = on_event or (lambda name, data: None)
        self.last_good_model_name: Optional[str] = None
        self._cooldowns: Dict[str, float] = {}  # model name -> timestamp until which to skip it

    def _ordered_models(self) -> List[ModelConfig]:
        now = time.time()
        available = [m for m in self.models if self._cooldowns.get(m.name, 0) <= now]
        # Prefer the last known-good model first if it's available.
        if self.last_good_model_name:
            available.sort(key=lambda m: 0 if m.name == self.last_good_model_name else 1)
        return available or self.models  # if everything's cooling down, try anyway

    def call(self, messages: List[dict], tools: List[dict]) -> CallResult:
        attempts = []
        ordered = self._ordered_models()

        if not ordered:
            raise AllModelsFailedError(["No models configured."])

        for i, model in enumerate(ordered):
            if model.name != self.last_good_model_name and i > 0:
                self.on_event("model_switch", {"model": model.name})
            try:
                response = call_model(model, messages, tools)
                self.on_event("model_success", {"model": model.name})
                self.last_good_model_name = model.name
                self._cooldowns.pop(model.name, None)
                return CallResult(model_used=model, response=response, attempts=attempts)
            except ProviderError as e:
                attempts.append(f"{model.name}: {e}")
                self.on_event("model_failed", {"model": model.name, "error": str(e), "retryable": e.retryable})
                # Cooldown so we don't hammer a dead endpoint on every turn.
                self._cooldowns[model.name] = time.time() + COOLDOWN_SECONDS
                if model.name == self.last_good_model_name:
                    self.last_good_model_name = None
                continue

        raise AllModelsFailedError(attempts)
