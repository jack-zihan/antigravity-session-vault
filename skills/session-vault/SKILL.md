---
name: session-vault
description: Inspect Antigravity Session Vault status, review subagent trees per main conversation, curate keep/archive retention rules by importance, search or discard cold archives online, and restore vaulted or archived conversations offline. Use when the user asks about missing chats, the 500 conversation cap, keeping important Swarm/Teamwork trees, archiving or discarding old subagents, or restoring sessions.
license: MIT
---

# Session Vault

Manage Antigravity Desktop's conversation retention, importance-based subagent curation, cold archive, and incremental root vault.

## When to Use

- Auditing which main conversations hold the most Swarm/subagent sessions (`trees`) and curating which trees to keep in place vs. archive first (`policy`).
- Checking active conversation count vs. Antigravity Desktop's hardcoded `PruneAll(500)` cap (`status`, `clean --dry-run`).
- Searching archived Swarm/subagent sessions and reading their transcripts directly online (`search`).
- Explicitly discarding unwanted cold-archived subagents to reclaim disk space (`discard`).
- Restoring a vaulted main conversation or archived subagent back into Desktop offline (`restore`).

## Conversational Curation Workflow

When the user wants to decide which Swarm/Teamwork trees to keep vs. archive:
1. Run `python -B -m session_vault trees` (read-only) to show each root conversation's active/archived subagent count, size, last active date, and current tag (`keep` / `archive` / `auto`).
2. Review the table with the user and apply their choices with 8-char ID prefixes:
   - `python -B -m session_vault policy keep <root_id...>` — keep this root's subagent tree hot in place (overrides the 2-day timeout; only earliest `killed=1` overflow if `keep_roots` alone exceed `target_max`).
   - `python -B -m session_vault policy archive <root_id...>` — offload this root's idle/completed subagents first on exit.
   - `python -B -m session_vault policy auto <id...>` — revert back to default time-based rules.
   - `python -B -m session_vault policy keep --subagent <cid>` — lock an individual subagent in place.
3. Verify the updated plan with `python -B -m session_vault clean --dry-run`. The running watcher automatically hot-reloads `config.json` before offline clean.

## Command Reference

Run with the plugin directory as working directory (`~/.gemini/config/plugins/antigravity-session-vault`; use `python3` on macOS/Linux, `python` on Windows). Global flags: `--config`, `--data-dir`, `--archive-dir`, `--vault-dir`, `--version`.

```bash
python -B -m session_vault status
python -B -m session_vault trees [--json] [--limit 30]
python -B -m session_vault policy [list | keep <id...> | archive <id...> | auto <id...>] [--subagent]
python -B -m session_vault search "<keyword>" [--root <prefix>] [--limit 20]
python -B -m session_vault clean --dry-run [--verbose]
python -B -m session_vault discard [--root <prefix> | <cid...>] [--yes]
python -B -m session_vault shield
python -B -m session_vault backup
python -B -m session_vault ensure [--force]
python -B -m session_vault clean [--target-max 420] [--root-active-days 2] [--keep-idle-days 3]
python -B -m session_vault restore --list
python -B -m session_vault restore <uuid> [--no-pin]
python -B -m session_vault config [--init]
```

## Safe Online vs. Offline Operations

- **Read-only (safe online, zero log side effects)**: `status`, `trees`, `policy` (without write action), `search`, `clean --dry-run`, `discard` (without `--yes`), `restore --list`, `config` (without `--init`).
- **Online mutating**: `policy keep|archive|auto`, `discard --yes` (deletes ONLY from cold `<archive_dir>/subagents/`, never live Desktop DBs or Root Vault), `shield`, `backup`, `ensure`, `watch`, `config --init`.
- **Offline only (Desktop must be closed)**: `clean` and `restore <uuid>`. Restored subagents are automatically pinned in `keep_subagents` (unless `--no-pin` is passed) so they are not re-archived on the next quit.
