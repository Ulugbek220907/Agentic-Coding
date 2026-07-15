"""
Tools the agent is allowed to call against the local project folder.
Everything is sandboxed to `project_root` -- path traversal outside of it
is rejected.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


class ToolError(Exception):
    pass


class ProjectTools:
    def __init__(self, project_root: str, confirm_write=None, confirm_command=None):
        """
        confirm_write: optional callable(path, new_content) -> bool, asks the
                       user before writing a file (wire this to a UI dialog).
        confirm_command: optional callable(command) -> bool, asks the user
                          before running a shell command.
        """
        self.root = Path(project_root).resolve()
        self.confirm_write = confirm_write or (lambda *a: True)
        self.confirm_command = confirm_command or (lambda *a: True)

    def _resolve(self, rel_path: str) -> Path:
        p = (self.root / rel_path).resolve()
        if self.root not in p.parents and p != self.root:
            raise ToolError(f"Path '{rel_path}' escapes the project folder -- refused.")
        return p

    def list_dir(self, path: str = ".") -> str:
        target = self._resolve(path)
        if not target.exists():
            return f"Error: '{path}' does not exist."
        entries = []
        for child in sorted(target.iterdir()):
            marker = "/" if child.is_dir() else ""
            entries.append(f"{child.name}{marker}")
        return "\n".join(entries) if entries else "(empty directory)"

    def read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.exists():
            return f"Error: '{path}' does not exist."
        try:
            # Always read as UTF-8. errors="replace" means a stray bad byte
            # becomes a placeholder character instead of crashing the read.
            return target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error reading '{path}': {e}"

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        if not self.confirm_write(str(target), content):
            return f"User declined the write to '{path}'."
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Explicit UTF-8 -- Windows' default encoding (cp1252) can't
            # represent emoji or many other Unicode characters models like
            # to use, and would otherwise crash mid-write leaving a partial
            # or empty file behind.
            target.write_text(content, encoding="utf-8")
        except Exception as e:
            # Return the error as tool output instead of letting it kill the
            # whole agent run -- the model gets to see what went wrong and
            # can retry (e.g. without the character that caused it).
            return f"Error writing '{path}': {e}"
        return f"Wrote {len(content)} characters to '{path}'."

    def run_command(self, command: str, timeout: int = 30) -> str:
        if not self.confirm_command(command):
            return f"User declined running command: {command}"
        try:
            result = subprocess.run(
                command, shell=True, cwd=self.root,
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
            out = result.stdout[-4000:]
            err = result.stderr[-2000:]
            return f"exit_code={result.returncode}\nstdout:\n{out}\nstderr:\n{err}"
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout}s: {command}"
        except Exception as e:
            return f"Error running command '{command}': {e}"


# Tool schemas exposed to the model (JSON Schema, OpenAI-function-call style;
# providers.py adapts this to Anthropic's input_schema format too).
AGENT_TOOLS = [
    {
        "name": "list_dir",
        "description": "List files and folders at a path relative to the project root.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative path, default '.'"}},
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": "Read the full text content of a file relative to the project root.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file relative to the project root with the given content.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the project root (e.g. run tests, install packages). Use sparingly.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "task_complete",
        "description": "Call this when the requested task is fully done. Provide a short summary of what changed.",
        "parameters": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]
