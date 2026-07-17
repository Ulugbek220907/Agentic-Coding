"""
Tools the agent is allowed to call against the local project folder.
Everything is sandboxed to `project_root` -- path traversal outside of it
is rejected.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Optional

from . import project_map


_UNICODE_FONT_NAME = None  # cached after first successful registration


def _register_unicode_font() -> str:
    """
    Register a font with full Cyrillic (and general Unicode) coverage for
    ReportLab, and return its registered name. ReportLab's built-in base14
    fonts (Helvetica, Times-Roman, etc.) have NO Cyrillic glyphs at all --
    that's why Russian/etc. text rendered as black boxes before. We ship
    DejaVu Sans with the app specifically so this works regardless of what
    fonts happen to be installed on the user's machine.
    """
    global _UNICODE_FONT_NAME
    if _UNICODE_FONT_NAME:
        return _UNICODE_FONT_NAME

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    bundled_regular = Path(__file__).parent.parent / "assets" / "fonts" / "DejaVuSans.ttf"
    bundled_bold = Path(__file__).parent.parent / "assets" / "fonts" / "DejaVuSans-Bold.ttf"

    candidates = [
        ("UnicodeFont", bundled_regular, "UnicodeFont-Bold", bundled_bold),
        # OS fallbacks, in case the bundled files are ever missing/removed
        ("UnicodeFont", Path(r"C:\Windows\Fonts\arial.ttf"), "UnicodeFont-Bold", Path(r"C:\Windows\Fonts\arialbd.ttf")),
        ("UnicodeFont", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
         "UnicodeFont-Bold", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
        ("UnicodeFont", Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
         "UnicodeFont-Bold", Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")),
    ]

    for reg_name, reg_path, bold_name, bold_path in candidates:
        if reg_path.exists():
            pdfmetrics.registerFont(TTFont(reg_name, str(reg_path)))
            if bold_path.exists():
                pdfmetrics.registerFont(TTFont(bold_name, str(bold_path)))
                pdfmetrics.registerFontFamily(reg_name, normal=reg_name, bold=bold_name)
            _UNICODE_FONT_NAME = reg_name
            return reg_name

    # Nothing found -- fall back to Helvetica. Non-Latin text will render as
    # boxes, but we don't want to hard-crash document generation over it.
    _UNICODE_FONT_NAME = "Helvetica"
    return _UNICODE_FONT_NAME


class ToolError(Exception):
    pass


class ProjectTools:
    def __init__(self, project_root: str, confirm_write=None, confirm_command=None, stop_check=None):
        """
        confirm_write: optional callable(path, new_content) -> bool, asks the
                       user before writing a file (wire this to a UI dialog).
        confirm_command: optional callable(command) -> bool, asks the user
                          before running a shell command.
        stop_check: optional callable() -> bool, polled while a shell command
                    is running so the Stop button can actually kill a
                    long-running process instead of only taking effect after
                    it finishes on its own.
        """
        self.root = Path(project_root).resolve()
        self.confirm_write = confirm_write or (lambda *a: True)
        self.confirm_command = confirm_command or (lambda *a: True)
        self.stop_check = stop_check or (lambda: False)

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

    def read_file_range(self, path: str, start_line: int, end_line: int) -> str:
        """Read only lines [start_line, end_line] (1-indexed, inclusive),
        each prefixed with its line number -- for pulling just the part of
        a large file you need, instead of the whole thing. Works on any
        text file (not just Python)."""
        target = self._resolve(path)
        if not target.exists():
            return f"Error: '{path}' does not exist."
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").split("\n")
        except Exception as e:
            return f"Error reading '{path}': {e}"
        start = max(1, start_line)
        end = min(len(lines), end_line)
        if start > len(lines):
            return f"Error: '{path}' only has {len(lines)} lines (requested start_line={start_line})."
        snippet = "\n".join(f"{i}: {lines[i-1]}" for i in range(start, end + 1))
        return snippet

    def edit_file_lines(self, path: str, start_line: int, end_line: int, new_content: str) -> str:
        """Replace lines [start_line, end_line] (1-indexed, inclusive) with
        new_content, leaving the rest of the file untouched. Costs roughly
        what the CHANGE is, not what the whole file is -- use this (or
        edit_symbol, preferred for Python) instead of rewriting an entire
        large file through write_file for a small change."""
        target = self._resolve(path)
        if not target.exists():
            return f"Error: '{path}' does not exist."
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").split("\n")
        except Exception as e:
            return f"Error reading '{path}': {e}"
        start = max(1, start_line)
        end = min(len(lines), end_line)
        if start > len(lines) + 1:
            return f"Error: '{path}' only has {len(lines)} lines (requested start_line={start_line})."

        preview = "\n".join(new_content.split("\n"))
        if not self.confirm_write(str(target), f"[replace lines {start}-{end}]\n{preview}"):
            return f"User declined editing '{path}'."

        new_lines = lines[:start - 1] + new_content.split("\n") + lines[end:]
        try:
            target.write_text("\n".join(new_lines), encoding="utf-8")
        except Exception as e:
            return f"Error writing '{path}': {e}"
        try:
            project_map.update_entry(str(self.root), path)
        except Exception:
            pass
        delta = len(new_lines) - len(lines)
        return (
            f"Replaced lines {start}-{end} in '{path}' ({len(new_lines)} lines now, "
            f"{'+' if delta >= 0 else ''}{delta} vs before). "
            f"NOTE: line numbers after line {start} have shifted by {delta} -- "
            f"re-check the project map or re-read before further line-based edits to this file."
        )

    def read_symbol(self, path: str, symbol_name: str) -> str:
        """Read just one class, function, or 'ClassName.method_name' from a
        Python file -- not the whole file. Boundaries are re-resolved fresh
        every call (never from a cached line number), so this stays correct
        even after earlier edits changed the file's line count."""
        target = self._resolve(path)
        if not target.exists():
            return f"Error: '{path}' does not exist."
        try:
            source = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error reading '{path}': {e}"
        loc = project_map.resolve_symbol(source, symbol_name)
        if not loc:
            return f"Error: could not find '{symbol_name}' in '{path}' (check exact name/casing, or use 'ClassName.method_name' for methods)."
        lines = source.split("\n")
        snippet = "\n".join(f"{i}: {lines[i-1]}" for i in range(loc["start_line"], loc["end_line"] + 1))
        return snippet

    def edit_symbol(self, path: str, symbol_name: str, new_code: str) -> str:
        """Replace just one class, function, or 'ClassName.method_name' with
        new_code, leaving the rest of the file untouched. new_code should
        include correct indentation matching what read_symbol showed (for a
        method, that means indented to sit inside its class -- if new_code's
        first line has no leading whitespace, it will be auto-indented to
        match). This is the preferred way to edit an existing Python file:
        it costs roughly what the changed piece is, not what the whole file
        is, which matters a lot on small-context free-tier models."""
        target = self._resolve(path)
        if not target.exists():
            return f"Error: '{path}' does not exist."
        try:
            source = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error reading '{path}': {e}"
        loc = project_map.resolve_symbol(source, symbol_name)
        if not loc:
            return f"Error: could not find '{symbol_name}' in '{path}' (check exact name/casing, or use 'ClassName.method_name' for methods)."

        lines = source.split("\n")
        original_first_line = lines[loc["start_line"] - 1]
        required_indent = original_first_line[: len(original_first_line) - len(original_first_line.lstrip())]

        new_lines_in = new_code.split("\n")
        first_new_indent = new_lines_in[0][: len(new_lines_in[0]) - len(new_lines_in[0].lstrip())] if new_lines_in[0].strip() else ""
        if loc["kind"] == "method" and first_new_indent != required_indent and not first_new_indent:
            # Model forgot the class-nesting indent -- auto-fix rather than
            # silently producing a file that fails to parse.
            new_lines_in = [f"{required_indent}{ln}" if ln.strip() else ln for ln in new_lines_in]

        if not self.confirm_write(str(target), f"[replace {symbol_name}]\n" + "\n".join(new_lines_in)):
            return f"User declined editing '{path}'."

        new_lines = lines[:loc["start_line"] - 1] + new_lines_in + lines[loc["end_line"]:]
        new_source = "\n".join(new_lines)

        # Verify the result still parses before committing -- a bad edit_symbol
        # call (wrong indent, unbalanced brackets, etc.) should surface as a
        # clear error, not silently corrupt the file.
        try:
            ast.parse(new_source)
        except SyntaxError as e:
            return (
                f"Edit NOT applied: the result would not be valid Python ({e}). "
                f"Check indentation and syntax in new_code, then try again."
            )

        try:
            target.write_text(new_source, encoding="utf-8")
        except Exception as e:
            return f"Error writing '{path}': {e}"
        try:
            project_map.update_entry(str(self.root), path)
        except Exception:
            pass
        return f"Replaced '{symbol_name}' in '{path}'. Project map updated -- re-check it if you need this file's other symbols' current line numbers."

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
        try:
            # Keep the project map in sync the instant a file changes --
            # cheap static parsing, no extra AI call, so the NEXT message
            # (or even the next step in this same run) already knows this
            # file's classes/functions without needing to read_file it again.
            project_map.update_entry(str(self.root), path)
        except Exception:
            pass  # map maintenance is a convenience, never worth failing the write over
        return f"Wrote {len(content)} characters to '{path}'."

    def generate_document(self, path: str, doc_type: str, title: str = "", sections: Optional[list] = None) -> str:
        """
        Create a real .docx, .pdf, or .xlsx file -- not a text dump. `sections`
        is a list of {"heading": str, "body": str} dicts (body can contain
        \\n-separated paragraphs; lines starting with "- " become bullets).
        For .xlsx, `sections` is instead a list of rows (each row a list of
        cell values), and `title` is used as the sheet name.
        """
        target = self._resolve(path)
        if not self.confirm_write(str(target), f"[generated {doc_type} document: {title}]"):
            return f"User declined creating '{path}'."
        sections = sections or []
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            doc_type = doc_type.lower().lstrip(".")

            if doc_type == "docx":
                from docx import Document
                doc = Document()
                if title:
                    doc.add_heading(title, level=0)
                for sec in sections:
                    if sec.get("heading"):
                        doc.add_heading(sec["heading"], level=1)
                    for line in sec.get("body", "").split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("- "):
                            doc.add_paragraph(line[2:], style="List Bullet")
                        else:
                            doc.add_paragraph(line)
                doc.save(str(target))

            elif doc_type == "pdf":
                from reportlab.lib.pagesizes import letter
                from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem
                from reportlab.lib.styles import getSampleStyleSheet

                font_name = _register_unicode_font()
                bold_font = f"{font_name}-Bold" if font_name != "Helvetica" else "Helvetica-Bold"

                styles = getSampleStyleSheet()
                for style_name in ("Title", "Heading2", "Normal"):
                    styles[style_name].fontName = (
                        bold_font if style_name in ("Title", "Heading2") else font_name
                    )

                story = []
                if title:
                    story.append(Paragraph(title, styles["Title"]))
                    story.append(Spacer(1, 12))
                for sec in sections:
                    if sec.get("heading"):
                        story.append(Paragraph(sec["heading"], styles["Heading2"]))
                    bullets = []
                    for line in sec.get("body", "").split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("- "):
                            bullets.append(ListItem(Paragraph(line[2:], styles["Normal"])))
                        else:
                            if bullets:
                                story.append(ListFlowable(bullets, bulletType="bullet"))
                                bullets = []
                            story.append(Paragraph(line, styles["Normal"]))
                    if bullets:
                        story.append(ListFlowable(bullets, bulletType="bullet"))
                    story.append(Spacer(1, 10))
                SimpleDocTemplate(str(target), pagesize=letter).build(story)

            elif doc_type == "xlsx":
                from openpyxl import Workbook
                wb = Workbook()
                ws = wb.active
                ws.title = (title or "Sheet1")[:31]
                for row in sections:
                    ws.append(row)
                wb.save(str(target))

            else:
                return f"Unsupported doc_type '{doc_type}'. Use 'docx', 'pdf', or 'xlsx'."

        except ImportError as e:
            return (
                f"Missing library for '{doc_type}' generation: {e}. "
                f"Install with: pip install python-docx reportlab openpyxl"
            )
        except Exception as e:
            return f"Error generating '{path}': {e}"

        return f"Created {doc_type} document at '{path}'."

    def _kill_process_tree(self, proc: "subprocess.Popen"):
        import sys, signal, os
        if sys.platform == "win32":
            # shell=True spawns cmd.exe as a wrapper around the real command;
            # proc.kill() only kills cmd.exe and can leave the actual child
            # (npm, python, etc.) running as an orphan. taskkill /T kills the
            # whole tree.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
        else:
            # Same orphan problem on POSIX: shell=True spawns /bin/sh -c
            # "<command>", and killing that shell process alone can leave the
            # actual child (e.g. a "sleep"/"npm"/test runner) running,
            # holding the stdout/stderr pipes open so any subsequent
            # communicate() call hangs until that orphan exits on its own.
            # We start the process in its own process group (see Popen call
            # below) specifically so we can kill the whole group here.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                proc.kill()

    def run_command(self, command: str, timeout: int = 30) -> str:
        if not self.confirm_command(command):
            return f"User declined running command: {command}"

        import time as _time
        import sys as _sys

        popen_kwargs = {}
        if _sys.platform != "win32":
            import os as _os
            popen_kwargs["preexec_fn"] = _os.setsid  # own process group, for _kill_process_tree

        try:
            proc = subprocess.Popen(
                command, shell=True, cwd=self.root,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                **popen_kwargs,
            )
        except Exception as e:
            return f"Error starting command '{command}': {e}"

        start = _time.time()
        poll_interval = 0.2
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=poll_interval)
                out = stdout[-4000:] if stdout else ""
                err = stderr[-2000:] if stderr else ""
                return f"exit_code={proc.returncode}\nstdout:\n{out}\nstderr:\n{err}"
            except subprocess.TimeoutExpired:
                pass

            if self.stop_check():
                self._kill_process_tree(proc)
                try:
                    proc.communicate(timeout=2)
                except Exception:
                    pass
                return f"Command stopped by user after {_time.time() - start:.1f}s: {command}"

            if _time.time() - start > timeout:
                self._kill_process_tree(proc)
                try:
                    proc.communicate(timeout=2)
                except Exception:
                    pass
                return f"Command timed out after {timeout}s: {command}"


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
        "description": (
            "Read the FULL text content of a file relative to the project root. "
            "For an EXISTING Python file that's already in the project map, "
            "prefer read_symbol instead -- it costs far less and avoids "
            "blowing past small free-tier request-size limits. Use read_file "
            "for new/small files, or files read_symbol can't parse."
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "read_file_range",
        "description": "Read only lines [start_line, end_line] (1-indexed, inclusive) of a file, each prefixed with its line number. Works on any text file. Cheaper than read_file for a large file when you only need part of it.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path", "start_line", "end_line"],
        },
    },
    {
        "name": "edit_file_lines",
        "description": (
            "Replace lines [start_line, end_line] (1-indexed, inclusive) of a "
            "file with new_content, leaving the rest of the file untouched. "
            "Use for non-Python files, or Python edits that don't cleanly map "
            "to one symbol. For Python, prefer edit_symbol when possible -- "
            "it doesn't require you to track line numbers yourself. NOTE: "
            "line numbers after your edit shift by however many lines you "
            "added/removed -- re-check the project map before another "
            "line-based edit to the same file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "new_content": {"type": "string", "description": "Replacement text for that line range (no line numbers, just the content)."},
            },
            "required": ["path", "start_line", "end_line", "new_content"],
        },
    },
    {
        "name": "read_symbol",
        "description": (
            "Read just ONE class, top-level function, or method ('ClassName.method_name') "
            "from a Python file -- not the whole file. THIS IS THE PREFERRED WAY to look "
            "at part of an existing large Python file: costs roughly what that piece is, "
            "not what the whole file is. Boundaries are re-resolved fresh every call, so "
            "it's always correct even after earlier edits to the file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "symbol_name": {"type": "string", "description": "e.g. 'MyClass', 'my_function', or 'MyClass.my_method'"},
            },
            "required": ["path", "symbol_name"],
        },
    },
    {
        "name": "edit_symbol",
        "description": (
            "Replace just ONE class, top-level function, or method ('ClassName.method_name') "
            "with new_code, leaving the rest of the file untouched. THIS IS THE PREFERRED WAY "
            "to edit an existing Python file: costs roughly what the CHANGE is, not the whole "
            "file, which is essential on small-context free-tier models. Result is verified to "
            "still be valid Python before being written -- a bad edit is rejected with a clear "
            "error instead of corrupting the file. new_code should include the def/class line(s) "
            "itself, with indentation matching what read_symbol showed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "symbol_name": {"type": "string", "description": "e.g. 'MyClass', 'my_function', or 'MyClass.my_method'"},
                "new_code": {"type": "string", "description": "Full replacement source for that class/function/method, including its def/class line."},
            },
            "required": ["path", "symbol_name", "new_code"],
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
        "name": "generate_document",
        "description": (
            "Create a REAL .docx, .pdf, or .xlsx file (not a text file). Use this "
            "whenever the user asks for a Word doc, PDF, report, or spreadsheet -- "
            "never try to fake one with write_file. For docx/pdf, 'sections' is a "
            "list of {heading, body} objects (body lines starting with '- ' become "
            "bullet points). For xlsx, 'sections' is a list of rows, each row a "
            "list of cell values, and the first row is typically your header."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Output path, e.g. 'reports/summary.pdf'"},
                "doc_type": {"type": "string", "enum": ["docx", "pdf", "xlsx"]},
                "title": {"type": "string"},
                "sections": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["path", "doc_type"],
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