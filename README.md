# Antigravity Session Vault

> **Keep your Antigravity Desktop main conversations forever while automatically archiving older Swarm & subagent sessions into a searchable cold archive.**

[中文文档](./README_ZH.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Dependencies: Zero](https://img.shields.io/badge/Dependencies-Zero%20(stdlib)-22C55E.svg)](#)
[![Platforms](https://img.shields.io/badge/Platforms-Windows%20%7C%20macOS%20%7C%20Linux-64748B.svg)](#)

---

## Why This Plugin Exists

Antigravity Desktop runs a hardcoded rolling retention pass (`PruneAll(500)`) on startup over `~/.gemini/antigravity/conversations/`. Every Swarm worker and subagent shares that flat 500-session quota alongside your primary conversations.

When you run heavy multi-agent workflows:
1. Hundreds of short-lived Swarm/subagent `.db` files push the total past 500.
2. On the next launch, `language_server` sorts `.db` files by filesystem `mtime` and deletes the oldest entries — frequently **evicting your older primary conversations** and their `brain/<id>/` artifacts.
3. Because `PruneAll(500)` does not prune `agyhub_summaries_proto.pb` or `conversation_summaries.db`, deleted conversations remain in the sidebar as **infinite-spinner ghost cards**.

**Antigravity Session Vault** solves this cleanly as a **native Antigravity plugin** — zero binary patching, zero third-party dependencies, and zero resident background processes when Antigravity Desktop is closed.

---

## How It Works

### Lifecycle-Bound Architecture ("Zero Footprint When Idle")

1. **Wake via Native Hooks (`hooks.json`)**: On `SessionStart` and `PreInvocation`, a fast hook checks a kernel singleton lock (<2ms when active) and spawns a single detached lifecycle watcher (`watch --exit-after-clean`) immune to parent Job Objects and `taskkill /T /F`.
2. **While Antigravity Runs**:
   - **Layer 1 — Online Priority Shield (every 2m)**: Demotes completed/idle subagent `.db` `mtime`s to `2010`/`2015` (invisible in the UI, which sorts by Protobuf `lastModifiedTime`), guaranteeing even a power loss or crash restart never evicts a primary conversation ahead of subagents.
   - **Layer 3 — WAL-Safe Incremental Vault (every 30m)**: Snapshots all protected main conversations, Side Questions, annotations, and `brain/` trees using SQLite's online `backup()` API + `PRAGMA quick_check`.
3. **Immediately After Antigravity Exits**:
   - **Layer 2 — Offline Smart Archival & Ghost Cleanup (<1.5s)**: Passes a fail-closed process & lock gate, skips inline crash/self-restarts, offloads eligible older subagents losslessly into `<archive_dir>/subagents/<cid>/`, updates `<archive_dir>/subagents/index.jsonl`, and atomically cleans purged/ghost entries from `conversation_summaries.db` (including `conversations_fts` integrity-check) and `agyhub_summaries_proto.pb` (preserving CLI rows and untouched fields byte-for-byte).
   - **Self-Terminates**: Once offline cleanup finishes, the watcher exits cleanly — leaving **0 background processes** until you next open Antigravity.

### Clean 4-Layer Architecture

The plugin is structured as a strict, zero-dependency 4-layer package (`session_vault/` + `scripts/hook.py`) with no circular imports:

- **Layer 0 (`config.py`, `proto.py`, `platform_ops.py`)**: Strict config loading & logger setup, bounds-checked byte-preserving Protobuf wire codec, per-directory kernel singleton locks, and Job-Object-safe detached process spawning.
- **Layer 1 (`store.py`)**: Atomic file writes, `index.jsonl` I/O, rolling metadata snapshots, 8-category conversation classifier, `PruneAll(500)` risk simulator, and fail-closed offline gate.
- **Layer 2 (`shield.py`, `archive.py`, `vault.py`)**: Independent retention layers — online `mtime` shield, offline 4-tier smart archival + dual-store cleanup, and incremental WAL-safe root backup, archive search, and offline restore.
- **Layer 3 (`watcher.py`, `cli.py`, `scripts/hook.py`)**: Event-driven lifecycle watcher (lazy-loads heavy modules only after acquiring its lock), read-only zero-log CLI queries, and a fast-path hook entrypoint that always emits `{}`.

### Smart 4-Tier Retention Policy

| Tier | Scope | Action |
|---|---|---|
| **Priority 0 (Never Archived)** | Main conversations, Side Questions (`conversation_parents` + `annotations/*.pbtxt`), pinned/annotated chats, unindexed new chats, any running/non-idle task, anything touched within 5 min (`recent_touch_guard_sec = 300`) | Always kept in place & backed up incrementally to the Vault |
| **Priority 1 (Hot Subagents)** | Non-killed (`killed == 0`) subagents of roots active within **2 days**, plus non-killed subagents active within **3 days** | Kept in place unless Priority 0 + Priority 1 alone exceed `target_max` |
| **Priority 2 (Normal Offload)** | Completed/inactive subagents of roots inactive for **> 2 days** | Losslessly archived oldest-first on exit |
| **Safety Valve (`target_max = 420`)** | Only if active DBs still exceed **420** (80-slot safety buffer below 500) | Archives the **oldest** `killed == 1` subagents of recent roots first, leaving your most recent Swarm/subagent sessions in place |

Every archived subagent remains **instantly readable online** without restarting Antigravity via `index.jsonl` and the bundled `rules/AGENTS.md` instruction, or can be restored back into the Desktop UI offline via `restore <uuid>`.

---

## Installation

### Requirements
- **Antigravity Desktop 2.0+**
- **Python 3.10+** (standard library only — zero `pip install` dependencies)

### Quick Install

Clone or copy this repository directly into Antigravity's native plugins folder:

```bash
# macOS / Linux
git clone https://github.com/jack-zihan/antigravity-session-vault.git \
  ~/.gemini/config/plugins/antigravity-session-vault
```

```powershell
# Windows (PowerShell)
git clone https://github.com/jack-zihan/antigravity-session-vault.git `
  "$env:USERPROFILE\.gemini\config\plugins\antigravity-session-vault"
```

Verify discovery with the Antigravity CLI (or check **Settings → Plugins** in Antigravity Desktop):

```bash
agy plugin list
```

Enable/disable anytime from Antigravity's Plugins UI — disabling the plugin cleanly turns off its hooks, rules, and skill in one click.

---

## Configuration

Session Vault works out of the box with zero configuration, storing data under `~/.gemini/antigravity-session-vault/`.

To customize storage paths (e.g., placing your Root Vault on a secondary drive) or retention thresholds, generate and edit `~/.gemini/antigravity-session-vault/config.json` (or set `SESSION_VAULT_CONFIG` to a custom JSON path):

```bash
python3 -B -m session_vault config --init   # Windows: python -B -m session_vault config --init
```

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

Configuration is Fail-Fast validated at load time: booleans in integer fields, out-of-range thresholds, non-object sections, and empty path overrides raise `ValueError` immediately while `_sec` timing keys remain backward-compatible.

---

## CLI Reference

Run from the plugin directory (or install optionally with `pip install -e .` to get the global `session-vault` command). Global flags supported by all subcommands: `--config <path>`, `--data-dir <path>`, `--archive-dir <path>`, `--vault-dir <path>`, `--version`. Read-only commands (`status`, `search`, `clean --dry-run`, `restore --list`, `config` without `--init`) produce **zero** log-file or log-directory side effects.

| Command | Safe Online? | Description |
|---|---|---|
| `python -B -m session_vault status` | Yes (read-only) | Active DB count, `PruneAll(500)` risk simulation, watcher state, ghost count, vault stats |
| `python -B -m session_vault search [query] [--root <id>] [--limit N]` | Yes (read-only) | Search archived subagents via `index.jsonl` and print direct `transcript.jsonl` paths |
| `python -B -m session_vault clean --dry-run [--verbose] [--target-max N] [--root-active-days N] [--keep-idle-days N]` | Yes (read-only) | Preview a concise summary of what the next offline clean will archive and purge (`--verbose` includes full `offload_ids`) |
| `python -B -m session_vault shield` | Yes | Run Layer 1 online `mtime` priority shield immediately |
| `python -B -m session_vault backup` | Yes | Run Layer 3 WAL-safe incremental backup of all protected roots |
| `python -B -m session_vault ensure [--force]` | Yes | Ensure the detached lifecycle watcher is running (`--force` spawns even if Desktop LS is offline) |
| `python -B -m session_vault watch [--shield-sec N] [--backup-sec N] [--exit-after-clean]` | Yes | Run the lifecycle watcher in the foreground (shield + backup online, offline clean on exit) |
| `python -B -m session_vault clean [--target-max N] [--root-active-days N] [--keep-idle-days N]` | **Offline only** | Execute smart archival & dual-store ghost cleanup (blocked if Desktop is running) |
| `python -B -m session_vault restore --list` | Yes (read-only) | List all restorable vaulted roots and archived subagents (bare `restore` behaves identically) |
| `python -B -m session_vault restore <uuid>` | **Offline only** | Restore a vaulted root or archived subagent back into Desktop (with automatic meta backup, `< 480` active DB guard, and path-traversal validation) |
| `python -B -m session_vault config [--init]` | Yes (`--init` writes) | Print resolved configuration (`--init` atomically writes `config.json`) |

---

## Development & Tests

```bash
python -B -m unittest discover -s tests -v
```

The test suite runs in isolated temporary directories and completes in seconds:
- Strict config validation and byte-preserving Protobuf wire codec (preserving Field 2 / CLI entries verbatim)
- 8-category conversation classifier, 4-tier oldest-first retention & `target_max` safety valve, and Layer 1 `mtime` shield
- Dirty WAL recovery, SQLite + FTS5 integrity verification, `PermissionError` atomic rollback, cross-filesystem moves, incremental dirty-skip backup, and offline restore with path-traversal & `< 480` guards
- Concurrent atomic file writes, cross-platform kernel singleton locks, and detached process survival across `KILL_ON_JOB_CLOSE` + `taskkill /T /F` (Windows) / `SIGKILL` (POSIX)
- End-to-end CLI workflows, zero-log read-only queries, lifecycle watcher restart-grace behavior, and fast-path hook execution

---

## License

[MIT](./LICENSE)
