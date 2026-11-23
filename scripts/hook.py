"""Fast-path Antigravity lifecycle hook entrypoint for Session Vault.

Invoked by hooks.json on SessionStart and PreInvocation. Must always emit valid
JSON ({}) to stdout quickly and never block the user's conversation turn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

# Reject Python <3.10 so `python3 || python || py -3` falls through cleanly.
if sys.version_info < (3, 10):
    sys.exit(1)

PLUGIN_ROOT = Path(os.environ.get("PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


def _read_hook_payload() -> dict[str, object]:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def main() -> int:
    payload = _read_hook_payload()
    transcript = str(payload.get("transcriptPath") or "").replace("\\", "/").lower()
    # Scope automatic watcher activation to Antigravity Desktop 2.0 (~/.gemini/antigravity/).
    if "/antigravity-cli/" in transcript or "/antigravity-ide/" in transcript:
        print("{}")
        return 0

    cfg = None
    try:
        from session_vault.config import build_logger, load_config
        from session_vault.watcher import ensure_watcher

        cfg = load_config()
        res = ensure_watcher(cfg, plugin_root=PLUGIN_ROOT, require_desktop_running=True)
        if res.get("status") == "spawned":
            log = build_logger(cfg.log_file, verbose_console=False)
            log.info("hook (%s): spawned detached watcher pid=%s", payload.get("hookEventName", "unknown"), res.get("pid"))
    except Exception as exc:
        # Never break the agent's conversation turn on a hook error; record it to log.
        try:
            from session_vault.config import build_logger, default_config_path

            log_path = cfg.log_file if cfg is not None else default_config_path().parent / "session-vault.log"
            log = build_logger(log_path, verbose_console=False)
            log.exception("hook error: %s", exc)
        except Exception:
            pass

    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
