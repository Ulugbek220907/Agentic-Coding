"""
The agentic loop: given a user instruction, repeatedly calls the model
(via the ModelRouter, which handles automatic failover), lets it inspect
and edit the project folder through tools, and stops when the model calls
task_complete or a safety limit is hit.

This is a generator: it `yield`s event dicts as it goes, so a UI can stream
progress instead of blocking until the whole task is done.

Event types yielded:
  {"type": "status", "text": ...}
  {"type": "model_switch", "model": ...}
  {"type": "model_failed", "model": ..., "error": ...}
  {"type": "assistant_text", "text": ...}
  {"type": "tool_call", "name": ..., "arguments": ...}
  {"type": "tool_result", "name": ..., "result": ...}
  {"type": "done", "summary": ... | None}
  {"type": "error", "text": ...}
  {"type": "stopped", "text": ...}
"""
from __future__ import annotations

import json
from typing import Generator, Optional, Callable, List

from .failover import ModelRouter, AllModelsFailedError
from .council import CouncilRouter
from .tools import ProjectTools, AGENT_TOOLS
from .skills import SkillManager
from . import project_map

BASE_SYSTEM_PROMPT = """You are a coding agent with direct access to a local project folder.

You can call these tools to explore and modify the project:
- list_dir(path): see what's in a folder
- read_file(path): read a file's FULL contents -- prefer read_symbol for existing Python files
- read_symbol(path, symbol_name): read just one class/function/method from a Python file (PREFERRED for existing files)
- edit_symbol(path, symbol_name, new_code): replace just one class/function/method (PREFERRED way to edit existing Python files)
- read_file_range(path, start_line, end_line): read just some lines of any file
- edit_file_lines(path, start_line, end_line, new_content): replace just some lines of any file
- write_file(path, content): create or overwrite a TEXT file (code, markdown, config, etc.) -- fine for new files, costly for editing large existing ones
- generate_document(path, doc_type, title, sections): create a REAL .docx, .pdf, or
  .xlsx file. Use this instead of write_file whenever the user asks for a Word
  document, PDF, report, or spreadsheet -- never fake a binary format as a text file.
- run_command(command): run a shell command in the project root (e.g. tests, installs)
- task_complete(summary): call this ONLY when the user's request is fully done

All paths are relative to the project root. Use "." for the root itself,
and things like "src/main.py" for nested files. Never use "/", "\\", or an
absolute path like "C:\\..." -- those are outside the sandbox and will be
refused.

If a PROJECT MAP is included below, TRUST IT for orientation -- it already
lists every existing file's classes and functions, kept automatically in
sync with the real files. Do NOT spend steps on list_dir/read_file just to
rediscover what already exists; the map already tells you. Only read_file
a specific file when you're about to edit it and need its exact current
contents, or when the map shows a parse error for it. This matters a lot
on multi-step tasks and "continue" requests -- re-exploring a codebase you
already mapped wastes most of your step budget before any new work
happens.

Work step by step: inspect relevant files before editing them, make focused
changes, and verify your work (e.g. by re-reading a file or running tests)
before declaring the task complete. Always finish by calling task_complete
with a short summary of what you changed.

Output quality rules for anything you say directly to the user (not file
contents):
- Any code you show in chat MUST be in a fenced code block with a language
  tag, e.g. ```python ... ``` -- this is what makes it render as a proper,
  copyable code box instead of a wall of plain text.
- Be concrete and specific. Prefer short, direct sentences over hedging or
  restating the question back at the user.
- Use markdown structure (headings, bullet lists, bold) when it genuinely
  clarifies something with real structure (steps, comparisons, multiple
  options) -- not for simple one-line answers, which should just be prose.
- Don't pad responses with throat-clearing ("Great question!", "Certainly!
  Here's...") or a restated summary of what you're about to do right before
  you do it.
- If the user asks for a document, report, spreadsheet, or presentation,
  actually generate the file with generate_document/write_file rather than
  pasting its contents into the chat -- chat text is for explanation, files
  are for deliverables.
"""


