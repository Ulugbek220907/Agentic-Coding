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


_INLINE_TOKEN_RE = None  # lazily compiled, see _parse_inline()


def _parse_inline(text: str) -> list:
    """
    Split text into (segment_text, {"bold":bool,"italic":bool,"code":bool})
    runs based on simple **bold**, *italic*, `code` markers. Used by both
    the docx and pdf writers so a model can write natural markdown-ish text
    once and have it render as REAL bold/italic runs in both formats,
    instead of everything coming out as flat unstyled text.
    """
    import re
    global _INLINE_TOKEN_RE
    if _INLINE_TOKEN_RE is None:
        _INLINE_TOKEN_RE = re.compile(r"(\*\*.+?\*\*|\*.+?\*|`.+?`)")

    segments = []
    for token in _INLINE_TOKEN_RE.split(text):
        if not token:
            continue
        if token.startswith("**") and token.endswith("**"):
            segments.append((token[2:-2], {"bold": True, "italic": False, "code": False}))
        elif token.startswith("*") and token.endswith("*"):
            segments.append((token[1:-1], {"bold": False, "italic": True, "code": False}))
        elif token.startswith("`") and token.endswith("`"):
            segments.append((token[1:-1], {"bold": False, "italic": False, "code": True}))
        else:
            segments.append((token, {"bold": False, "italic": False, "code": False}))
    return segments


def _docx_add_inline_runs(paragraph, text: str):
    for seg_text, style in _parse_inline(text):
        run = paragraph.add_run(seg_text)
        run.bold = style["bold"]
        run.italic = style["italic"]
        if style["code"]:
            run.font.name = "Consolas"

def _docx_shade_cell(cell, hex_color: str):
    """python-docx has no high-level API for cell background shading --
    this drops down to the underlying OOXML, which is the documented way
    to do it."""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), hex_color)
    cell._tc.get_or_add_tcPr().append(shading)


def _pdf_inline_to_markup(text: str) -> str:
    """Convert our **bold**/*italic*/`code` markers to reportlab's Paragraph
    mini-markup (a restricted HTML-like subset it natively understands)."""
    import html as _html
    parts = []
    for seg_text, style in _parse_inline(text):
        escaped = _html.escape(seg_text)
        if style["bold"]:
            escaped = f"<b>{escaped}</b>"
        if style["italic"]:
            escaped = f"<i>{escaped}</i>"
        if style["code"]:
            escaped = f"<font face='Courier'>{escaped}</font>"
        parts.append(escaped)
    return "".join(parts)


