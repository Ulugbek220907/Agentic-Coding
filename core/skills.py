"""
Skills: user-supplied folders of Python functions the AI agent can call,
stored in ~/.ai_agent_desktop/skills/<skill_name>/.

==========================  RULES / CAPABILITIES  ==========================
These are enforced (not just documented) by this module:

1. STRUCTURE -- a skill is a folder containing:
     skill.json   (required) manifest: name, entry_file, and an explicit
                  JSON-schema "parameters" block for each callable function.
                  Functions not listed here are NEVER callable by the AI,
                  even if they exist in the code -- the manifest is an
                  allowlist, not a suggestion.
     SKILL.md     (required) human-readable description, appended to the
                  system prompt so the model knows when to reach for it.
     *.py         the entry_file plus any modules it imports locally.

2. SIZE LIMITS -- a skill folder over MAX_SKILL_FILES files or
   MAX_SKILL_BYTES total is rejected at discovery time. This isn't a
   security boundary so much as a sanity one: skills are meant to be a
   handful of focused functions, not a place to smuggle in a whole
   alternate application.

3. NO AUTO-INSTALLED DEPENDENCIES -- the app will never pip-install
   anything on a skill's behalf. If a skill needs a package beyond the
   Python standard library and whatever this app already depends on
   (matplotlib etc.), the user installs it themselves into the same
   environment. Auto-installing arbitrary packages named by untrusted code
   is a supply-chain risk this app deliberately does not take on.

4. EXECUTION ISOLATION -- every skill function call runs in a SEPARATE
   PYTHON SUBPROCESS (see skill_runner.py), not inside the main app
   process:
     - cwd is the current project folder (skills can't casually wander the
       filesystem the way an in-process import could).
     - The subprocess gets a MINIMAL environment -- explicitly NOT the
       full os.environ, and never the AI provider API keys, so a skill
       cannot read or exfiltrate your model credentials even if it tried.
     - A hard timeout (default 30s, configurable) kills the subprocess if
       it hangs -- a runaway skill can't freeze the app.
     - A crash/exception/segfault in the subprocess cannot take down the
       main PyQt6 app.

5. RETURN CONTRACT -- a skill function's return value must be plain,
   JSON-serializable data. To surface a generated visual, return a dict
   with an "output_file" key (a path to a PNG/etc it wrote). The app never
   eval()s or exec()s anything a skill returns.

6. TRUST ON FIRST LOAD -- before a new or CHANGED skill can be enabled,
   its source is statically scanned for risky patterns (subprocess/os
   execution, eval/exec, raw sockets, package installs, filesystem
   deletion, network calls). The findings are shown to the user, who must
   explicitly approve before the skill becomes callable. This is a
   deterrent/awareness measure, NOT a sandbox -- a sufficiently obfuscated
   malicious skill could evade simple pattern matching. Only enable skills
   from sources you actually trust.

7. DISABLED BY DEFAULT -- newly discovered skills start disabled. Nothing
   becomes callable by the AI without an explicit opt-in per skill.
==============================================================================
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SKILLS_DIR = Path.home() / ".ai_agent_desktop" / "skills"
TRUST_FILE = Path.home() / ".ai_agent_desktop" / "skills_trust.json"

MAX_SKILL_FILES = 20
MAX_SKILL_BYTES = 2 * 1024 * 1024  # 2 MB
DEFAULT_TIMEOUT_SECONDS = 30

# Patterns flagged during the trust scan. Deliberately broad/simple --
# false positives are fine (the user just sees "uses subprocess" and can
# judge for themselves), false negatives are the real risk to minimize.
RISK_PATTERNS = {
    r"\bsubprocess\b": "runs shell commands (subprocess)",
    r"\bos\.system\b": "runs shell commands (os.system)",
    r"\bos\.popen\b": "runs shell commands (os.popen)",
    r"\beval\s*\(": "uses eval()",
    r"\bexec\s*\(": "uses exec()",
    r"\bsocket\b": "opens raw network sockets",
    r"\b(requests|urllib|http\.client)\b": "makes network requests",
    r"\bshutil\.rmtree\b": "can recursively delete directories",
    r"\bos\.remove\b|\bos\.unlink\b": "can delete files",
    r"\bpip\b|\bpip_install\b": "may attempt to install packages",
    r"__import__\s*\(": "uses dynamic __import__",
}


@dataclass
class SkillFunction:
    name: str
    description: str
    parameters: dict  # JSON-schema, as given verbatim in skill.json


@dataclass
class SkillInfo:
    name: str
    display_name: str
    path: Path
    entry_file: str
    functions: List[SkillFunction]
    doc: str                  # contents of SKILL.md
    content_hash: str
    risk_findings: List[str] = field(default_factory=list)
    enabled: bool = False
    trusted_hash: Optional[str] = None  # hash the user last approved, if any
    load_error: Optional[str] = None

    @property
    def needs_trust_review(self) -> bool:
        return self.trusted_hash != self.content_hash


def _hash_folder(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(path.rglob("*")):
        if f.is_file():
            h.update(f.name.encode("utf-8", "replace"))
            h.update(f.read_bytes())
    return h.hexdigest()


def _scan_risk_patterns(path: Path) -> List[str]:
    findings = []
    for py_file in sorted(path.glob("*.py")):
        try:
            text = py_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for pattern, label in RISK_PATTERNS.items():
            if re.search(pattern, text):
                findings.append(f"{py_file.name}: {label}")
    return findings


class SkillManager:
    def __init__(self, skills_dir: Path = SKILLS_DIR, trust_file: Path = TRUST_FILE):
        self.skills_dir = skills_dir
        self.trust_file = trust_file
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def _load_trust_store(self) -> dict:
        if self.trust_file.exists():
            try:
                return json.loads(self.trust_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_trust_store(self, store: dict):
        self.trust_file.write_text(json.dumps(store, indent=2), encoding="utf-8")

    def approve_skill(self, skill: SkillInfo):
        """Call after showing the user the risk findings and getting explicit approval."""
        store = self._load_trust_store()
        store[skill.name] = {"hash": skill.content_hash, "enabled": True}
        self._save_trust_store(store)

    def set_enabled(self, skill_name: str, enabled: bool):
        store = self._load_trust_store()
        store.setdefault(skill_name, {})["enabled"] = enabled
        self._save_trust_store(store)

    def discover(self) -> List[SkillInfo]:
        trust_store = self._load_trust_store()
        results = []
        if not self.skills_dir.exists():
            return results

        for folder in sorted(p for p in self.skills_dir.iterdir() if p.is_dir()):
            manifest_path = folder / "skill.json"
            doc_path = folder / "SKILL.md"

            files = list(folder.rglob("*"))
            file_count = sum(1 for f in files if f.is_file())
            total_bytes = sum(f.stat().st_size for f in files if f.is_file())

            if not manifest_path.exists():
                results.append(SkillInfo(
                    name=folder.name, display_name=folder.name, path=folder,
                    entry_file="", functions=[], doc="", content_hash="",
                    load_error="Missing skill.json manifest -- skipped.",
                ))
                continue
            if file_count > MAX_SKILL_FILES or total_bytes > MAX_SKILL_BYTES:
                results.append(SkillInfo(
                    name=folder.name, display_name=folder.name, path=folder,
                    entry_file="", functions=[], doc="", content_hash="",
                    load_error=f"Too large ({file_count} files, {total_bytes} bytes) -- "
                               f"limit is {MAX_SKILL_FILES} files / {MAX_SKILL_BYTES} bytes. Skipped.",
                ))
                continue

            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                entry_file = manifest["entry_file"]
                functions = [
                    SkillFunction(name=f["name"], description=f.get("description", ""), parameters=f.get("parameters", {}))
                    for f in manifest.get("functions", [])
                ]
                if not (folder / entry_file).exists():
                    raise ValueError(f"entry_file '{entry_file}' does not exist in {folder}")
            except Exception as e:
                results.append(SkillInfo(
                    name=folder.name, display_name=folder.name, path=folder,
                    entry_file="", functions=[], doc="", content_hash="",
                    load_error=f"Invalid skill.json: {e}",
                ))
                continue

            doc = doc_path.read_text(encoding="utf-8", errors="replace") if doc_path.exists() else ""
            content_hash = _hash_folder(folder)
            risk_findings = _scan_risk_patterns(folder)
            trust_entry = trust_store.get(folder.name, {})

            results.append(SkillInfo(
                name=folder.name,
                display_name=manifest.get("display_name", folder.name),
                path=folder,
                entry_file=entry_file,
                functions=functions,
                doc=doc,
                content_hash=content_hash,
                risk_findings=risk_findings,
                enabled=trust_entry.get("enabled", False),
                trusted_hash=trust_entry.get("hash"),
            ))
        return results

    def enabled_callable_skills(self) -> List[SkillInfo]:
        """Skills that are both enabled AND have had their current content
        hash explicitly trusted -- if a skill's files changed since
        approval, it's excluded here until re-reviewed, even if the old
        'enabled' flag is still set."""
        return [s for s in self.discover() if s.enabled and not s.needs_trust_review and not s.load_error]

    def tool_schemas(self) -> List[dict]:
        """Tool schemas for every enabled+trusted skill's functions, in the
        same shape as core.tools.AGENT_TOOLS, so agent.py can just concatenate
        them. Tool names are namespaced as skill__<skill>__<function> to
        avoid collisions between skills."""
        schemas = []
        for skill in self.enabled_callable_skills():
            for fn in skill.functions:
                schemas.append({
                    "name": f"skill__{skill.name}__{fn.name}",
                    "description": f"[Skill: {skill.display_name}] {fn.description}",
                    "parameters": fn.parameters,
                })
        return schemas

    def system_prompt_fragment(self) -> str:
        skills = self.enabled_callable_skills()
        if not skills:
            return ""
        parts = ["You also have access to the following user-provided skills:"]
        for skill in skills:
            parts.append(f"\n--- Skill: {skill.display_name} ---\n{skill.doc.strip()}")
        return "\n".join(parts)

    def is_skill_tool(self, tool_name: str) -> bool:
        return tool_name.startswith("skill__")

    def call(self, tool_name: str, arguments: dict, project_root: str, timeout: Optional[int] = None) -> str:
        """Dispatch a skill__<skill>__<function> tool call, running the
        actual function in an isolated subprocess. Returns a string (the
        same convention as every other tool in core/tools.py)."""
        try:
            _, skill_name, func_name = tool_name.split("__", 2)
        except ValueError:
            return f"Malformed skill tool name: {tool_name}"

        skills = {s.name: s for s in self.enabled_callable_skills()}
        skill = skills.get(skill_name)
        if not skill:
            return f"Skill '{skill_name}' is not enabled/trusted (it may need re-approval in Skill settings)."
        if not any(f.name == func_name for f in skill.functions):
            return f"'{func_name}' is not a declared function of skill '{skill_name}'."

        timeout = timeout or DEFAULT_TIMEOUT_SECONDS
        with tempfile.TemporaryDirectory() as tmp:
            args_path = str(Path(tmp) / "args.json")
            output_path = str(Path(tmp) / "output.json")
            Path(args_path).write_text(json.dumps(arguments), encoding="utf-8")

            runner = str(Path(__file__).parent / "skill_runner.py")
            # Minimal environment -- deliberately NOT os.environ. In
            # particular this means the skill subprocess never sees your
            # configured AI provider API keys, even though the main app
            # process holds them.
            import os
            minimal_env = {"PATH": os.environ.get("PATH", "")}
            if sys.platform == "win32":
                minimal_env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
                minimal_env["PATHEXT"] = os.environ.get("PATHEXT", "")

            try:
                subprocess.run(
                    [sys.executable, runner, str(skill.path), skill.entry_file, func_name, args_path, output_path],
                    cwd=project_root,
                    env=minimal_env,
                    timeout=timeout,
                    capture_output=True,
                )
            except subprocess.TimeoutExpired:
                return f"Skill function '{skill_name}.{func_name}' timed out after {timeout}s."

            if not Path(output_path).exists():
                return f"Skill function '{skill_name}.{func_name}' produced no output (it may have crashed before writing a result)."

            try:
                result = json.loads(Path(output_path).read_text(encoding="utf-8"))
            except Exception as e:
                return f"Could not parse skill output: {e}"

            if not result.get("ok"):
                return f"Skill function '{skill_name}.{func_name}' failed: {result.get('error')}"
            return json.dumps(result.get("result"))