def build_system_prompt(
    project_memory: Optional[List[str]] = None,
    skill_manager: Optional[SkillManager] = None,
    project_root: Optional[str] = None,
) -> str:
    prompt = BASE_SYSTEM_PROMPT
    if project_root:
        map_data = project_map.load_map(project_root)
        map_text = project_map.render_for_prompt(map_data)
        if map_text:
            prompt += "\n\n" + map_text
    if project_memory:
        bullet_list = "\n".join(f"- {entry}" for entry in project_memory)
        prompt += (
            "\n\nHere is a short summary of what's already happened in this "
            "project across earlier requests (most recent last). Use this as "
            "context -- the user should not have to repeat it:\n" + bullet_list
        )
    if skill_manager:
        fragment = skill_manager.system_prompt_fragment()
        if fragment:
            prompt += "\n\n" + fragment
    return prompt


def run_agent_task(
    router: CouncilRouter,
    project_root: str,
    user_instruction: str,
    max_iterations: int = 60,
    confirm_write=None,
    confirm_command=None,
    stop_check: Optional[Callable[[], bool]] = None,
    project_memory: Optional[List[str]] = None,
) -> Generator[dict, None, None]:
    """
    max_iterations: 0 means no fixed step ceiling -- the loop instead relies
                    on stall detection (see below) plus the Stop button as
                    the actual safety nets. A flat step count doesn't scale
                    well once tools got more surgical (read_symbol/edit_symbol
                    do less per call than a full read_file/write_file did,
                    so the same amount of real work now takes more steps) --
                    better to let it run until genuinely done or genuinely
                    stuck, rather than cutting off legitimate progress at an
                    arbitrary number.
    stop_check: optional callable returning True if the user hit Stop.
                Checked between steps so a running task can be cancelled
                without killing the whole app.
    project_memory: optional list of short summaries of earlier work in this
                     project (see core/memory.py), folded into the system
                     prompt for continuity without replaying full history.
    """
    stop_check = stop_check or (lambda: False)
    tools = ProjectTools(project_root, confirm_write=confirm_write, confirm_command=confirm_command, stop_check=stop_check)
    skill_manager = SkillManager()
    all_tools = AGENT_TOOLS + skill_manager.tool_schemas()

    history = [
        {"role": "system", "content": build_system_prompt(project_memory, skill_manager, project_root)},
        {"role": "user", "content": user_instruction},
    ]

    def on_router_event(name, data):
        # Bridge ModelRouter's callback style into our generator's event stream.
        # (Captured/relayed via the events list below since generators can't
        # yield from inside a plain callback.)
        pending_events.append({"type": name, **data})

    pending_events = []
    router.on_event = on_router_event

    # ---------- stall detection state ----------
    # Real safety net instead of (or alongside) a flat step count: if the
    # model repeats the exact same tool call several times in a row, or
    # goes a long stretch of steps without any tool call actually changing
    # the project, that's a much better signal that something's wrong than
    # "we hit step N".
    MUTATING_TOOLS = {"write_file", "edit_file_lines", "edit_symbol", "generate_document", "run_command"}
    REPEAT_STALL_THRESHOLD = 3     # same exact (tool, args) this many times in a row -> stop
    NO_PROGRESS_STALL_THRESHOLD = 20  # this many steps with no successful mutating call -> stop
    recent_call_signatures: List[tuple] = []
    steps_since_progress = 0

    i = 0
    while True:
        if max_iterations and i >= max_iterations:
            yield {"type": "error", "text": f"Stopped after reaching the max iteration limit ({max_iterations})."}
            return
        i += 1

        if stop_check():
            yield {"type": "stopped", "text": "Stopped by user."}
            return

        step_label = f"step {i}" if not max_iterations else f"step {i}/{max_iterations}"
        yield {"type": "status", "text": f"Thinking ({step_label})..."}

        try:
            result = router.call(history, all_tools)
        except AllModelsFailedError as e:
            yield {"type": "error", "text": str(e)}
            return

        # Flush any model_switch / model_failed / council_* events captured
        # during this call (per-participant failover events, plus
        # council_vote / council_disagreement / council_synthesized / etc.
        # bridged the same way from CouncilRouter.on_event).
        for ev in pending_events:
            yield ev
        pending_events.clear()

        if len(result.contributors) > 1:
            yield {
                "type": "council_step",
                "contributors": result.contributors,
                "winning_models": result.winning_models,
                "agreement": result.agreement,
                "synthesized": result.synthesized,
                "failed_models": result.failed_models,
            }

        response = result.response
        assistant_msg = {
            "role": "assistant",
            "content": response.get("content"),
            "tool_calls": response.get("tool_calls") or [],
        }
        history.append(assistant_msg)

        if response.get("content"):
            yield {
                "type": "assistant_text",
                "text": response["content"],
                "model": ", ".join(result.winning_models),
            }

        tool_calls = response.get("tool_calls") or []
        if not tool_calls:
            # Plain conversational reply, no tool call -- we already yielded
            # its text as assistant_text above. Don't repeat the same text
            # again as a "done" summary, that's just a duplicate on screen.
            yield {"type": "done", "summary": None}
            return

        task_finished = False
        finish_summary = ""

        for tc in tool_calls:
            if stop_check():
                yield {"type": "stopped", "text": "Stopped by user."}
                return

            name = tc["name"]
            args = tc.get("arguments", {})
            yield {"type": "tool_call", "name": name, "arguments": args}

            try:
                if name == "task_complete":
                    task_finished = True
                    finish_summary = args.get("summary", "Task complete.")
                    tool_output = "Task marked complete."
                elif name == "list_dir":
                    tool_output = tools.list_dir(args.get("path", "."))
                elif name == "read_file":
                    tool_output = tools.read_file(args.get("path", ""))
                elif name == "read_file_range":
                    tool_output = tools.read_file_range(args.get("path", ""), args.get("start_line", 1), args.get("end_line", 1))
                elif name == "edit_file_lines":
                    tool_output = tools.edit_file_lines(
                        args.get("path", ""), args.get("start_line", 1), args.get("end_line", 1), args.get("new_content", "")
                    )
                elif name == "read_symbol":
                    tool_output = tools.read_symbol(args.get("path", ""), args.get("symbol_name", ""))
                elif name == "edit_symbol":
                    tool_output = tools.edit_symbol(args.get("path", ""), args.get("symbol_name", ""), args.get("new_code", ""))
                elif name == "write_file":
                    tool_output = tools.write_file(args.get("path", ""), args.get("content", ""))
                elif name == "generate_document":
                    tool_output = tools.generate_document(
                        args.get("path", ""),
                        args.get("doc_type", ""),
                        args.get("title", ""),
                        args.get("sections", []),
                    )
                elif skill_manager.is_skill_tool(name):
                    tool_output = skill_manager.call(name, args, project_root)
                elif name == "run_command":
                    tool_output = tools.run_command(args.get("command", ""))
                else:
                    tool_output = f"Unknown tool: {name}"
            except Exception as e:
                # ANY tool failure (bad path, permission error, whatever) becomes
                # feedback the model can see and react to -- it must never kill
                # the whole run.
                tool_output = f"Error executing tool '{name}': {e}"

            yield {"type": "tool_result", "name": name, "result": tool_output}
            history.append({
                "role": "tool_result",
                "tool_call_id": tc.get("id", ""),
                "name": name,
                "content": tool_output,
            })

            # ---------- stall detection ----------
            sig = (name, json.dumps(args, sort_keys=True, default=str))
            recent_call_signatures.append(sig)
            recent_call_signatures[:] = recent_call_signatures[-REPEAT_STALL_THRESHOLD:]
            if len(recent_call_signatures) == REPEAT_STALL_THRESHOLD and len(set(recent_call_signatures)) == 1:
                yield {
                    "type": "error",
                    "text": f"Stopped: the same tool call ({name}) with identical arguments repeated "
                            f"{REPEAT_STALL_THRESHOLD} times in a row -- this looks like a stuck loop "
                            f"rather than progress, not a step-count limit.",
                }
                return

            made_progress = name in MUTATING_TOOLS and not str(tool_output).startswith(("Error", "User declined"))
            if made_progress:
                steps_since_progress = 0
            else:
                steps_since_progress += 1
            if steps_since_progress >= NO_PROGRESS_STALL_THRESHOLD:
                yield {
                    "type": "error",
                    "text": f"Stopped: {NO_PROGRESS_STALL_THRESHOLD} steps with no successful file/command "
                            f"change -- this looks stuck rather than making progress. If the task is "
                            f"genuinely this exploration-heavy, consider breaking it into smaller requests.",
                }
                return

        if task_finished:
            yield {"type": "done", "summary": finish_summary}
            return