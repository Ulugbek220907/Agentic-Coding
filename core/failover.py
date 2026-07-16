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
import collections
from dataclasses import dataclass
from typing import List, Callable, Optional, Dict, Any

from .config import ModelConfig
from .providers import call_model, ProviderError

DEFAULT_COOLDOWN_SECONDS = 60   # fallback when the provider doesn't tell us how long to wait
MAX_COOLDOWN_SECONDS = 600      # cap so a bad Retry-After value can't stall a model for hours


class UsageTracker:
    """
    Tracks how many times we've actually called each model, so a
    user-configured rpm_limit/rpd_limit (see ModelConfig) can be enforced
    BEFORE making a request -- skipping a model that's about to blow its
    quota instead of finding out via a real 429 from the provider.

    This is process-wide (module-level singleton), not per-CouncilRouter --
    a new CouncilRouter/ModelRouter gets built on every Send, so tracking
    would otherwise reset every single message and the limit would do
    nothing.

    NOTE: this only tracks usage since the app was last started. It's a
    best-effort LOCAL governor, not a substitute for the provider's real
    quota -- restarting the app resets these counters even though your
    actual quota with the provider hasn't reset. It exists to stop *this
    app* from being the thing that blows your quota, not to guarantee the
    provider will never 429 you for other reasons (other apps/scripts
    using the same key, clock skew, etc).
    """

    def __init__(self):
        self._timestamps: Dict[str, "collections.deque[float]"] = {}

    def record(self, model_name: str):
        now = time.time()
        dq = self._timestamps.setdefault(model_name, collections.deque())
        dq.append(now)
        cutoff = now - 86400
        while dq and dq[0] < cutoff:
            dq.popleft()

    def seconds_until_allowed(
        self, model_name: str, rpm_limit: Optional[int], rpd_limit: Optional[int]
    ) -> float:
        """0 if a call is allowed right now, else how many seconds to wait."""
        if not rpm_limit and not rpd_limit:
            return 0.0
        dq = self._timestamps.get(model_name)
        if not dq:
            return 0.0
        now = time.time()
        wait = 0.0
        if rpm_limit:
            in_window = [t for t in dq if t > now - 60]
            if len(in_window) >= rpm_limit:
                wait = max(wait, 60 - (now - in_window[0]))
        if rpd_limit:
            in_window = [t for t in dq if t > now - 86400]
            if len(in_window) >= rpd_limit:
                wait = max(wait, 86400 - (now - in_window[0]))
        return wait


_usage_tracker = UsageTracker()  # process-wide singleton, see docstring above


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
        self._consecutive_failures: Dict[str, int] = {}  # model name -> count, for escalating backoff

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
            wait = _usage_tracker.seconds_until_allowed(model.name, model.rpm_limit, model.rpd_limit)
            if wait > 0:
                attempts.append(
                    f"{model.name}: self-imposed limit reached (configured in Model settings), "
                    f"would need to wait {wait:.0f}s"
                )
                self.on_event("model_self_throttled", {"model": model.name, "wait_seconds": wait})
                continue

            if model.name != self.last_good_model_name and i > 0:
                self.on_event("model_switch", {"model": model.name})
            try:
                _usage_tracker.record(model.name)
                response = call_model(model, messages, tools)
                self.on_event("model_success", {"model": model.name})
                self.last_good_model_name = model.name
                self._cooldowns.pop(model.name, None)
                self._consecutive_failures.pop(model.name, None)
                return CallResult(model_used=model, response=response, attempts=attempts)
            except ProviderError as e:
                attempts.append(f"{model.name}: {e}")
                self.on_event("model_failed", {"model": model.name, "error": str(e), "retryable": e.retryable})
                self._consecutive_failures[model.name] = self._consecutive_failures.get(model.name, 0) + 1
                if e.retry_after is not None:
                    cooldown = min(e.retry_after, MAX_COOLDOWN_SECONDS)
                else:
                    # Escalate: 60s, 120s, 240s, ... so we stop hammering a
                    # quota that clearly isn't coming back in under a minute,
                    # instead of cascading into "all models failed" again on
                    # the very next turn.
                    n = self._consecutive_failures[model.name]
                    cooldown = min(DEFAULT_COOLDOWN_SECONDS * (2 ** (n - 1)), MAX_COOLDOWN_SECONDS)
                self._cooldowns[model.name] = time.time() + cooldown
                if model.name == self.last_good_model_name:
                    self.last_good_model_name = None
                continue

        raise AllModelsFailedError(attempts)