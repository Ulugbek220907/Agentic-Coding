"""
Configuration storage for the AI Agent Desktop app.

Stores the list of configured models (with provider, endpoint, key, priority)
and the last-used project folder, in a simple JSON file under the user's
home directory: ~/.ai_agent_desktop/config.json

NOTE ON SECURITY: API keys are stored in plaintext JSON here for simplicity.
For anything beyond personal/local use, swap this out for the `keyring`
package (OS-level credential storage) -- see the README for a drop-in
replacement sketch.
"""
from __future__ import annotations

import json
import os
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

CONFIG_DIR = Path.home() / ".ai_agent_desktop"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class ModelConfig:
    name: str                     # friendly name, e.g. "GPT-4.1 primary"
    provider: str                 # "openai_compatible" | "anthropic"
    base_url: str                 # e.g. https://api.openai.com/v1  or https://api.anthropic.com
    api_key: str
    model_id: str                 # e.g. "gpt-4.1", "claude-sonnet-5", "llama-3.1-70b"
    priority: int = 0              # lower number = tried first
    enabled: bool = True
    timeout_seconds: int = 60
    extra_headers: dict = field(default_factory=dict)
    rpm_limit: Optional[int] = None  # requests/minute you want to self-cap at; None/0 = unlimited
    rpd_limit: Optional[int] = None  # requests/day you want to self-cap at; None/0 = unlimited


@dataclass
class AppConfig:
    project_folder: Optional[str] = None
    recent_projects: List[str] = field(default_factory=list)  # most-recent-first, capped at 10
    models: List[ModelConfig] = field(default_factory=list)
    max_agent_iterations: int = 60  # 0 = no fixed cap (stall detection + Stop button are the safety net instead)
    auto_confirm_writes: bool = False   # if False, UI will ask before writing files
    auto_confirm_commands: bool = False # if False, UI will ask before running shell commands


class ConfigManager:
    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self.config = AppConfig()
        self.load()

    def load(self) -> AppConfig:
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            models = [ModelConfig(**m) for m in raw.get("models", [])]
            self.config = AppConfig(
                project_folder=raw.get("project_folder"),
                recent_projects=raw.get("recent_projects", []),
                models=models,
                max_agent_iterations=raw.get("max_agent_iterations", 60),
                auto_confirm_writes=raw.get("auto_confirm_writes", False),
                auto_confirm_commands=raw.get("auto_confirm_commands", False),
            )
        else:
            self.config = AppConfig()
            self.save()
        return self.config

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = dataclasses.asdict(self.config)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def sorted_models(self) -> List[ModelConfig]:
        """Enabled models, ordered by priority (ascending = tried first)."""
        return sorted(
            [m for m in self.config.models if m.enabled],
            key=lambda m: m.priority,
        )

    def add_model(self, model: ModelConfig) -> None:
        self.config.models.append(model)
        self.save()

    def update_model(self, index: int, model: ModelConfig) -> None:
        self.config.models[index] = model
        self.save()

    def remove_model(self, index: int) -> None:
        del self.config.models[index]
        self.save()

    def set_project_folder(self, folder: str) -> None:
        self.config.project_folder = folder
        recents = [p for p in self.config.recent_projects if p != folder]
        recents.insert(0, folder)
        self.config.recent_projects = recents[:10]
        self.save()