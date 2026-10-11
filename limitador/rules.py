"""Configuration: global limits, per-application rules and UI options.

Persisted as JSON in ``%APPDATA%\\Limitador\\config.json`` so a portable copy of the app
and an installed copy share settings per user.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "Limitador"
APP_VERSION = "2.0.0"
MB = 1024 * 1024


def appdata_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_NAME


def config_path() -> Path:
    return appdata_dir() / "config.json"


def log_path() -> Path:
    return appdata_dir() / "limitador.log"


@dataclass
class AppRule:
    """Per-application limit. ``exe`` is the normalised full image path."""

    exe: str
    name: str = ""
    down_bps: float = 0.0
    up_bps: float = 0.0
    enabled: bool = True

    @property
    def has_limit(self) -> bool:
        return self.enabled and (self.down_bps > 0 or self.up_bps > 0)

    @property
    def display_name(self) -> str:
        return self.name or (self.exe.rsplit("\\", 1)[-1] if self.exe else "?")


@dataclass
class Settings:
    """Global options. A rate of 0 means 'unlimited'."""

    global_down_bps: float = 0.0
    global_up_bps: float = 0.0
    max_delay_ms: float = 50.0
    exempt_control: bool = True      # never shape pure ACKs / DNS / ICMP
    exclude_lan: bool = True         # never shape traffic to/from local networks
    limit_ipv6: bool = True
    minimize_to_tray: bool = True
    autostart: bool = False
    start_on_launch: bool = False    # begin limiting as soon as the app opens

    def clamp(self) -> None:
        self.global_down_bps = max(0.0, float(self.global_down_bps))
        self.global_up_bps = max(0.0, float(self.global_up_bps))
        self.max_delay_ms = min(500.0, max(5.0, float(self.max_delay_ms)))

    @property
    def has_global_limit(self) -> bool:
        return self.global_down_bps > 0 or self.global_up_bps > 0


@dataclass
class Config:
    settings: Settings = field(default_factory=Settings)
    rules: list[AppRule] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    # --------------------------------------------------------------------- rules
    @staticmethod
    def normalise_exe(exe: str) -> str:
        return os.path.normcase(os.path.abspath(exe)) if exe else ""

    def rule_for_exe(self, exe: str | None) -> AppRule | None:
        if not exe:
            return None
        key = self.normalise_exe(exe)
        with self._lock:
            for rule in self.rules:
                if self.normalise_exe(rule.exe) == key:
                    return rule
        return None

    def rule_for_pid(self, procmap, pid: int | None) -> AppRule | None:
        if not pid:
            return None
        return self.rule_for_exe(procmap.path_for(pid))

    def set_rule(self, rule: AppRule) -> AppRule:
        rule.exe = self.normalise_exe(rule.exe)
        with self._lock:
            for i, existing in enumerate(self.rules):
                if self.normalise_exe(existing.exe) == rule.exe:
                    self.rules[i] = rule
                    return rule
            self.rules.append(rule)
        return rule

    def remove_rule(self, exe: str) -> bool:
        key = self.normalise_exe(exe)
        with self._lock:
            before = len(self.rules)
            self.rules = [r for r in self.rules if self.normalise_exe(r.exe) != key]
            return len(self.rules) != before

    def active_rules(self) -> list[AppRule]:
        with self._lock:
            return [r for r in self.rules if r.has_limit]

    # ---------------------------------------------------------------------- disk
    def to_dict(self) -> dict:
        return {
            "version": APP_VERSION,
            "settings": asdict(self.settings),
            "rules": [asdict(r) for r in self.rules],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        settings = Settings(**(data.get("settings") or {}))
        rules = []
        for raw in data.get("rules") or []:
            try:
                rules.append(AppRule(**raw))
            except TypeError:
                continue
        config = cls(settings=settings, rules=rules)
        config.settings.clamp()
        return config

    def save(self, path: Path | None = None) -> None:
        target = path or config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        with self._lock:
            payload = self.to_dict()
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        target = path or config_path()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (json.JSONDecodeError, OSError):
            return cls()
        return cls.from_dict(data)
