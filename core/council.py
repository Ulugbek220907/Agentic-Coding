"""
CouncilRouter: multi-model consensus on top of ModelRouter.

Three modes (see CouncilMode):
  LIGHT   - single model, identical to plain ModelRouter.call().
  COUNCIL - top 2-3 priority models are queried in parallel each step.
  MAX     - all enabled models are queried in parallel each step.

Design goal: keep token/dollar cost close to "1 extra model call per
participant" and NOT multiply cost further with arbitration overhead.

How a step is resolved
-----------------------
1. All participating models are called in parallel (each still gets its own
   failover via a per-model ModelRouter, so a flaky individual model doesn't
   sink the whole council step -- it just doesn't get a vote that round).
2. If one or more responses include tool_calls:
     - Tool calls are compared by a normalized signature (name + sorted
       json-serialized arguments).
     - If a signature has >=2 votes (or is the only one), it's the "winner"
       and is executed EXACTLY ONCE regardless of how many models proposed
       it. This is the key cost/safety guardrail: we never run the same
       write_file/run_command multiple times just because multiple models
       agreed on it.
     - If there's no majority (all models disagree), we fall back to the
       proposal from the highest-priority participating model, and flag the
       round as a "disagreement" so the UI can show it transparently. This
       avoids paying for an extra arbitration/judge call on every
       disagreement -- arbitration by priority-order is free.
3. If NONE of the responses include tool_calls (i.e. everyone thinks the
   task is done / wants to reply in plain text), we treat this as the final
   answer step and DO spend one extra call: the highest-priority model is
   asked to synthesize the best possible reply given all the drafts. This
   is the one place where paying for an extra call is worth it, since it's
   the last thing the user reads, and it happens at most once per task
   (not once per step).
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from .config import ModelConfig
from .failover import ModelRouter, AllModelsFailedError, CallResult


class CouncilMode(str, Enum):
    LIGHT = "light"       # 1 model
    COUNCIL = "council"   # 2-3 models
    MAX = "max"           # all enabled models


def participants_for_mode(sorted_models: List[ModelConfig], mode: CouncilMode) -> List[ModelConfig]:
    """sorted_models: already priority-sorted, enabled-only list."""
    if mode == CouncilMode.LIGHT:
        return sorted_models[:1]
    if mode == CouncilMode.COUNCIL:
        return sorted_models[:3] if len(sorted_models) >= 3 else sorted_models[:2] or sorted_models[:1]
    if mode == CouncilMode.MAX:
        return sorted_models
    return sorted_models[:1]


def _tool_call_signature(tool_calls: List[dict]) -> str:
    """Order-independent signature so two models proposing the same set of
    calls (possibly in a different order) are recognized as agreeing."""
    normalized = sorted(
        json.dumps({"name": tc["name"], "arguments": tc.get("arguments", {})}, sort_keys=True)
        for tc in tool_calls
    )
    return "||".join(normalized)


@dataclass
class ParticipantResult:
    model: ModelConfig
    call_result: Optional[CallResult] = None
    error: Optional[str] = None


@dataclass
class CouncilStepResult:
    response: Dict[str, Any]              # the winning/synthesized response, same shape as provider output
    contributors: List[str]               # model names that actually returned a usable response this step
    winning_models: List[str]             # model names whose proposal was the one used/executed
    agreement: bool                       # True if the winning proposal had >=2 votes (or was unanimous / only one)
    synthesized: bool                     # True if this was a final-text synthesis step
    failed_models: List[str] = field(default_factory=list)
    raw_texts: Dict[str, str] = field(default_factory=dict)  # model name -> its draft text, for UI transparency


class CouncilRouter:
    def __init__(
        self,
        all_models: List[ModelConfig],
        mode: CouncilMode = CouncilMode.LIGHT,
        on_event: Optional[Callable[[str, dict], None]] = None,
        max_workers: int = 6,
        shared_routers: Optional[Dict[str, ModelRouter]] = None,
    ):
        self.all_models = all_models
        self.mode = mode
        self.on_event = on_event or (lambda name, data: None)
        self.max_workers = max_workers
        # One ModelRouter per participant so each individual model still
        # benefits from its own failover/cooldown bookkeeping across turns.
        # If shared_routers is passed in (e.g. from Team mode, which
        # constructs a fresh CouncilRouter for every subtask/review call),
        # that cooldown/backoff state is shared across ALL of those calls
        # instead of being thrown away and rebuilt from scratch every time
        # -- without this, a broken/deprecated model gets retried with zero
        # memory of its previous failure on every single call.
        self._sub_routers: Dict[str, ModelRouter] = shared_routers if shared_routers is not None else {}

    def _router_for(self, model: ModelConfig) -> ModelRouter:
        if model.name not in self._sub_routers:
            self._sub_routers[model.name] = ModelRouter([model])
        return self._sub_routers[model.name]

    def _participants(self) -> List[ModelConfig]:
        # NOTE: deliberately preserve the caller's ordering rather than
        # re-sorting by priority here. The UI's "preferred model" picker
        # moves a manually-chosen model to the front of the list before
        # constructing this router; re-sorting by priority would silently
        # discard that choice.
        enabled = [m for m in self.all_models if m.enabled]
        return participants_for_mode(enabled, self.mode)

    def _query_all(self, messages: List[dict], tools: List[dict]) -> List[ParticipantResult]:
        participants = self._participants()
        results: List[ParticipantResult] = []

        def _call_one(model: ModelConfig) -> ParticipantResult:
            try:
                cr = self._router_for(model).call(messages, tools)
                return ParticipantResult(model=model, call_result=cr)
            except AllModelsFailedError as e:
                return ParticipantResult(model=model, error=str(e))

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(participants) or 1)) as pool:
            futures = {pool.submit(_call_one, m): m for m in participants}
            for fut in as_completed(futures):
                results.append(fut.result())

        # Keep deterministic priority order in the returned list (as_completed
        # scrambles order); the caller cares about "highest priority ok result".
        order = {m.name: i for i, m in enumerate(participants)}
        results.sort(key=lambda r: order.get(r.model.name, 999))
        return results

    def call(self, messages: List[dict], tools: List[dict]) -> CouncilStepResult:
        if self.mode == CouncilMode.LIGHT:
            results = self._query_all(messages, tools)
            if not results or results[0].error:
                raise AllModelsFailedError([r.error or "no result" for r in results] or ["No models configured."])
            r = results[0]
            resp = r.call_result.response
            return CouncilStepResult(
                response=resp,
                contributors=[r.model.name],
                winning_models=[r.model.name],
                agreement=True,
                synthesized=False,
                raw_texts={r.model.name: resp.get("content") or ""},
            )

        results = self._query_all(messages, tools)
        ok = [r for r in results if r.call_result is not None]
        failed = [r.model.name for r in results if r.call_result is None]

        for r in ok:
            self.on_event("council_vote", {
                "model": r.model.name,
                "has_tool_calls": bool(r.call_result.response.get("tool_calls")),
            })
        for name in failed:
            self.on_event("council_participant_failed", {"model": name})

        if not ok:
            raise AllModelsFailedError([r.error or "unknown error" for r in results])

        raw_texts = {r.model.name: (r.call_result.response.get("content") or "") for r in ok}

        # --- Case 1: at least one participant proposed tool calls -> vote ---
        proposals_with_tools = [r for r in ok if r.call_result.response.get("tool_calls")]
        if proposals_with_tools:
            votes: Dict[str, List[ParticipantResult]] = {}
            for r in proposals_with_tools:
                sig = _tool_call_signature(r.call_result.response["tool_calls"])
                votes.setdefault(sig, []).append(r)

            # Winning signature = most votes; ties broken by highest priority
            # participant among the tied signatures (ok is already priority-sorted).
            best_sig = max(votes.keys(), key=lambda s: (len(votes[s]), -min(
                [i for i, r in enumerate(ok) if r in votes[s]]
            )))
            winners = votes[best_sig]
            agreement = len(winners) >= 2 or len(proposals_with_tools) == 1

            if not agreement:
                self.on_event("council_disagreement", {
                    "proposals": {r.model.name: r.call_result.response.get("tool_calls") for r in proposals_with_tools},
                    "chosen": winners[0].model.name,
                })

            chosen = winners[0]
            return CouncilStepResult(
                response=chosen.call_result.response,
                contributors=[r.model.name for r in ok],
                winning_models=[r.model.name for r in winners],
                agreement=agreement,
                synthesized=False,
                failed_models=failed,
                raw_texts=raw_texts,
            )

        # --- Case 2: nobody wants a tool call -> everyone thinks we're done.
        # Synthesize the best final answer with the top-priority model. ---
        texts = [r.call_result.response.get("content") or "" for r in ok]
        if len(ok) == 1 or len(set(t.strip() for t in texts)) == 1:
            # Only one voice, or they all already agree verbatim -- no need
            # to spend an extra synthesis call.
            chosen = ok[0]
            return CouncilStepResult(
                response=chosen.call_result.response,
                contributors=[r.model.name for r in ok],
                winning_models=[r.model.name for r in ok],
                agreement=True,
                synthesized=False,
                failed_models=failed,
                raw_texts=raw_texts,
            )

        synthesizer_model = ok[0].model  # highest priority participant
        synth_prompt = (
            "Multiple AI assistants were asked the same question and each "
            "produced a draft final answer below. Write a single best answer "
            "that combines their strengths, resolves any contradictions in "
            "favor of whichever seems most accurate, and drops redundancy. "
            "Reply with ONLY the final answer text, no preamble like "
            "'here is the synthesized answer'.\n\n"
            + "\n\n".join(f"--- Draft from {r.model.name} ---\n{r.call_result.response.get('content') or ''}" for r in ok)
        )
        synth_messages = messages + [{"role": "user", "content": synth_prompt}]
        try:
            synth_result = self._router_for(synthesizer_model).call(synth_messages, [])
            merged_text = synth_result.response.get("content") or texts[0]
        except AllModelsFailedError:
            # Synthesis failed (e.g. that model just went down) -- fall back
            # to the top-priority participant's own draft rather than error out.
            merged_text = texts[0]

        self.on_event("council_synthesized", {
            "contributors": [r.model.name for r in ok],
            "synthesizer": synthesizer_model.name,
        })

        return CouncilStepResult(
            response={"content": merged_text, "tool_calls": []},
            contributors=[r.model.name for r in ok],
            winning_models=[synthesizer_model.name],
            agreement=True,
            synthesized=True,
            failed_models=failed,
            raw_texts=raw_texts,
        )