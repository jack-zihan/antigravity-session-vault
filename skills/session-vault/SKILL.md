---
name: session-vault
description: Inspect Antigravity Session Vault status, search archived Swarm and subagent sessions online, configure retention thresholds or vault paths, and restore vaulted or archived conversations offline. Use when the user asks about missing chats, the 500 conversation cap, archived subagents, vault backups, or restoring old sessions.
license: MIT
---

# Session Vault

Manage Antigravity Desktop's conversation retention, cold subagent archive, and incremental root vault.

## When to Use

- Checking active conversation count vs. Antigravity Desktop's hardcoded `PruneAll(500)` cap.
- Checking whether the lifecycle watcher is active or previewing what the next offline clean will archive.
- Searching archived Swarm/subagent sessions and reading their transcripts directly (online, no restart required).
- Configuring retention thresholds (`target_max`, `root_active_days`, `keep_idle_days`, `recent_touch_guard_sec`) or custom `archive_dir` / `vault_dir` / `lock_dir` / `log_file` paths.
- Restoring a vaulted main conversation or archived subagent back into the Desktop sidebar (requires Desktop to be closed).

## How to Run Commands

All commands use Python 3.10+ standard library only (zero third-party dependencies). Run them with the plugin directory as working directory (or `PYTHONPATH` pointing to the plugin directory, typically `~/.gemini/config/plugins/antigravity-session-vault`). Global overrides (`--config <path>`, `--data-dir <path>`, `--archive-dir <path>`, `--vault-dir <path>`, `--version`, or `SESSION_VAULT_CONFIG` env var) are supported across all subcommands:

```bash
# On macOS / Linux use python3; on Windows use python
python -B -m session_vault status
python -B -m session_vault search "<keyword>" --limit 20
python -B -m session_vault search --root <root-uuid-prefix>
python -B -m session_vault clean --dry-run [--verbose]
python -B -m session_vault shield
python -B -m session_vault backup
python -B -m session_vault ensure [--force]
python -B -m session_vault watch [--shield-sec 120] [--backup-sec 1800] [--exit-after-clean]
python -B -m session_vault clean [--target-max 420] [--root-active-days 2] [--keep-idle-days 3]
python -B -m session_vault restore --list
python -B -m session_vault restore <uuid>
python -B -m session_vault config
python -B -m session_vault config --init
```

## Safe Online vs. Offline Operations

- **Read-only (safe online, zero log-file/dir side effects)**: `status`, `search`, `clean --dry-run` (uses `plan_clean` with no `InstanceLock` or offline gate), `restore --list` (or bare `restore`), `config` (without `--init`).
- **Online mutating / daemon**: `shield`, `backup`, `ensure`, `watch`, `config --init` (atomic write).
- **Offline only (Desktop must be fully closed)**: `clean` (without `--dry-run`) and `restore <uuid>`. Both acquire `InstanceLock("op", lock_dir)`, enforce a fail-closed process + file-lock gate, and automatically snapshot `conversation_summaries.db` (plus `-wal` and `-shm`) and `agyhub_summaries_proto.pb` before editing. `restore <uuid>` also enforces the `< 480` active DB safety guard, rejects invalid/path-traversal `cid` values, and keeps root `chapter_titles/<cid>.pb` separate from subagent companion `conversations/<cid>.pb`.

## Configuration

User overrides live in `~/.gemini/antigravity-session-vault/config.json` (or `SESSION_VAULT_CONFIG`; run `python -B -m session_vault config --init` to generate a starter file):

```json
{
  "data_dir": "~/.gemini/antigravity",
  "archive_dir": "~/.gemini/antigravity-session-vault/archive",
  "vault_dir": "~/.gemini/antigravity-session-vault/vault",
  "log_file": "~/.gemini/antigravity-session-vault/session-vault.log",
  "lock_dir": "~/.gemini/antigravity-session-vault/locks",
  "retention": {
    "target_max": 420,
    "root_active_days": 2,
    "keep_idle_days": 3,
    "recent_touch_guard_sec": 300
  },
  "watcher": {
    "shield_interval_sec": 120,
    "backup_interval_sec": 1800
  }
}
```

`load_config` applies Fail-Fast validation on startup: rejecting booleans in integer fields, out-of-range thresholds, non-object sections, and empty path values while preserving `_sec` timing keys.
