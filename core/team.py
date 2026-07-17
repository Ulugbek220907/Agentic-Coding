"""
Team mode: "divide & conquer" multi-model collaboration, as distinct from
Council/Max (which have every model vote on the SAME step). Team mode:

  1. PLAN    -- one model breaks the user's request into independent
                subtasks, each declaring which files it will touch.
  2. SCHEDULE -- subtasks whose target files don't overlap are grouped into
                "waves" that run concurrently; overlapping subtasks are
                pushed to a later wave so two workers never race on the
                same file.
  3. WORK    -- each subtask in a wave is assigned to a DIFFERENT model
                (round-robin) and runs the normal agentic tool-loop
                (core.agent.run_agent_task) scoped to just that piece.
                Rate-limit pacing comes for free: each subtask's router is
                a CouncilRouter in LIGHT mode over [assigned_model, ...
                other models as fallback], which already consults
                UsageTracker (core/failover.py) before every call and skips
                a model that's about to exceed its configured RPM/RPD.
  4. REVIEW  -- after a wave finishes, a reviewer model (preferring one
                that did NOT do the work, to avoid self-grading) checks
                each subtask's target files against its acceptance
                criteria. Failed subtasks get reassigned to a DIFFERENT
                model and retried, up to MAX_RETRIES_PER_SUBTASK times.
  5. DONE    -- a final summary of what passed, what got retried, and
                anything still failing after retries are exhausted.

No single model gets outsized influence: the planner is just whichever
model answers first, work is spread round-robin across all enabled
models, and review prefers a different model than the one who did the
work.
"""
from __future__ import annotations

import json
import queue
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Generator, List, Optional

from .config import ModelConfig
from .council import CouncilRouter, CouncilMode
from .failover import AllModelsFailedError
from .agent import run_agent_task
from .tools import ProjectTools

MAX_RETRIES_PER_SUBTASK = 2
MAX_SUBTASK_ITERATIONS = 15  # smaller than a full task's budget -- each piece should be focused

PLANNER_SYSTEM_PROMPT = """You are a project planner splitting work between several AI coding workers who will run AT THE SAME TIME. Break the user's request into 2-6 subtasks that can be done independently and in parallel with minimal file overlap between them.

Respond with ONLY strict JSON, no markdown fences, no commentary, in exactly this shape:
{"subtasks": [{"id": 1, "description": "...", "target_files": ["relative/path.py"], "acceptance_criteria": "a concrete, checkable description of what 'done correctly' looks like for this piece"}]}

Rules:
- 2-6 subtasks. Fewer, larger subtasks are fine if the request doesn't split further.
- target_files must be relative paths within the project. Two subtasks should not list the same file unless the work genuinely can't be split further.
- acceptance_criteria must be something checkable by reading the resulting files -- not vague ("looks good") but concrete ("exports a Calculator class with add/subtract/multiply/divide methods").
"""


def _parse_json_response(text: str) -> Optional[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text.strip())
    except Exception:
        return None


def _group_into_waves(subtasks: List[dict]) -> List[List[dict]]:
    """First-fit bin packing by target_files conflict: two subtasks in the
    same wave never share a target file, so running a wave concurrently
    can't race two workers on the same write."""
    waves: List[List[dict]] = []
    for st in subtasks:
        st_files = set(st.get("target_files", []))
        placed = False
        for wave in waves:
            if not any(st_files & set(other.get("target_files", [])) for other in wave):
                wave.append(st)
                placed = True
                break
        if not placed:
            waves.append([st])
    return waves


def _assign_models(wave: List[dict], models: List[ModelConfig], exclude: dict) -> dict:
    """Round-robin assignment within a wave, preferring a model not already
    used elsewhere in the same wave, and never one in that subtask's
    exclude set (models that already failed it in a previous attempt)."""
    assignment = {}
    used = set()
    for i, st in enumerate(wave):
        st_exclude = exclude.get(st["id"], set())
        candidates = [m for m in models if m.name not in used and m.name not in st_exclude]
        if not candidates:
            candidates = [m for m in models if m.name not in st_exclude] or models
        assignment[st["id"]] = candidates[i % len(candidates)]
        used.add(assignment[st["id"]].name)
    return assignment