def _normalize_blocks(sections: Optional[list], blocks: Optional[list]) -> list:
    """Back-compat: if the caller used the old sections=[{heading,body}]
    shape, convert it into the new blocks format so both code paths funnel
    through one renderer."""
    if blocks:
        return blocks
    out = []
    for sec in sections or []:
        if sec.get("heading"):
            out.append({"type": "heading", "text": sec["heading"], "level": 1})
        current_list = []
        for line in sec.get("body", "").split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("- "):
                current_list.append(line[2:])
            else:
                if current_list:
                    out.append({"type": "bullet_list", "items": current_list})
                    current_list = []
                out.append({"type": "paragraph", "text": line})
        if current_list:
            out.append({"type": "bullet_list", "items": current_list})
    return out


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

    def generate_document(
        self, path: str, doc_type: str, title: str = "",
        sections: Optional[list] = None, blocks: Optional[list] = None,
        subtitle: str = "", author: str = "",
    ) -> str:
        """
        Create a real .docx, .pdf, or .xlsx file with professional
        formatting -- headings, styled tables, inline bold/italic, bullet
        and numbered lists, images, and page breaks.

        `blocks` (preferred) is a list of dicts, each with a "type":
          {"type": "heading", "text": "...", "level": 1-4}
          {"type": "paragraph", "text": "supports **bold**, *italic*, `code`"}
          {"type": "bullet_list", "items": ["...", "..."]}
          {"type": "numbered_list", "items": ["...", "..."]}
          {"type": "table", "headers": ["Col A","Col B"], "rows": [["1","2"]],
           "col_widths": [2.0, 3.0]}   # optional, inches
          {"type": "image", "path": "chart.png", "width_inches": 5.5}
          {"type": "page_break"}
          {"type": "spacer"}

        `sections` (old shape, still accepted) is auto-converted to blocks.

        For .xlsx: `blocks`/`sections` can be a list of {"sheet_name":str,
        "headers":[...], "rows":[[...],...]} for one or more styled sheets,
        OR the old plain list-of-rows shape for a single unstyled sheet.
        """
        target = self._resolve(path)
        if not self.confirm_write(str(target), f"[generated {doc_type} document: {title}]"):
            return f"User declined creating '{path}'."
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            doc_type = doc_type.lower().lstrip(".")

            if doc_type == "docx":
                self._generate_docx(target, title, subtitle, author, _normalize_blocks(sections, blocks))
            elif doc_type == "pdf":
                self._generate_pdf(target, title, subtitle, author, _normalize_blocks(sections, blocks))
            elif doc_type == "xlsx":
                self._generate_xlsx(target, title, sections or blocks or [])
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

    def _generate_docx(self, target, title, subtitle, author, blocks):
        from docx import Document
        from docx.shared import Inches, Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        doc = Document()
        if author:
            doc.core_properties.author = author
        if title:
            doc.core_properties.title = title

        if title:
            h = doc.add_heading(title, level=0)
        if subtitle:
            sub = doc.add_paragraph(subtitle)
            sub.style = doc.styles["Subtitle"] if "Subtitle" in [s.name for s in doc.styles] else sub.style
            for run in sub.runs:
                run.italic = True
                run.font.color.rgb = RGBColor(0x60, 0x60, 0x60)

        for block in blocks:
            btype = block.get("type")
            if btype == "heading":
                doc.add_heading(block.get("text", ""), level=min(max(block.get("level", 1), 1), 4))
            elif btype == "paragraph":
                p = doc.add_paragraph()
                _docx_add_inline_runs(p, block.get("text", ""))
            elif btype == "bullet_list":
                for item in block.get("items", []):
                    p = doc.add_paragraph(style="List Bullet")
                    _docx_add_inline_runs(p, item)
            elif btype == "numbered_list":
                for item in block.get("items", []):
                    p = doc.add_paragraph(style="List Number")
                    _docx_add_inline_runs(p, item)
            elif btype == "table":
                headers = block.get("headers", [])
                rows = block.get("rows", [])
                col_widths = block.get("col_widths")
                if not headers and not rows:
                    continue
                n_cols = len(headers) if headers else (len(rows[0]) if rows else 0)
                if n_cols == 0:
                    continue
                table = doc.add_table(rows=0, cols=n_cols)
                table.style = "Table Grid"
                if headers:
                    row_cells = table.add_row().cells
                    for i, h_text in enumerate(headers):
                        row_cells[i].text = ""
                        p = row_cells[i].paragraphs[0]
                        run = p.add_run(str(h_text))
                        run.bold = True
                        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                        _docx_shade_cell(row_cells[i], "2F5597")  # professional dark blue header
                for r_idx, row in enumerate(rows):
                    row_cells = table.add_row().cells
                    for i, cell_val in enumerate(row):
                        if i >= n_cols:
                            break
                        row_cells[i].text = ""
                        p = row_cells[i].paragraphs[0]
                        _docx_add_inline_runs(p, str(cell_val))
                        if r_idx % 2 == 1:
                            _docx_shade_cell(row_cells[i], "F2F2F2")  # subtle zebra striping
                if col_widths:
                    for i, w in enumerate(col_widths):
                        if i < n_cols:
                            for row in table.rows:
                                row.cells[i].width = Inches(w)
                doc.add_paragraph()  # breathing room after a table
            elif btype == "image":
                img_path = self._resolve(block.get("path", ""))
                if img_path.exists():
                    width = block.get("width_inches")
                    doc.add_picture(str(img_path), width=Inches(width) if width else None)
                else:
                    doc.add_paragraph(f"[image not found: {block.get('path')}]")
            elif btype == "page_break":
                doc.add_page_break()
            elif btype == "spacer":
                doc.add_paragraph()

        doc.save(str(target))

    def _generate_pdf(self, target, title, subtitle, author, blocks):
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem,
            Table, TableStyle, Image, PageBreak,
        )
        from reportlab.lib.styles import getSampleStyleSheet

        font_name = _register_unicode_font()
        bold_font = f"{font_name}-Bold" if font_name != "Helvetica" else "Helvetica-Bold"

        styles = getSampleStyleSheet()
        for style_name in ("Title", "Heading1", "Heading2", "Heading3", "Heading4", "Normal"):
            if style_name in styles.byName:
                is_heading = style_name.startswith("Heading") or style_name == "Title"
                styles[style_name].fontName = bold_font if is_heading else font_name

        story = []
        if title:
            story.append(Paragraph(title, styles["Title"]))
        if subtitle:
            sub_style = styles["Normal"].clone("Subtitle")
            sub_style.textColor = colors.HexColor("#606060")
            sub_style.fontSize = 12
            story.append(Paragraph(subtitle, sub_style))
        if title or subtitle:
            story.append(Spacer(1, 16))

        heading_style_for_level = {1: "Heading1", 2: "Heading2", 3: "Heading3", 4: "Heading4"}

        for block in blocks:
            btype = block.get("type")
            if btype == "heading":
                level = min(max(block.get("level", 1), 1), 4)
                story.append(Paragraph(_pdf_inline_to_markup(block.get("text", "")), styles[heading_style_for_level[level]]))
                story.append(Spacer(1, 6))
            elif btype == "paragraph":
                story.append(Paragraph(_pdf_inline_to_markup(block.get("text", "")), styles["Normal"]))
                story.append(Spacer(1, 6))
            elif btype == "bullet_list":
                items = [ListItem(Paragraph(_pdf_inline_to_markup(i), styles["Normal"])) for i in block.get("items", [])]
                story.append(ListFlowable(items, bulletType="bullet"))
                story.append(Spacer(1, 8))
            elif btype == "numbered_list":
                items = [ListItem(Paragraph(_pdf_inline_to_markup(i), styles["Normal"])) for i in block.get("items", [])]
                story.append(ListFlowable(items, bulletType="1"))
                story.append(Spacer(1, 8))
            elif btype == "table":
                headers = block.get("headers", [])
                rows = block.get("rows", [])
                if not headers and not rows:
                    continue
                # Wrap every cell in a Paragraph (not a bare string) so long
                # text actually WRAPS instead of overflowing the column --
                # this is a common reportlab gotcha with plain-string Table data.
                cell_style = styles["Normal"].clone("TableCell")
                cell_style.fontSize = 9
                header_style = cell_style.clone("TableHeader")
                header_style.textColor = colors.white
                header_style.fontName = bold_font

                table_data = []
                if headers:
                    table_data.append([Paragraph(_pdf_inline_to_markup(str(h)), header_style) for h in headers])
                for row in rows:
                    table_data.append([Paragraph(_pdf_inline_to_markup(str(c)), cell_style) for c in row])

                col_widths = block.get("col_widths")
                col_widths_pt = [w * inch for w in col_widths] if col_widths else None

                t = Table(table_data, colWidths=col_widths_pt, repeatRows=1 if headers else 0)
                style_cmds = [
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
                if headers:
                    style_cmds.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2F5597")))
                    data_start = 1
                else:
                    data_start = 0
                # Zebra striping on data rows for readability.
                for i in range(data_start, len(table_data)):
                    if (i - data_start) % 2 == 1:
                        style_cmds.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F2F2F2")))
                t.setStyle(TableStyle(style_cmds))
                story.append(t)
                story.append(Spacer(1, 12))
            elif btype == "image":
                img_path = self._resolve(block.get("path", ""))
                if img_path.exists():
                    width = block.get("width_inches", 5.0) * inch
                    story.append(Image(str(img_path), width=width, height=None, kind="proportional"))
                    story.append(Spacer(1, 10))
                else:
                    story.append(Paragraph(f"[image not found: {block.get('path')}]", styles["Normal"]))
            elif btype == "page_break":
                story.append(PageBreak())
            elif btype == "spacer":
                story.append(Spacer(1, 12))

        def _add_page_number(canvas, doc):
            canvas.saveState()
            canvas.setFont(font_name, 8)
            canvas.setFillColor(colors.HexColor("#888888"))
            canvas.drawRightString(letter[0] - 0.6 * inch, 0.4 * inch, f"Page {doc.page}")
            canvas.restoreState()

        doc = SimpleDocTemplate(
            str(target), pagesize=letter,
            leftMargin=0.9 * inch, rightMargin=0.9 * inch,
            topMargin=0.8 * inch, bottomMargin=0.8 * inch,
            title=title or "", author=author or "",
        )
        doc.build(story, onFirstPage=_add_page_number, onLaterPages=_add_page_number)

    def _generate_xlsx(self, target, title, sheets):
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

        wb = Workbook()
        wb.remove(wb.active)  # replace default sheet with our own explicitly-named ones

        # Accept either the new multi-sheet shape or the old flat list-of-rows shape.
        if sheets and isinstance(sheets[0], dict) and ("rows" in sheets[0] or "headers" in sheets[0]):
            sheet_specs = sheets
        else:
            sheet_specs = [{"sheet_name": title or "Sheet1", "headers": [], "rows": sheets}]

        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="2F5597", end_color="2F5597", fill_type="solid")
        zebra_fill = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
        thin = Side(style="thin", color="CCCCCC")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        for spec in sheet_specs:
            ws = wb.create_sheet(title=(spec.get("sheet_name") or "Sheet1")[:31])
            headers = spec.get("headers", [])
            rows = spec.get("rows", [])

            if headers:
                ws.append(headers)
                for cell in ws[1]:
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                    cell.border = border

            for r_idx, row in enumerate(rows):
                ws.append(row)
                if headers:  # only zebra-stripe/border rows when there's a real header to anchor against
                    excel_row = r_idx + 2
                    for cell in ws[excel_row]:
                        cell.border = border
                        if r_idx % 2 == 1:
                            cell.fill = zebra_fill

            # Auto-size columns based on content length (openpyxl has no
            # built-in autofit, this is the standard workaround).
            for col_cells in ws.columns:
                length = max((len(str(c.value)) for c in col_cells if c.value is not None), default=8)
                ws.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 10), 50)

            if headers:
                ws.freeze_panes = "A2"

        wb.save(str(target))

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
            "Create a REAL, professionally formatted .docx, .pdf, or .xlsx file "
            "(not a text file). Use this whenever the user asks for a Word doc, "
            "PDF, report, or spreadsheet -- never fake one with write_file.\n\n"
            "For docx/pdf, use 'blocks': a list of typed objects rendered in order:\n"
            '  {"type":"heading","text":"...","level":1-4}\n'
            '  {"type":"paragraph","text":"supports **bold**, *italic*, `code`"}\n'
            '  {"type":"bullet_list","items":["...","..."]}\n'
            '  {"type":"numbered_list","items":["...","..."]}\n'
            '  {"type":"table","headers":["Col A","Col B"],"rows":[["1","2"]],"col_widths":[2.0,3.0]}\n'
            '  {"type":"image","path":"chart.png","width_inches":5.5}\n'
            '  {"type":"page_break"}\n'
            '  {"type":"spacer"}\n'
            "Tables get real styled headers, borders, and zebra-striped rows -- "
            "always use a table block for any tabular/comparison data instead of "
            "faking a table with dashes or pipes in a paragraph.\n\n"
            "For xlsx, 'blocks' is a list of one or more sheets: "
            '[{"sheet_name":"Sheet1","headers":[...],"rows":[[...],...]}, ...] -- '
            "each becomes its own styled sheet with a bold header row and auto-sized columns.\n\n"
            "'sections' (legacy: list of {heading, body}) is still accepted but "
            "'blocks' should be preferred for anything needing a table, image, or "
            "precise formatting."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Output path, e.g. 'reports/summary.pdf'"},
                "doc_type": {"type": "string", "enum": ["docx", "pdf", "xlsx"]},
                "title": {"type": "string"},
                "subtitle": {"type": "string", "description": "Optional, shown under the title (docx/pdf only)."},
                "author": {"type": "string", "description": "Optional document metadata author (docx/pdf only)."},
                "blocks": {"type": "array", "items": {"type": "object"}, "description": "Preferred. See description for block types."},
                "sections": {"type": "array", "items": {"type": "object"}, "description": "Legacy alternative to blocks: list of {heading, body}."},
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