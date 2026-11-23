"""Command-line interface for Antigravity Session Vault."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
from pathlib import Path
import sys

from . import __version__
from .archive import plan_clean, run_clean
from .config import VaultConfig, build_logger, default_config_path, load_config
from .platform_ops import InstanceLock
from .shield import run_shield
from .store import PRUNE_CAP, atomic_write_bytes, load_state, simulate_prune500
from .vault import count_restorable, list_restorable, run_backup, run_restore, search_archive
from .watcher import ensure_watcher, run_watch


def format_status(cfg: VaultConfig) -> str:
    state = load_state(cfg)
    evict_total, at_risk_prot = simulate_prune500(state)
    killed = sum(1 for r in state.subagents.values() if r.killed == 1)
    idle_alive = sum(
        1 for r in state.subagents.values() if r.killed == 0 and r.status == "CASCADE_RUN_STATUS_IDLE"
    )
    active_sub = len(state.subagents) - killed - idle_alive
    restorable = count_restorable(cfg)
    watcher_active = InstanceLock("watch", cfg.lock_dir).is_held()
    lines = [
        f"Session Vault:  v{__version__} (watcher={'running' if watcher_active else 'stopped'})",
        f"Data dir:       {cfg.data_dir}",
        f"Archive dir:    {cfg.archive_dir}",
        f"Vault dir:      {cfg.vault_dir}",
        f"Active DBs:     {len(state.disk_dbs)} / {PRUNE_CAP} (excess={max(0, len(state.disk_dbs) - PRUNE_CAP)})",
        f"  Protected:    {len(state.protected_on_disk)} (pinned={len(state.pinned_ids & set(state.disk_dbs))}, side_questions={len(state.side_question_ids & set(state.disk_dbs))})",
        f"  Subagents:    {len(state.subagents)} (killed={killed}, idle_alive={idle_alive}, active={active_sub})",
        f"PruneAll(500):  would evict {evict_total} DBs, at-risk protected={len(at_risk_prot)}",
        f"Ghost orphans:  named_roots={len(state.named_root_orphans)}, child={len(state.other_orphans)}, cli_preserved={len(state.cli_orphans)}",
        f"Vault roots:    {restorable['vault_roots']} | Archived subagents: {restorable['archived_subagents']}",
        f"Policy:         target_max={cfg.retention.target_max}, root_active_days={cfg.retention.root_active_days}, keep_idle_days={cfg.retention.keep_idle_days}",
    ]
    for cid in at_risk_prot:
        row = state.summaries.get(cid)
        title = (row.title or "(untitled)") if row is not None else "<unindexed/side-question>"
        lines.append(f"  AT RISK:      {cid[:8]} | {title}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="session-vault",
        description="Antigravity Session Vault — preserve main conversations & auto-archive subagents.",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--config", type=Path, default=None, help="Path to config.json")
    ap.add_argument("--data-dir", type=Path, default=None, help="Override Antigravity data directory")
    ap.add_argument("--archive-dir", type=Path, default=None, help="Override subagent archive directory")
    ap.add_argument("--vault-dir", type=Path, default=None, help="Override main conversation vault directory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show active DB count, PruneAll(500) risk, watcher state, and archive stats")
    sub.add_parser("shield", help="Layer 1: demote completed/idle subagent .db mtimes (safe online)")
    sub.add_parser("backup", help="Layer 3: WAL-safe incremental backup of protected roots (safe online)")

    p_clean = sub.add_parser("clean", help="Layer 2: offline smart archival + dual-store ghost cleanup")
    p_clean.add_argument("--dry-run", action="store_true")
    p_clean.add_argument("--verbose", action="store_true", help="Include offload_ids list in --dry-run output")
    p_clean.add_argument("--target-max", type=int, default=None)
    p_clean.add_argument("--root-active-days", type=int, default=None)
    p_clean.add_argument("--keep-idle-days", type=int, default=None)

    p_watch = sub.add_parser("watch", help="Run lifecycle watcher (shield + backup online, clean on exit)")
    p_watch.add_argument("--shield-sec", type=int, default=None)
    p_watch.add_argument("--backup-sec", type=int, default=None)
    p_watch.add_argument("--exit-after-clean", action="store_true")

    p_ens = sub.add_parser("ensure", help="Start detached lifecycle watcher if not already running")
    p_ens.add_argument("--force", action="store_true", help="Spawn even if Desktop LS is not currently running")

    p_search = sub.add_parser("search", help="Search archived subagents via index.jsonl (safe online)")
    p_search.add_argument("query", nargs="?", default="")
    p_search.add_argument("--root", default="", help="Filter by root conversation ID or prefix")
    p_search.add_argument("--limit", type=int, default=20)

    p_rest = sub.add_parser("restore", help="Restore a vaulted root or archived subagent (offline only)")
    p_rest.add_argument("uuid", nargs="?")
    p_rest.add_argument("--list", action="store_true")

    p_cfg = sub.add_parser("config", help="Show resolved configuration (or write default config.json)")
    p_cfg.add_argument("--init", action="store_true", help="Write resolved config to config.json")
    return ap


def _replace_non_none(obj: object, **overrides: object) -> object:
    active = {k: v for k, v in overrides.items() if v is not None}
    return dataclasses.replace(obj, **active) if active else obj


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config(
            args.config,
            data_dir=args.data_dir,
            archive_dir=args.archive_dir,
            vault_dir=args.vault_dir,
        )
        # Lazy logger: read-only commands never touch the filesystem or create log dirs.
        def get_log(*, verbose: bool = True) -> logging.Logger:
            return build_logger(cfg.log_file, verbose_console=verbose)

        if args.cmd == "status":
            print(format_status(cfg))
        elif args.cmd == "shield":
            print(json.dumps(run_shield(cfg, get_log()), indent=2))
        elif args.cmd == "backup":
            print(json.dumps(run_backup(cfg, get_log()), indent=2))
        elif args.cmd == "clean":
            pol = _replace_non_none(
                cfg.retention,
                target_max=args.target_max,
                root_active_days=args.root_active_days,
                keep_idle_days=args.keep_idle_days,
            )
            res = (
                plan_clean(cfg, pol, include_ids=args.verbose)
                if args.dry_run
                else run_clean(cfg, get_log(), policy=pol)
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif args.cmd == "watch":
            wat = _replace_non_none(
                cfg.watcher,
                shield_interval_sec=args.shield_sec,
                backup_interval_sec=args.backup_sec,
            )
            run_watch(
                cfg,
                get_log(verbose=False),
                shield_sec=wat.shield_interval_sec,
                backup_sec=wat.backup_interval_sec,
                exit_after_clean=args.exit_after_clean,
            )
        elif args.cmd == "ensure":
            res = ensure_watcher(
                cfg,
                config_path=args.config,
                data_dir=args.data_dir,
                archive_dir=args.archive_dir,
                vault_dir=args.vault_dir,
                require_desktop_running=not args.force,
            )
            if res.get("status") == "spawned":
                get_log(verbose=False).info("ensure: spawned detached watcher pid=%s", res.get("pid"))
            print(json.dumps(res, indent=2))
        elif args.cmd == "search":
            hits = [
                h if h.get("root_title") else {**h, "root_title": "(untitled)"}
                for h in search_archive(cfg, args.query, root_id=args.root, limit=args.limit)
            ]
            print(json.dumps(hits, ensure_ascii=False, indent=2))
        elif args.cmd == "restore":
            if args.list or not args.uuid:
                print(json.dumps(list_restorable(cfg), ensure_ascii=False, indent=2))
            else:
                print(json.dumps(run_restore(cfg, args.uuid, get_log()), ensure_ascii=False, indent=2))
        elif args.cmd == "config":
            target = (args.config or default_config_path()).expanduser()
            if args.init:
                atomic_write_bytes(
                    target,
                    (json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                )
                get_log(verbose=False).info("config: initialized %s", target)
                print(json.dumps({"written": str(target), **cfg.to_dict()}, ensure_ascii=False, indent=2))
            else:
                print(json.dumps({"config_path": str(target), **cfg.to_dict()}, ensure_ascii=False, indent=2))
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0
