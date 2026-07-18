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
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Generator, List, Optional, Dict

from .config import ModelConfig
from .council import CouncilRouter, CouncilMode
from .failover import AllModelsFailedError, ModelRouter
from .agent import run_agent_task
from .tools import ProjectTools
from . import project_map as project_map_mod

MAX_RETRIES_PER_SUBTASK = 2
MAX_REVIEW_INFRA_RETRIES = 2  # separate budget: re-trying a review that couldn't even RUN doesn't burn a worker retry
MAX_SUBTASK_ITERATIONS = 15  # smaller than a full task's budget -- each piece should be focused
MAX_SPEC_FILE_CHARS = 6000
SPEC_FILENAME_RE = re.compile(r"(structure|plan|spec|readme|requirements)", re.IGNORECASE)

PLANNER_SYSTEM_PROMPT = """You are a project planner splitting work between several AI coding workers who will run AT THE SAME TIME. Break the user's request into 2-6 subtasks that can be done independently and in parallel with minimal file overlap between them.

If a PROJECT MAP and/or spec/plan file contents are provided below, GROUND YOUR PLAN IN THEM:
- target_files must match the structure those documents actually describe. Do NOT invent a
  generic layout (e.g. separate models.py/logic.py/api.py files) unless the spec genuinely
  calls for that -- if the spec describes a single backend file, plan around that single file
  instead of splitting it into pieces that don't exist in the spec.
- If the project already has folders (e.g. a web_app/ folder), target_files should use those
  real paths (e.g. "web_app/app.py"), not invented flat paths (e.g. "app.py") that ignore the
  existing structure.

Respond with ONLY strict JSON, no markdown fences, no commentary, in exactly this shape:
{"subtasks": [{"id": 1, "description": "...", "target_files": ["relative/path.py"], "acceptance_criteria": "a concrete, checkable description of what 'done correctly' looks like for this piece"}]}

Rules:
- 2-6 subtasks. Fewer, larger subtasks are fine if the request doesn't split further.
- target_files must be relative paths within the project, matching real/intended structure as described above.
- Two subtasks should not list the same file unless the work genuinely can't be split further.
- acceptance_criteria must be something checkable by reading the resulting files -- not vague ("looks good") but concrete ("exports a Calculator class with add/subtract/multiply/divide methods").
"""


