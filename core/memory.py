"""
Lightweight, token-cheap "project memory".

Replaying the ENTIRE raw conversation (every tool call, every file dump)
on every single message would work, but gets expensive fast -- a few
file writes and read_file calls and you're resending tens of thousands of
tokens just for the model to remember "we already made calculator.py".

Instead: every time a task finishes (or errors/gets stopped), we distill
it into ONE short line and append it to a small per-project memory file.
That compact list gets folded into the system prompt on every future
request, so the model has continuity -- "we already built X, now add Y"
-- without you re-explaining, and without paying full-transcript prices
every turn.

Stored inside the project folder itself (`.ai_agent_memory.json`), so it
follows the project even across app restarts. Delete that file any time
to reset the agent's memory of a project.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

MEMORY_FILENAME = ".ai_agent_memory.json"
MAX_ENTRIES = 40          # keep at most this many summaries
MAX_TOTAL_CHARS = 6000    # ...and this many characters total, whichever is smaller


def _memory_path(project_root: str) -> Path:
    return Path(project_root) / MEMORY_FILENAME


def load_memory(project_root: str) -> List[str]:
    path = _memory_path(project_root)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    return []


def _trim(entries: List[str]) -> List[str]:
    entries = entries[-MAX_ENTRIES:]
    while entries and sum(len(e) for e in entries) > MAX_TOTAL_CHARS:
        entries.pop(0)
    return entries


def save_memory(project_root: str, entries: List[str]) -> None:
    path = _memory_path(project_root)
    try:
        path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # memory is a nice-to-have; never let it crash the app


def append_memory(project_root: str, entry: str) -> List[str]:
    entries = load_memory(project_root)
    entries.append(entry)
    entries = _trim(entries)
    save_memory(project_root, entries)
    return entries
