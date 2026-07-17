"""
Project map: a persistent, auto-maintained index of what's already in the
project -- per file, its purpose and its top-level classes/functions --
so the agent can orient itself from the system prompt instead of spending
its entire step budget re-running list_dir/read_file on every single
message just to rediscover a codebase it already built last turn.

Design choice: this is built with STATIC PARSING (Python's ast module for
.py files), not an extra AI call. That makes it:
  - free (no tokens spent maintaining it)
  - instant (parsing is microseconds, not a network round trip)
  - always in sync (recomputed the moment a file is written, not
    "whenever the model remembers to update docs")

Stored as .ai_project_map.json in the project root, analogous to
.ai_agent_memory.json (memory.py) but indexing STRUCTURE rather than
conversation history. Same "don't put this in git" category -- add it to
.gitignore alongside the memory file.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Optional

MAP_FILENAME = ".ai_project_map.json"

# Extensions we don't try to parse structurally -- just recorded with size,
# so the map still tells the model these files exist without pretending to
# understand their internals.
CODE_EXTENSIONS = {".py"}
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".ai_agent_desktop"}
MAX_FILES = 500  # sanity cap so a huge project doesn't make map-building slow


def _map_path(project_root: str) -> Path:
    return Path(project_root) / MAP_FILENAME


def _describe_python_file(path: Path) -> dict:
    """Extract module docstring + top-level classes (with methods) and
    functions via ast -- no execution, no imports resolved, just parsing."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except Exception as e:
        return {"error": f"could not parse: {e}"}

    module_doc = ast.get_docstring(tree)
    classes = []
    functions = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = []
            for n in node.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and (not n.name.startswith("_") or n.name == "__init__"):
                    start = n.decorator_list[0].lineno if n.decorator_list else n.lineno
                    methods.append({"name": n.name, "lines": [start, n.end_lineno]})
            bases = [ast.unparse(b) if hasattr(ast, "unparse") else "" for b in node.bases]
            doc = ast.get_docstring(node)
            class_start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            classes.append({
                "name": node.name,
                "bases": bases,
                "doc": (doc or "").strip().split("\n")[0][:150],
                "methods": methods,
                "lines": [class_start, node.end_lineno],
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            doc = ast.get_docstring(node)
            try:
                args = [a.arg for a in node.args.args]
            except Exception:
                args = []
            fn_start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            functions.append({
                "name": node.name,
                "args": args,
                "doc": (doc or "").strip().split("\n")[0][:150],
                "lines": [fn_start, node.end_lineno],
            })

    return {
        "doc": (module_doc or "").strip().split("\n")[0][:150] if module_doc else "",
        "classes": classes,
        "functions": functions,
    }


def describe_file(project_root: str, rel_path: str) -> dict:
    path = Path(project_root) / rel_path
    if not path.exists() or not path.is_file():
        return {"error": "file not found"}
    entry = {"size_bytes": path.stat().st_size}
    if path.suffix in CODE_EXTENSIONS:
        entry.update(_describe_python_file(path))
    return entry


def load_map(project_root: str) -> dict:
    p = _map_path(project_root)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_map(project_root: str, map_data: dict):
    _map_path(project_root).write_text(json.dumps(map_data, indent=2), encoding="utf-8")


def update_entry(project_root: str, rel_path: str):
    """Recompute the map entry for ONE file -- call this right after
    write_file succeeds. Cheap enough to do on every write with no
    noticeable delay."""
    map_data = load_map(project_root)
    map_data[rel_path] = describe_file(project_root, rel_path)
    save_map(project_root, map_data)


def remove_entry(project_root: str, rel_path: str):
    map_data = load_map(project_root)
    if rel_path in map_data:
        del map_data[rel_path]
        save_map(project_root, map_data)


def rebuild_full_map(project_root: str) -> dict:
    """Walk the whole project and (re)build the map from scratch. Called
    once when a project folder is opened, so even a project the agent
    didn't build in this app still gets indexed instead of starting blank."""
    root = Path(project_root)
    map_data = {}
    count = 0
    for f in sorted(root.rglob("*")):
        if count >= MAX_FILES:
            break
        if not f.is_file():
            continue
        if any(part in SKIP_DIRS for part in f.relative_to(root).parts):
            continue
        if f.name == MAP_FILENAME or f.name.startswith(".ai_"):
            continue
        rel = str(f.relative_to(root))
        map_data[rel] = describe_file(project_root, rel)
        count += 1
    save_map(project_root, map_data)
    return map_data


def resolve_symbol(source: str, symbol_name: str) -> Optional[dict]:
    """
    Find a symbol's CURRENT location by parsing the given source fresh --
    deliberately not using any cached/stored line numbers, since those
    drift the instant an earlier edit adds or removes a line. This is what
    makes read_symbol/edit_symbol safe to use repeatedly on the same file
    without ever going stale.

    symbol_name: "function_name", "ClassName", or "ClassName.method_name".
    Returns {"start_line": int, "end_line": int, "kind": "class"|"function"|"method"} or None.
    """
    try:
        tree = ast.parse(source)
    except Exception:
        return None

    if "." in symbol_name:
        class_name, method_name = symbol_name.split(".", 1)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for n in node.body:
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method_name:
                        start = n.decorator_list[0].lineno if n.decorator_list else n.lineno
                        return {"start_line": start, "end_line": n.end_lineno, "kind": "method"}
        return None

    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == symbol_name:
            start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            return {"start_line": start, "end_line": node.end_lineno, "kind": "class"}
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol_name:
            start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            return {"start_line": start, "end_line": node.end_lineno, "kind": "function"}
    return None


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def render_for_prompt(map_data: dict, max_entries: int = 200) -> str:
    """Compact, token-cheap text summary for the system prompt."""
    if not map_data:
        return ""
    lines = []
    for rel_path, entry in list(map_data.items())[:max_entries]:
        if entry.get("error"):
            lines.append(f"{rel_path} ({entry['error']})")
            continue
        doc = entry.get("doc", "")
        header = f"{rel_path}" + (f" -- {doc}" if doc else "")
        lines.append(header)
        for cls in entry.get("classes", []):
            base_str = f"({', '.join(cls['bases'])})" if cls.get("bases") else ""
            cls_lines = cls.get("lines")
            loc_str = f" [L{cls_lines[0]}-{cls_lines[1]}]" if cls_lines else ""
            method_str = ", ".join(
                f"{m['name']}(L{m['lines'][0]}-{m['lines'][1]})" if m.get("lines") else m["name"]
                for m in cls.get("methods", [])[:12]
            )
            doc_str = f" -- {cls['doc']}" if cls.get("doc") else ""
            lines.append(f"  class {cls['name']}{base_str}{loc_str}: {method_str}{doc_str}")
        for fn in entry.get("functions", []):
            arg_str = ", ".join(fn.get("args", []))
            fn_lines = fn.get("lines")
            loc_str = f" [L{fn_lines[0]}-{fn_lines[1]}]" if fn_lines else ""
            doc_str = f" -- {fn['doc']}" if fn.get("doc") else ""
            lines.append(f"  def {fn['name']}({arg_str}){loc_str}{doc_str}")
    if not lines:
        return ""
    return (
        "PROJECT MAP (existing files, classes, functions, and their line "
        "ranges -- kept up to date automatically). You do NOT need to "
        "list_dir or read_file just to see what already exists.\n\n"
        "IMPORTANT -- for any EXISTING file shown here, prefer read_symbol/"
        "edit_symbol over read_file/write_file: read_file/write_file "
        "transfer the ENTIRE file every time, which is why large files blow "
        "past small free-tier request-size limits (many free models cap "
        "requests around 8000 tokens). read_symbol('path','ClassName') or "
        "read_symbol('path','ClassName.method_name') fetches just that "
        "piece; edit_symbol replaces just that piece and leaves the rest of "
        "the file untouched -- both cost roughly what the piece is, not "
        "what the whole file is. Only use read_file/write_file for files "
        "that don't exist yet, or files small enough that it doesn't matter.\n\n"
        + "\n".join(lines)
    )