def _gather_planning_context(project_root: str, user_instruction: str) -> str:
    """
    Ground the planner in the ACTUAL project instead of letting it invent a
    generic file layout. This is the fix for the most damaging failure mode
    observed: a planner that never saw structure.txt invented a
    models.py/logic.py/api.py split when the spec actually described a
    single web_app/app.py -- causing every subsequent review to fail
    against files that were never going to exist as declared.

    Two things get gathered:
      1. The current project map (directory tree + existing file structure)
         -- same map the main coding agent already relies on.
      2. Full contents of any small "spec-like" file in the project root
         (structure.txt, PROJECT_STRUCTURE.txt, plan.md, README.md, ...),
         OR any file explicitly named in the user's instruction.
    """
    parts = []

    try:
        map_data = project_map_mod.load_map(project_root)
        if not map_data:
            map_data = project_map_mod.rebuild_full_map(project_root)
        tree_text = project_map_mod.render_for_prompt(map_data)
        if tree_text:
            parts.append(tree_text)
    except Exception:
        pass

    root = Path(project_root)
    candidates: List[str] = re.findall(r"[\w./-]+\.(?:txt|md)", user_instruction, flags=re.IGNORECASE)
    try:
        for f in root.iterdir():
            if f.is_file() and f.suffix.lower() in (".txt", ".md") and SPEC_FILENAME_RE.search(f.name):
                candidates.append(f.name)
    except Exception:
        pass

    seen = set()
    spec_texts = []
    total = 0
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        p = root / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        remaining = MAX_SPEC_FILE_CHARS - total
        if remaining <= 0:
            break
        snippet = text[:remaining]
        total += len(snippet)
        spec_texts.append(f"--- {name} ---\n{snippet}")

    if spec_texts:
        parts.append(
            "SPEC/PLAN FILE CONTENTS (base your subtasks on these -- do not "
            "invent a file layout that contradicts them):\n\n" + "\n\n".join(spec_texts)
        )

    return "\n\n".join(parts)


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

    # Shared across EVERY CouncilRouter built during this whole task (planner,
    # every worker, every reviewer) so cooldown/backoff state actually
    # persists -- without this, a broken/deprecated model gets retried with
    # zero memory of its previous failure on every single call, which is why
    # the same dead-model error could repeat back to back.
    shared_routers: Dict[str, ModelRouter] = {}

    # ---------- 1. PLAN ----------
    yield {"type": "status", "text": "Planning: splitting the request into subtasks..."}
    planning_context = _gather_planning_context(project_root, user_instruction)
    planner_router = CouncilRouter(models, mode=CouncilMode.LIGHT, shared_routers=shared_routers)
    plan_user_content = user_instruction
    if planning_context:
        plan_user_content = f"{planning_context}\n\n---\n\nUser request: {user_instruction}"
    plan_messages = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": plan_user_content},
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
        st["review_infra_retries"] = 0
        st["assigned_model"] = None
        st["failed_models"] = set()
        st["made_any_change"] = False

    yield {"type": "plan_ready", "subtasks": [
        {"id": st["id"], "description": st["description"], "target_files": st["target_files"]}
        for st in subtasks
    ]}

    waves = _group_into_waves(subtasks)
    yield {"type": "status", "text": f"Scheduled into {len(waves)} wave(s) of parallel work."}

    tools = ProjectTools(project_root)  # only used for review's read_file calls

    def _run_subtask(st: dict, model: ModelConfig, event_queue: "queue.Queue"):
        st["assigned_model"] = model.name
        st["made_any_change"] = False
        event_queue.put({"type": "subtask_assigned", "subtask_id": st["id"], "model": model.name})
        # Assigned model tried first; the rest of the pool is fallback --
        # so a subtask doesn't die just because its assigned model happens
        # to be rate-limited or briefly down.
        fallback = [m for m in models if m.name != model.name]
        subtask_router = CouncilRouter([model] + fallback, mode=CouncilMode.LIGHT, shared_routers=shared_routers)
        subtask_prompt = (
            f"You are one of several AI workers collaborating on a larger project. "
            f"The overall user request was:\n\"{user_instruction}\"\n\n"
            f"Your specific piece of this project is:\n{st['description']}\n\n"
            f"Focus ONLY on this piece -- don't redo work outside your scope. "
            f"Expected files you'll create/modify: {st['target_files']}. "
            f"Call task_complete when this piece is done."
        )
        summary = None
        MUTATING = {"write_file", "edit_file_lines", "edit_symbol", "generate_document"}
        try:
            for ev in run_agent_task(
                subtask_router, project_root, subtask_prompt,
                max_iterations=MAX_SUBTASK_ITERATIONS,
                confirm_write=confirm_write, confirm_command=confirm_command,
                stop_check=stop_check, project_memory=project_memory,
            ):
                event_queue.put({"type": "subtask_event", "subtask_id": st["id"], "event": ev})
                if ev["type"] == "tool_result" and ev.get("name") in MUTATING:
                    result_text = str(ev.get("result", ""))
                    if not result_text.startswith(("Error", "User declined")):
                        st["made_any_change"] = True
                if ev["type"] == "done":
                    summary = ev.get("summary") or "(completed without an explicit summary)"
                elif ev["type"] == "error":
                    summary = f"(error: {ev['text']})"
                elif ev["type"] == "stopped":
                    summary = "(stopped by user)"
        except Exception as e:
            summary = f"(unexpected error: {e})"
        event_queue.put({"type": "subtask_done", "subtask_id": st["id"], "summary": summary})

    def _review_subtask(st: dict) -> tuple:
        """Returns (passed, reason) where passed is True/False/None.
        None means the review itself could not be run (reviewer models all
        failed) -- this is NOT the same as "the work is wrong" and must be
        handled differently by the caller (retry the review, don't punish
        the worker for infrastructure flakiness)."""
        file_contents = []
        for path in st["target_files"]:
            content = tools.read_file(path)
            file_contents.append(f"--- {path} ---\n{content}")

        # Path-aware review: a worker may reasonably place a file at a
        # slightly different (often more correct) path than the plan
        # originally declared -- e.g. the plan said "api.py" but the
        # project already has a web_app/ folder, so the worker correctly
        # wrote web_app/api.py instead. Without seeing the CURRENT tree,
        # the reviewer has no way to know that and will wrongly report
        # "api.py does not exist" even when equivalent, correct work exists
        # right there under a different path.
        try:
            map_data = project_map_mod.load_map(project_root)
            tree_text = project_map_mod.render_for_prompt(map_data) if map_data else ""
        except Exception:
            tree_text = ""

        reviewer_pool = [m for m in models if m.name != st["assigned_model"]] or models
        reviewer_router = CouncilRouter(reviewer_pool, mode=CouncilMode.LIGHT, shared_routers=shared_routers)
        review_prompt = (
            f"A subtask was assigned to an AI worker:\n{st['description']}\n\n"
            f"Acceptance criteria: {st['acceptance_criteria']}\n\n"
            f"Originally planned target files and their current contents "
            f"(a file showing an error may just mean the worker reasonably "
            f"placed it at a different path -- check the CURRENT PROJECT "
            f"TREE below before concluding something is missing):\n"
            + "\n\n".join(file_contents)
            + (f"\n\nCURRENT PROJECT TREE:\n{tree_text}" if tree_text else "")
            + "\n\nWas this subtask completed correctly and completely (checking "
              "the actual current tree for equivalently-placed files, not just "
              "the exact originally-planned paths)? Respond with ONLY strict JSON: "
              '{"passed": true or false, "reason": "short explanation"}'
        )
        try:
            result = reviewer_router.call([{"role": "user", "content": review_prompt}], tools=[])
        except AllModelsFailedError as e:
            return None, f"Could not run review (reviewer models unavailable): {e}"
        parsed = _parse_json_response(result.response.get("content", ""))
        if not parsed:
            return None, "Reviewer didn't return valid JSON -- treating as inconclusive."
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

                # If the worker never actually managed to change anything
                # (bad API key, immediate rate limit, total failure before a
                # single successful write) there's nothing to review --
                # reassign directly instead of spending a review call
                # confirming what we already know.
                if not st["made_any_change"]:
                    yield {
                        "type": "review_result", "subtask_id": st["id"],
                        "passed": False, "reason": "Worker made no successful changes -- skipping review, reassigning directly.",
                    }
                    st["failed_models"].add(st["assigned_model"])
                    st["retries"] += 1
                    if st["retries"] <= MAX_RETRIES_PER_SUBTASK:
                        st["status"] = "retrying"
                        still_pending.append(st)
                        yield {"type": "subtask_reassigned", "subtask_id": st["id"], "attempt": st["retries"] + 1}
                    else:
                        st["status"] = "needs_manual_review"
                    continue

                yield {"type": "status", "text": f"Reviewing subtask {st['id']}..."}
                passed, reason = _review_subtask(st)
                yield {"type": "review_result", "subtask_id": st["id"], "passed": passed, "reason": reason}

                if passed is None:
                    # Review INFRASTRUCTURE failed (reviewer models down) --
                    # this says nothing about whether the work is actually
                    # correct. Retry the review itself (not the worker) up
                    # to a separate small budget; if reviewing genuinely
                    # never becomes possible, accept the work rather than
                    # discard potentially-correct output over an unrelated
                    # outage.
                    #
                    # KNOWN LIMITATION: if the review stays inconclusive
                    # across an entire outer wave-retry cycle, this subtask
                    # can get re-run by _run_subtask on the next attempt
                    # even though its existing output might already be
                    # correct -- the wave loop doesn't currently have a
                    # separate "review-only, don't re-do the work" queue.
                    # This wastes a redundant worker call in that rare
                    # double-fault case, but critically it never mislabels
                    # the work as wrong or discards it -- strictly better
                    # than treating "couldn't verify" as "failed".
                    st["review_infra_retries"] += 1
                    if st["review_infra_retries"] <= MAX_REVIEW_INFRA_RETRIES:
                        still_pending.append(st)  # NOTE: re-added without incrementing retries/failed_models/reassigning the worker
                        yield {
                            "type": "status",
                            "text": f"Subtask #{st['id']}: review couldn't run, will retry the review "
                                    f"(not the work) shortly.",
                        }
                        # Keep the SAME assigned_model/output -- only the review re-runs.
                        # Re-run review immediately rather than going through
                        # another full worker wave.
                        passed2, reason2 = _review_subtask(st)
                        yield {"type": "review_result", "subtask_id": st["id"], "passed": passed2, "reason": reason2}
                        if passed2 is True:
                            st["status"] = "passed"
                            still_pending.remove(st)
                        elif passed2 is False:
                            still_pending.remove(st)
                            st["failed_models"].add(st["assigned_model"])
                            st["retries"] += 1
                            if st["retries"] <= MAX_RETRIES_PER_SUBTASK:
                                st["status"] = "retrying"
                                still_pending.append(st)
                                yield {"type": "subtask_reassigned", "subtask_id": st["id"], "attempt": st["retries"] + 1}
                            else:
                                st["status"] = "needs_manual_review"
                        # else still None: leave in still_pending, will be
                        # picked up as a fresh retry attempt below (counted
                        # against the outer wave's attempt loop, not against
                        # this subtask's own retry budget).
                    else:
                        # Repeatedly couldn't verify -- accept the work
                        # rather than punish it for review infrastructure
                        # that never recovered.
                        st["status"] = "unverified_assumed_pass"
                        yield {
                            "type": "status",
                            "text": f"Subtask #{st['id']}: review infrastructure never recovered -- "
                                    f"accepting the work unverified rather than discarding it.",
                        }
                    continue

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