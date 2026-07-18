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
from typing import Optional, List

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


DIR_KEY = "_directories"  # top-level key in the map JSON holding the full folder tree


def _ensure_dirs_tracked(map_data: dict, rel_path: str):
    """Register every parent directory of rel_path in the map's directory
    list. Called after any write so newly created folders show up in the
    tree immediately, not just after the next full rebuild."""
    dirs = set(map_data.get(DIR_KEY, []))
    parts = Path(rel_path).parts[:-1]  # drop the filename itself
    for i in range(1, len(parts) + 1):
        dirs.add(str(Path(*parts[:i])))
    map_data[DIR_KEY] = sorted(dirs)


def update_entry(project_root: str, rel_path: str):
    """Recompute the map entry for ONE file -- call this right after
    write_file succeeds. Cheap enough to do on every write with no
    noticeable delay."""
    map_data = load_map(project_root)
    map_data[rel_path] = describe_file(project_root, rel_path)
    _ensure_dirs_tracked(map_data, rel_path)
    save_map(project_root, map_data)


def remove_entry(project_root: str, rel_path: str):
    map_data = load_map(project_root)
    if rel_path in map_data:
        del map_data[rel_path]
        save_map(project_root, map_data)


def rebuild_full_map(project_root: str) -> dict:
    """Walk the whole project and (re)build the map from scratch, indexing
    BOTH files (with their parsed structure) AND the full directory tree --
    including directories that are currently EMPTY. Without empty
    directories being tracked, the model has no way to know "this folder
    exists and has nothing in it yet" except by calling list_dir on it one
    folder at a time, which is exactly the slow one-by-one exploration this
    map exists to prevent. Called once when a project folder is opened, so
    even a project the agent didn't build in this app still gets indexed
    instead of starting blank."""
    root = Path(project_root)
    map_data: dict = {}
    dirs = set()
    count = 0
    for f in sorted(root.rglob("*")):
        rel_parts = f.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        if f.is_dir():
            dirs.add(str(f.relative_to(root)))
            continue
        if not f.is_file():
            continue
        if f.name == MAP_FILENAME or f.name.startswith(".ai_"):
            continue
        for i in range(1, len(rel_parts)):
            dirs.add(str(Path(*rel_parts[:i])))
        if count >= MAX_FILES:
            continue
        rel = str(f.relative_to(root))
        map_data[rel] = describe_file(project_root, rel)
        count += 1
    map_data[DIR_KEY] = sorted(dirs)
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


def _build_tree_lines(dirs: List[str], file_paths: List[str], max_lines: int = 300) -> List[str]:
    """
    Render a compact indented tree combining ALL directories (including
    currently-empty ones) and files -- similar to what a `tree` command or
    a PROJECT_STRUCTURE.txt would show. This is what lets the model see the
    ENTIRE project skeleton (populated or not) in one shot from the system
    prompt, instead of calling list_dir on every single folder one at a
    time to discover "yep, this one's empty too."
    """
    root: dict = {}
    for d in dirs:
        node = root
        for part in Path(d).parts:
            node = node.setdefault(part, {})
    for f in file_paths:
        parts = Path(f).parts
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault("__files__", []).append(parts[-1])

    lines: List[str] = []

    def _walk(node: dict, prefix: str):
        if len(lines) >= max_lines:
            return
        subdirs = sorted(k for k in node if k != "__files__")
        files = sorted(node.get("__files__", []))
        for d in subdirs:
            if len(lines) >= max_lines:
                return
            child = node[d]
            child_has_content = bool(child)
            lines.append(f"{prefix}{d}/" + ("" if child_has_content else "  (empty)"))
            _walk(child, prefix + "  ")
        for fname in files:
            if len(lines) >= max_lines:
                return
            lines.append(f"{prefix}{fname}")

    _walk(root, "")
    if len(lines) >= max_lines:
        lines.append(f"... (tree truncated at {max_lines} lines)")
    return lines


def render_for_prompt(map_data: dict, max_entries: int = 200) -> str:
    """Compact, token-cheap text summary for the system prompt: a full
    directory tree first (so the model sees the whole skeleton, including
    empty folders, without exploring it), then per-file structure detail
    for anything that has actual code in it."""
    if not map_data:
        return ""

    file_entries = {k: v for k, v in map_data.items() if k != DIR_KEY}
    dirs = map_data.get(DIR_KEY, [])

    tree_lines = _build_tree_lines(dirs, list(file_entries.keys()))

    detail_lines = []
    for rel_path, entry in list(file_entries.items())[:max_entries]:
        if entry.get("error"):
            continue
        if not entry.get("classes") and not entry.get("functions") and not entry.get("doc"):
            continue  # nothing structural to say beyond what the tree already showed
        doc = entry.get("doc", "")
        header = f"{rel_path}" + (f" -- {doc}" if doc else "")
        detail_lines.append(header)
        for cls in entry.get("classes", []):
            base_str = f"({', '.join(cls['bases'])})" if cls.get("bases") else ""
            cls_lines = cls.get("lines")
            loc_str = f" [L{cls_lines[0]}-{cls_lines[1]}]" if cls_lines else ""
            method_str = ", ".join(
                f"{m['name']}(L{m['lines'][0]}-{m['lines'][1]})" if m.get("lines") else m["name"]
                for m in cls.get("methods", [])[:12]
            )
            doc_str = f" -- {cls['doc']}" if cls.get("doc") else ""
            detail_lines.append(f"  class {cls['name']}{base_str}{loc_str}: {method_str}{doc_str}")
        for fn in entry.get("functions", []):
            arg_str = ", ".join(fn.get("args", []))
            fn_lines = fn.get("lines")
            loc_str = f" [L{fn_lines[0]}-{fn_lines[1]}]" if fn_lines else ""
            doc_str = f" -- {fn['doc']}" if fn.get("doc") else ""
            detail_lines.append(f"  def {fn['name']}({arg_str}){loc_str}{doc_str}")

    if not tree_lines and not detail_lines:
        return ""

    parts = [
        "PROJECT MAP (kept up to date automatically -- this is the COMPLETE "
        "project skeleton, including empty folders, plus the classes/"
        "functions already written in existing files).\n\n"
        "IMPORTANT: this map already shows every folder and file that "
        "exists, including which folders are still EMPTY. Do NOT call "
        "list_dir folder-by-folder to re-discover this -- it's all here. "
        "Only call list_dir if you need to check something genuinely not "
        "reflected in this map (e.g. right after a shell command that might "
        "have created files outside this app's tracking).\n\n"
        "For any EXISTING file with structure shown below, prefer "
        "read_symbol/edit_symbol over read_file/write_file: read_file/"
        "write_file transfer the ENTIRE file every time, which is why large "
        "files blow past small free-tier request-size limits (many free "
        "models cap requests around 8000 tokens). read_symbol('path',"
        "'ClassName') or read_symbol('path','ClassName.method_name') "
        "fetches just that piece; edit_symbol replaces just that piece and "
        "leaves the rest of the file untouched.\n"
    ]
    if tree_lines:
        parts.append("--- Directory tree ---\n" + "\n".join(tree_lines))
    if detail_lines:
        parts.append("--- File contents (classes/functions already written) ---\n" + "\n".join(detail_lines))
    return "\n\n".join(parts)