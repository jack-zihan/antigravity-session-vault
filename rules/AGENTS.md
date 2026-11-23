## Archived Subagent & Session Vault Lookup

The `antigravity-session-vault` plugin protects all user main conversations and Side Questions in place, backs them up incrementally to a vault, and automatically offloads older Swarm/subagent sessions to a lossless cold archive when Antigravity Desktop exits (keeping active DB count safely below Desktop's 500-session eviction cap).

When looking up a historical Swarm/subagent conversation ID that is no longer present under `~/.gemini/antigravity/brain/<cid>`:
- **Check the archive index first**: read `<archive_dir>/subagents/index.jsonl` (default `~/.gemini/antigravity-session-vault/archive/subagents/index.jsonl`; if customized, check `archive_dir` in `~/.gemini/antigravity-session-vault/config.json`) or run `python -B -m session_vault search <query>` from the plugin directory.
- **Read archived transcripts directly online (no restart needed)**: each archived subagent preserves its full brain directory at `<archive_dir>/subagents/<cid>/brain/.system_generated/logs/transcript.jsonl` (plus `transcript_full.jsonl` and artifacts).
- **Restore back into Antigravity Desktop UI (offline only)**: close Antigravity Desktop completely, then run `python -B -m session_vault restore <cid>` from the plugin directory.