def run_team_task(
    models: List[ModelConfig],
    project_root: str,
    user_instruction: str,
    confirm_write=None,
    confirm_command=None,
    stop_check: Optional[Callable[[], bool]] = None,
    project_memory: Optional[List[str]] = None,
) -> Generator[dict, None, None]:
    stop_check = stop_check or (lambda: False)

    if len(models) < 2:
        yield {"type": "error", "text": "Team mode needs at least 2 enabled models to divide work between."}
        return

    # ---------- 1. PLAN ----------
    yield {"type": "status", "text": "Planning: splitting the request into subtasks..."}
    planner_router = CouncilRouter(models, mode=CouncilMode.LIGHT)
    plan_messages = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": user_instruction},
    ]
    try:
        plan_result = planner_router.call(plan_messages, tools=[])
    except AllModelsFailedError as e:
        yield {"type": "error", "text": f"Planning failed: {e}"}
        return

    plan = _parse_json_response(plan_result.response.get("content", ""))
    if not plan or "subtasks" not in plan:
        yield {"type": "error", "text": "Planner didn't return valid JSON. Try again, or use Light/Council mode for this request."}
        return

    subtasks = plan["subtasks"]
    for st in subtasks:
        st.setdefault("target_files", [])
        st["status"] = "pending"
        st["retries"] = 0
        st["assigned_model"] = None
        st["failed_models"] = set()

    yield {"type": "plan_ready", "subtasks": [
        {"id": st["id"], "description": st["description"], "target_files": st["target_files"]}
        for st in subtasks
    ]}

    waves = _group_into_waves(subtasks)
    yield {"type": "status", "text": f"Scheduled into {len(waves)} wave(s) of parallel work."}

    tools = ProjectTools(project_root)  # only used for review's read_file calls

    def _run_subtask(st: dict, model: ModelConfig, event_queue: "queue.Queue"):
        st["assigned_model"] = model.name
        event_queue.put({"type": "subtask_assigned", "subtask_id": st["id"], "model": model.name})
        # Assigned model tried first; the rest of the pool is fallback --
        # so a subtask doesn't die just because its assigned model happens
        # to be rate-limited or briefly down.
        fallback = [m for m in models if m.name != model.name]
        subtask_router = CouncilRouter([model] + fallback, mode=CouncilMode.LIGHT)
        subtask_prompt = (
            f"You are one of several AI workers collaborating on a larger project. "
            f"The overall user request was:\n\"{user_instruction}\"\n\n"
            f"Your specific piece of this project is:\n{st['description']}\n\n"
            f"Focus ONLY on this piece -- don't redo work outside your scope. "
            f"Expected files you'll create/modify: {st['target_files']}. "
            f"Call task_complete when this piece is done."
        )
        summary = None
        try:
            for ev in run_agent_task(
                subtask_router, project_root, subtask_prompt,
                max_iterations=MAX_SUBTASK_ITERATIONS,
                confirm_write=confirm_write, confirm_command=confirm_command,
                stop_check=stop_check, project_memory=project_memory,
            ):
                event_queue.put({"type": "subtask_event", "subtask_id": st["id"], "event": ev})
                if ev["type"] == "done":
                    summary = ev.get("summary") or "(completed without an explicit summary)"
                elif ev["type"] == "error":
                    summary = f"(error: {ev['text']})"
                elif ev["type"] == "stopped":
                    summary = "(stopped by user)"
        except Exception as e:
            summary = f"(unexpected error: {e})"
        event_queue.put({"type": "subtask_done", "subtask_id": st["id"], "summary": summary})

    def _review_subtask(st: dict) -> tuple[bool, str]:
        file_contents = []
        for path in st["target_files"]:
            content = tools.read_file(path)
            file_contents.append(f"--- {path} ---\n{content}")
        reviewer_pool = [m for m in models if m.name != st["assigned_model"]] or models
        reviewer_router = CouncilRouter(reviewer_pool, mode=CouncilMode.LIGHT)
        review_prompt = (
            f"A subtask was assigned to an AI worker:\n{st['description']}\n\n"
            f"Acceptance criteria: {st['acceptance_criteria']}\n\n"
            f"Current contents of the target files:\n" + "\n\n".join(file_contents) + "\n\n"
            f"Was this subtask completed correctly and completely? Respond with ONLY strict JSON: "
            f'{{"passed": true or false, "reason": "short explanation"}}'
        )
        try:
            result = reviewer_router.call([{"role": "user", "content": review_prompt}], tools=[])
        except AllModelsFailedError as e:
            return False, f"Could not run review: {e}"
        parsed = _parse_json_response(result.response.get("content", ""))
        if not parsed:
            return False, "Reviewer didn't return valid JSON -- treating as unverified."
        return bool(parsed.get("passed")), parsed.get("reason", "")

    # ---------- 2 & 3. SCHEDULE + WORK (wave by wave) ----------
    for wave_num, wave in enumerate(waves, start=1):
        if stop_check():
            yield {"type": "stopped", "text": "Team task stopped by user."}
            return
        yield {"type": "status", "text": f"Wave {wave_num}/{len(waves)}: {len(wave)} subtask(s) running in parallel."}

        pending = list(wave)
        attempt = 0
        while pending and attempt <= MAX_RETRIES_PER_SUBTASK:
            attempt += 1
            exclude = {st["id"]: st["failed_models"] for st in pending}
            assignment = _assign_models(pending, models, exclude)

            event_q: "queue.Queue" = queue.Queue()
            with ThreadPoolExecutor(max_workers=len(pending)) as pool:
                futures = [pool.submit(_run_subtask, st, assignment[st["id"]], event_q) for st in pending]
                done_ids = set()
                while len(done_ids) < len(pending):
                    try:
                        ev = event_q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    yield ev
                    if ev["type"] == "subtask_done":
                        done_ids.add(ev["subtask_id"])
                for f in futures:
                    f.result()  # surface any thread exception

            # Review everyone who just ran this attempt.
            still_pending = []
            for st in pending:
                if stop_check():
                    yield {"type": "stopped", "text": "Team task stopped by user."}
                    return
                yield {"type": "status", "text": f"Reviewing subtask {st['id']}..."}
                passed, reason = _review_subtask(st)
                yield {"type": "review_result", "subtask_id": st["id"], "passed": passed, "reason": reason}
                if passed:
                    st["status"] = "passed"
                else:
                    st["failed_models"].add(st["assigned_model"])
                    st["retries"] += 1
                    if st["retries"] <= MAX_RETRIES_PER_SUBTASK:
                        st["status"] = "retrying"
                        still_pending.append(st)
                        yield {"type": "subtask_reassigned", "subtask_id": st["id"], "attempt": st["retries"] + 1}
                    else:
                        st["status"] = "needs_manual_review"
            pending = still_pending

    # ---------- 4. DONE ----------
    results = [
        {"id": st["id"], "description": st["description"], "status": st["status"], "assigned_model": st["assigned_model"]}
        for st in subtasks
    ]
    yield {"type": "team_done", "results": results}