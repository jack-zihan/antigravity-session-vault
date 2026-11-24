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
from .config import RetentionPolicy, VaultConfig, build_logger, default_config_path, load_config
from .platform_ops import InstanceLock
from .shield import run_shield
from .store import (
    PRUNE_CAP,
    load_state,
    read_index_entries,
    resolve_id_prefix,
    save_config,
    simulate_prune500,
    summarize_trees,
)
from .vault import (
    count_restorable,
    discard_archived,
    list_restorable,
    run_backup,
    run_restore,
    search_archive,
)
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
    pol = cfg.retention
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
        f"Policy:         target_max={pol.target_max}, root_active_days={pol.root_active_days}, keep_idle_days={pol.keep_idle_days} | keep_roots={len(pol.keep_roots)}, archive_roots={len(pol.archive_roots)}, keep_subagents={len(pol.keep_subagents)}",
    ]
    for cid in at_risk_prot:
        row = state.summaries.get(cid)
        title = (row.title or "(untitled)") if row is not None else "<unindexed/side-question>"
        lines.append(f"  AT RISK:      {cid[:8]} | {title}")
    return "\n".join(lines)


def format_trees(rows: list[dict[str, object]], *, limit: int = 30) -> str:
    if not rows:
        return "No active or archived subagent trees found."
    shown = rows[:limit] if limit > 0 else rows
    header = f"{'Root ID':8s}  {'Policy':7s}  {'Active (K/A)':14s}  {'Arch':>4s}  {'Size':>7s}  {'Modified':10s}  Title"
    lines = [header, "-" * len(header)]
    for r in shown:
        rid = str(r["root_id"])[:8]
        pol = str(r["policy"])
        act = f"{r['active_subagents']} ({r['killed']}/{r['alive']})"
        mb = f"{int(r['active_bytes']) / (1024 * 1024):.1f}MB"
        mod = str(r["last_modified_time"])[:10] or "-"
        title = str(r["title"]) or "<orphan/workflow>"
        lines.append(f"{rid:8s}  {pol:7s}  {act:14s}  {int(r['archived_subagents']):4d}  {mb:>7s}  {mod:10s}  {title}")
    if limit > 0 and len(rows) > limit:
        lines.append(f"... ({len(rows) - limit} more; use --limit 0 or --json to view all)")
    return "\n".join(lines)


def _apply_policy_action(
    cfg: VaultConfig,
    action: str,
    raw_ids: list[str],
    *,
    is_subagent: bool,
) -> tuple[RetentionPolicy, list[str]]:
    state = load_state(cfg)
    index_entries = read_index_entries(cfg.subagent_index_path)
    candidates: set[str] = (
        set(state.disk_dbs)
        | set(state.summaries)
        | set(cfg.retention.keep_roots)
        | set(cfg.retention.archive_roots)
        | set(cfg.retention.keep_subagents)
        | {str(e.get("cid") or "") for e in index_entries}
        | {str(e.get("root_id") or "") for e in index_entries}
    )
    candidates.discard("")
    resolved = [resolve_id_prefix(raw, candidates) for raw in raw_ids]

    keep_r = list(cfg.retention.keep_roots)
    arch_r = list(cfg.retention.archive_roots)
    keep_s = list(cfg.retention.keep_subagents)

    for cid in resolved:
        if is_subagent:
            if action == "keep":
                if cid not in keep_s:
                    keep_s.append(cid)
            elif action == "auto":
                keep_s = [x for x in keep_s if x != cid]
            else:
                raise ValueError("--subagent only supports 'keep' or 'auto'")
        elif action == "keep":
            arch_r = [x for x in arch_r if x != cid]
            if cid not in keep_r:
                keep_r.append(cid)
        elif action == "archive":
            keep_r = [x for x in keep_r if x != cid]
            if cid not in arch_r:
                arch_r.append(cid)
        elif action == "auto":
            keep_r = [x for x in keep_r if x != cid]
            arch_r = [x for x in arch_r if x != cid]
            keep_s = [x for x in keep_s if x != cid]

    new_pol = dataclasses.replace(
        cfg.retention,
        keep_roots=tuple(keep_r),
        archive_roots=tuple(arch_r),
        keep_subagents=tuple(keep_s),
    )
    return new_pol, resolved


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="session-vault",
        description="Antigravity Session Vault — preserve main conversations & curate subagent archives.",
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

    p_trees = sub.add_parser("trees", help="Inspect active/archived subagents grouped by root conversation")
    p_trees.add_argument("--json", action="store_true", dest="as_json")
    p_trees.add_argument("--limit", type=int, default=30)

    p_pol = sub.add_parser("policy", help="Inspect or update per-root/subagent retention rules (keep/archive/auto)")
    p_pol.add_argument("action", nargs="?", choices=("list", "keep", "archive", "auto"), default="list")
    p_pol.add_argument("ids", nargs="*", help="Conversation UUIDs or unique prefixes")
    p_pol.add_argument("--subagent", action="store_true", help="Target individual subagent IDs (keep_subagents)")

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

    p_disc = sub.add_parser("discard", help="Explicitly delete unwanted cold-archived subagents (dry-run without --yes)")
    p_disc.add_argument("cids", nargs="*", help="Specific archived subagent UUIDs")
    p_disc.add_argument("--root", default="", help="Discard all archived subagents belonging to this root ID/prefix")
    p_disc.add_argument("--yes", action="store_true", help="Confirm permanent deletion from cold archive")

    p_rest = sub.add_parser("restore", help="Restore a vaulted root or archived subagent (offline only)")
    p_rest.add_argument("uuid", nargs="?")
    p_rest.add_argument("--list", action="store_true")
    p_rest.add_argument("--no-pin", action="store_true", help="Do not add restored subagent to keep_subagents")

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
        elif args.cmd == "trees":
            rows = summarize_trees(cfg)
            print(json.dumps(rows, ensure_ascii=False, indent=2) if args.as_json else format_trees(rows, limit=args.limit))
        elif args.cmd == "policy":
            if args.action == "list":
                print(json.dumps(cfg.retention.to_dict(), ensure_ascii=False, indent=2))
            else:
                if not args.ids:
                    raise ValueError(f"'policy {args.action}' requires at least one conversation ID or prefix")
                new_pol, resolved = _apply_policy_action(cfg, args.action, args.ids, is_subagent=args.subagent)
                target = save_config(dataclasses.replace(cfg, retention=new_pol), args.config)
                get_log(verbose=False).info("policy %s (%s): %s", args.action, "subagent" if args.subagent else "root", resolved)
                print(json.dumps({"written": str(target), "updated": resolved, "retention": new_pol.to_dict()}, ensure_ascii=False, indent=2))
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
                config_path=args.config,
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
        elif args.cmd == "discard":
            res = discard_archived(
                cfg,
                root_id=args.root,
                cids=tuple(args.cids),
                confirm=args.yes,
                log=get_log() if args.yes else None,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif args.cmd == "restore":
            if args.list or not args.uuid:
                print(json.dumps(list_restorable(cfg), ensure_ascii=False, indent=2))
            else:
                print(
                    json.dumps(
                        run_restore(cfg, args.uuid, get_log(), config_path=args.config, pin_subagent=not args.no_pin),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
        elif args.cmd == "config":
            target = (args.config or default_config_path()).expanduser()
            if args.init:
                written = save_config(cfg, target)
                get_log(verbose=False).info("config: initialized %s", written)
                print(json.dumps({"written": str(written), **cfg.to_dict()}, ensure_ascii=False, indent=2))
            else:
                print(json.dumps({"config_path": str(target), **cfg.to_dict()}, ensure_ascii=False, indent=2))
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0
