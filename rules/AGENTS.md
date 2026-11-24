## Archived Subagent & Session Vault Lookup

The `antigravity-session-vault` plugin protects all user main conversations and Side Questions in place, backs them up incrementally to a vault, and offloads Swarm/subagent sessions by user importance (`keep_roots` / `archive_roots` / `keep_subagents`) with oldest-first fallback when Antigravity Desktop exits.

When looking up a historical Swarm/subagent conversation ID missing from `~/.gemini/antigravity/brain/<cid>`:
- **Check the archive index first**: read `<archive_dir>/subagents/index.jsonl` (default `~/.gemini/antigravity-session-vault/archive/subagents/index.jsonl`; if customized, check `archive_dir` in `~/.gemini/antigravity-session-vault/config.json`) or run `python -B -m session_vault search <query>` from the plugin directory.
- **Read archived transcripts directly online (no restart needed)**: each archived subagent preserves its full brain directory at `<archive_dir>/subagents/<cid>/brain/.system_generated/logs/transcript.jsonl` (plus `transcript_full.jsonl` and artifacts).
- **Curate keep/archive priorities online**: run `python -B -m session_vault trees` to review subagent usage per main conversation and `python -B -m session_vault policy keep|archive|auto <id>` to update preferences (hot-reloaded before the next offline clean).
- **Restore back into Antigravity Desktop UI (offline only)**: close Antigravity Desktop completely, then run `python -B -m session_vault restore <cid>` (automatically pins restored subagents into `keep_subagents`).
