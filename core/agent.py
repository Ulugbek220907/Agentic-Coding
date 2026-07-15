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

from typing import Generator, Optional, Callable, List

from .failover import ModelRouter, AllModelsFailedError
from .tools import ProjectTools, AGENT_TOOLS

BASE_SYSTEM_PROMPT = """You are a coding agent with direct access to a local project folder.

You can call these tools to explore and modify the project:
- list_dir(path): see what's in a folder
- read_file(path): read a file's contents
- write_file(path, content): create or overwrite a file
- run_command(command): run a shell command in the project root (e.g. tests, installs)
- task_complete(summary): call this ONLY when the user's request is fully done

All paths are relative to the project root. Use "." for the root itself,
and things like "src/main.py" for nested files. Never use "/", "\\", or an
absolute path like "C:\\..." -- those are outside the sandbox and will be
refused.

Work step by step: inspect relevant files before editing them, make focused
changes, and verify your work (e.g. by re-reading a file or running tests)
before declaring the task complete. Always finish by calling task_complete
with a short summary of what you changed.
"""


def build_system_prompt(project_memory: Optional[List[str]] = None) -> str:
    prompt = BASE_SYSTEM_PROMPT
    if project_memory:
        bullet_list = "\n".join(f"- {entry}" for entry in project_memory)
        prompt += (
            "\n\nHere is a short summary of what's already happened in this "
            "project across earlier requests (most recent last). Use this as "
            "context -- the user should not have to repeat it:\n" + bullet_list
        )
    return prompt


def run_agent_task(
    router: ModelRouter,
    project_root: str,
    user_instruction: str,
    max_iterations: int = 25,
    confirm_write=None,
    confirm_command=None,
    stop_check: Optional[Callable[[], bool]] = None,
    project_memory: Optional[List[str]] = None,
) -> Generator[dict, None, None]:
    """
    stop_check: optional callable returning True if the user hit Stop.
                Checked between steps so a running task can be cancelled
                without killing the whole app.
    project_memory: optional list of short summaries of earlier work in this
                     project (see core/memory.py), folded into the system
                     prompt for continuity without replaying full history.
    """
    tools = ProjectTools(project_root, confirm_write=confirm_write, confirm_command=confirm_command)
    stop_check = stop_check or (lambda: False)

    history = [
        {"role": "system", "content": build_system_prompt(project_memory)},
        {"role": "user", "content": user_instruction},
    ]

    def on_router_event(name, data):
        # Bridge ModelRouter's callback style into our generator's event stream.
        # (Captured/relayed via the events list below since generators can't
        # yield from inside a plain callback.)
        pending_events.append({"type": name, **data})

    pending_events = []
    router.on_event = on_router_event

    for i in range(max_iterations):
        if stop_check():
            yield {"type": "stopped", "text": "Stopped by user."}
            return

        yield {"type": "status", "text": f"Thinking (step {i + 1}/{max_iterations})..."}

        try:
            result = router.call(history, AGENT_TOOLS)
        except AllModelsFailedError as e:
            yield {"type": "error", "text": str(e)}
            return

        # Flush any model_switch / model_failed events captured during this call.
        for ev in pending_events:
            yield ev
        pending_events.clear()

        response = result.response
        assistant_msg = {
            "role": "assistant",
            "content": response.get("content"),
            "tool_calls": response.get("tool_calls") or [],
        }
        history.append(assistant_msg)

        if response.get("content"):
            yield {"type": "assistant_text", "text": response["content"], "model": result.model_used.name}

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
                elif name == "write_file":
                    tool_output = tools.write_file(args.get("path", ""), args.get("content", ""))
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

        if task_finished:
            yield {"type": "done", "summary": finish_summary}
            return

    yield {"type": "error", "text": f"Stopped after reaching the max iteration limit ({max_iterations})."}
