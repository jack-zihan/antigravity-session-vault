"""Configuration model, validation, and logger setup for Antigravity Session Vault."""

from __future__ import annotations

import dataclasses
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

ENV_CONFIG_PATH = "SESSION_VAULT_CONFIG"


def _validate_int(val: object, name: str, *, min_value: int) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"Config '{name}' must be an integer, got {val!r}")
    if val < min_value:
        raise ValueError(f"Config '{name}' must be >= {min_value}, got {val}")
    return val


def _require_path(value: object, label: str) -> Path:
    if isinstance(value, str):
        if not value.strip():
            raise ValueError(f"{label} must be non-empty")
        return Path(value).expanduser()
    if isinstance(value, Path):
        if not value.parts or not str(value).strip():
            raise ValueError(f"{label} must be non-empty")
        return value.expanduser()
    raise ValueError(f"{label} must be a Path or non-empty string, got {value!r}")


def default_home_dir() -> Path:
    return Path.home() / ".gemini" / "antigravity-session-vault"


def default_config_path() -> Path:
    env_path = os.environ.get(ENV_CONFIG_PATH, "").strip()
    return Path(env_path).expanduser() if env_path else default_home_dir() / "config.json"


@dataclasses.dataclass(frozen=True)
class RetentionPolicy:
    target_max: int = 420
    root_active_days: int = 2
    keep_idle_days: int = 3
    recent_touch_guard_sec: int = 300

    def __post_init__(self) -> None:
        _validate_int(self.target_max, "retention.target_max", min_value=1)
        _validate_int(self.root_active_days, "retention.root_active_days", min_value=0)
        _validate_int(self.keep_idle_days, "retention.keep_idle_days", min_value=0)
        _validate_int(self.recent_touch_guard_sec, "retention.recent_touch_guard_sec", min_value=0)


@dataclasses.dataclass(frozen=True)
class WatcherSettings:
    shield_interval_sec: int = 120
    backup_interval_sec: int = 1800

    def __post_init__(self) -> None:
        _validate_int(self.shield_interval_sec, "watcher.shield_interval_sec", min_value=1)
        _validate_int(self.backup_interval_sec, "watcher.backup_interval_sec", min_value=1)


@dataclasses.dataclass(frozen=True)
class VaultConfig:
    data_dir: Path
    archive_dir: Path
    vault_dir: Path
    log_file: Path
    lock_dir: Path
    retention: RetentionPolicy = dataclasses.field(default_factory=RetentionPolicy)
    watcher: WatcherSettings = dataclasses.field(default_factory=WatcherSettings)

    @property
    def conv_dir(self) -> Path:
        return self.data_dir / "conversations"

    @property
    def brain_dir(self) -> Path:
        return self.data_dir / "brain"

    @property
    def ann_dir(self) -> Path:
        return self.data_dir / "annotations"

    @property
    def chap_dir(self) -> Path:
        return self.data_dir / "chapter_titles"

    @property
    def summary_db(self) -> Path:
        return self.data_dir / "conversation_summaries.db"

    @property
    def summary_pb(self) -> Path:
        return self.data_dir / "agyhub_summaries_proto.pb"

    @property
    def subagent_archive_dir(self) -> Path:
        return self.archive_dir / "subagents"

    @property
    def subagent_index_path(self) -> Path:
        return self.subagent_archive_dir / "index.jsonl"

    @property
    def vault_roots_dir(self) -> Path:
        return self.vault_dir / "roots"

    def to_dict(self) -> dict[str, object]:
        return {
            "data_dir": str(self.data_dir),
            "archive_dir": str(self.archive_dir),
            "vault_dir": str(self.vault_dir),
            "log_file": str(self.log_file),
            "lock_dir": str(self.lock_dir),
            "retention": dataclasses.asdict(self.retention),
            "watcher": dataclasses.asdict(self.watcher),
        }


def _resolve_path(override: Path | str | None, raw: dict[str, object], key: str, default: Path) -> Path:
    raw_path: Path | None = None
    if key in raw:
        raw_val = raw[key]
        if not isinstance(raw_val, str) or not raw_val.strip():
            raise ValueError(f"Config '{key}' must be a non-empty string, got {raw_val!r}")
        raw_path = Path(raw_val).expanduser()
    if override is not None:
        return _require_path(override, f"Path override for '{key}'")
    return raw_path if raw_path is not None else default


def load_config(
    config_path: Path | str | None = None,
    *,
    data_dir: Path | str | None = None,
    archive_dir: Path | str | None = None,
    vault_dir: Path | str | None = None,
) -> VaultConfig:
    """Load VaultConfig from JSON file (if present) overlaid with explicit overrides."""
    base = default_home_dir()
    cfg_path = _require_path(config_path, "config_path") if config_path is not None else default_config_path()

    raw: dict[str, object] = {}
    if cfg_path.is_file():
        loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Config file must contain a JSON object: {cfg_path}")
        raw = loaded

    ret_raw = raw.get("retention", {})
    if not isinstance(ret_raw, dict):
        raise ValueError(f"Config section 'retention' must be a JSON object, got {type(ret_raw).__name__}")
    wat_raw = raw.get("watcher", {})
    if not isinstance(wat_raw, dict):
        raise ValueError(f"Config section 'watcher' must be a JSON object, got {type(wat_raw).__name__}")

    return VaultConfig(
        data_dir=_resolve_path(data_dir, raw, "data_dir", Path.home() / ".gemini" / "antigravity"),
        archive_dir=_resolve_path(archive_dir, raw, "archive_dir", base / "archive"),
        vault_dir=_resolve_path(vault_dir, raw, "vault_dir", base / "vault"),
        log_file=_resolve_path(None, raw, "log_file", base / "session-vault.log"),
        lock_dir=_resolve_path(None, raw, "lock_dir", base / "locks"),
        retention=RetentionPolicy(
            target_max=ret_raw.get("target_max", 420),
            root_active_days=ret_raw.get("root_active_days", 2),
            keep_idle_days=ret_raw.get("keep_idle_days", 3),
            recent_touch_guard_sec=ret_raw.get("recent_touch_guard_sec", 300),
        ),
        watcher=WatcherSettings(
            shield_interval_sec=wat_raw.get("shield_interval_sec", 120),
            backup_interval_sec=wat_raw.get("backup_interval_sec", 1800),
        ),
    )


def build_logger(log_file: Path, *, verbose_console: bool = False) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("session_vault")
    log.setLevel(logging.INFO)
    for h in list(log.handlers):
        try:
            h.close()
        except OSError:
            pass
    log.handlers.clear()
    fh = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024, backupCount=1, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)
    if verbose_console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(sh)
    return log
