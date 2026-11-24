"""Layer 2: Offline importance-first subagent archival + index.jsonl + dual-store ghost cleanup."""

from __future__ import annotations

import dataclasses
import datetime
import errno
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import time

from .config import RetentionPolicy, VaultConfig
from .platform_ops import InstanceLock
from .proto import filter_summaries_proto
from .store import (
    StateSnapshot,
    SummaryRow,
    atomic_write_bytes,
    load_state,
    parse_iso_ts,
    read_index_entries,
    resolve_root_id,
    rewrite_index_entries,
    snapshot_meta_files,
    verify_offline_gate,
)


@dataclasses.dataclass(frozen=True)
class OffloadSelection:
    """Ordered buckets of subagent IDs selected for offline archival."""

    explicit_archive: list[str]
    auto_normal: list[str]
    auto_overflow_killed: list[str]
    auto_overflow_alive: list[str]
    keep_overflow: list[str]

    @property
    def all_ids(self) -> list[str]:
        return (
            self.explicit_archive
            + self.auto_normal
            + self.auto_overflow_killed
            + self.auto_overflow_alive
            + self.keep_overflow
        )


def select_offload_subagents(
    state: StateSnapshot,
    policy: RetentionPolicy | None = None,
) -> OffloadSelection:
    """Select subagents to archive: user importance rules first, oldest-first time fallback.

    Never offloads:
      - Protected main conversations, side questions, pinned/annotated chats, unindexed DBs
      - Any running / non-idle subagent or one modified within recent_touch_guard_sec
      - Any subagent explicitly listed in policy.keep_subagents

    Offload priority (oldest-first within each tier):
      1. Explicit archive: idle/killed subagents of roots in policy.archive_roots
      2. Auto normal: subagents of inactive non-kept roots (>root_active_days)
      3. Safety-valve overflow (only if remaining > target_max):
         3a. oldest killed=1 subagents of active auto roots
         3b. oldest killed=0 idle subagents of auto roots
         3c. last resort: oldest killed=1, then killed=0 idle subagents of policy.keep_roots
    """
    pol = policy or RetentionPolicy()
    now = time.time()
    idle_cutoff = now - pol.keep_idle_days * 86400
    root_cutoff = now - pol.root_active_days * 86400

    keep_roots = set(pol.keep_roots)
    archive_roots = set(pol.archive_roots)
    keep_subagents = set(pol.keep_subagents)

    active_roots = {
        cid
        for cid in state.protected_ids
        if (row := state.summaries.get(cid)) and parse_iso_ts(row.last_modified_time) >= root_cutoff
    }

    explicit_archive: list[tuple[float, str]] = []
    auto_normal: list[tuple[float, str]] = []
    auto_active_killed: list[tuple[float, str]] = []
    auto_alive_idle: list[tuple[float, str]] = []
    keep_killed: list[tuple[float, str]] = []
    keep_alive: list[tuple[float, str]] = []

    for cid, row in state.subagents.items():
        if cid in keep_subagents:
            continue
        if row.not_fully_idle != 0 or row.status != "CASCADE_RUN_STATUS_IDLE":
            continue
        ts = parse_iso_ts(row.last_modified_time)
        if not ts:
            try:
                ts = state.disk_dbs[cid].stat().st_mtime
            except FileNotFoundError:
                continue
        if now - ts < pol.recent_touch_guard_sec:
            continue

        root_id = resolve_root_id(state, cid)
        if root_id in archive_roots:
            explicit_archive.append((ts, cid))
        elif root_id in keep_roots:
            (keep_killed if row.killed == 1 else keep_alive).append((ts, cid))
        else:
            root_is_recent = root_id in active_roots
            if row.killed == 0 and (root_is_recent or ts >= idle_cutoff):
                auto_alive_idle.append((ts, cid))
            elif row.killed == 1 and root_is_recent:
                auto_active_killed.append((ts, cid))
            else:
                auto_normal.append((ts, cid))

    for bucket in (explicit_archive, auto_normal, auto_active_killed, auto_alive_idle, keep_killed, keep_alive):
        bucket.sort()

    t_explicit = [cid for _, cid in explicit_archive]
    t_normal = [cid for _, cid in auto_normal]
    t_auto_killed: list[str] = []
    t_auto_alive: list[str] = []
    t_keep_overflow: list[str] = []

    remaining = len(state.disk_dbs) - len(t_explicit) - len(t_normal)
    if remaining > pol.target_max:
        t_auto_killed = [cid for _, cid in auto_active_killed[: remaining - pol.target_max]]
        remaining -= len(t_auto_killed)
    if remaining > pol.target_max:
        t_auto_alive = [cid for _, cid in auto_alive_idle[: remaining - pol.target_max]]
        remaining -= len(t_auto_alive)
    if remaining > pol.target_max:
        keep_pool = [cid for _, cid in keep_killed] + [cid for _, cid in keep_alive]
        t_keep_overflow = keep_pool[: remaining - pol.target_max]

    return OffloadSelection(
        explicit_archive=t_explicit,
        auto_normal=t_normal,
        auto_overflow_killed=t_auto_killed,
        auto_overflow_alive=t_auto_alive,
        keep_overflow=t_keep_overflow,
    )


def _export_lost_named_chats(cfg: VaultConfig, named_orphans: list[SummaryRow]) -> Path | None:
    out_path = cfg.archive_dir / "lost_named_chats.md"
    if out_path.exists() or not named_orphans:
        return None
    lines = [
        "# Historical Lost Named Conversations (Pre-Vault Evictions)",
        "",
        f"Exported: {datetime.datetime.now().isoformat(timespec='seconds')}",
        "",
        "| Conversation ID | Last Modified | Steps | Title | Workspace |",
        "|---|---|---:|---|---|",
    ]
    for r in sorted(named_orphans, key=lambda x: x.last_modified_time, reverse=True):
        title = r.title.replace("|", "\\|")
        ws = r.workspace_uris.replace("|", "\\|")
        lines.append(f"| `{r.cid}` | {r.last_modified_time[:19]} | {r.step_count} | {title} | {ws} |")
    atomic_write_bytes(out_path, ("\n".join(lines) + "\n").encode("utf-8"))
    return out_path


def _delete_ids_chunked(
    conn: sqlite3.Connection, sql_tpl: str, ids: list[str], dual_param: bool = False
) -> None:
    for i in range(0, len(ids), 400):
        chunk = ids[i : i + 400]
        ph = ",".join("?" * len(chunk))
        conn.execute(sql_tpl.format(ph=ph), chunk + chunk if dual_param else chunk)


def _move_path(src: Path, dst: Path) -> None:
    """Move file or directory atomically on same FS, or copy+remove across filesystems."""
    try:
        os.replace(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
            raise
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        shutil.rmtree(src)
    else:
        tmp = dst.with_name(f"{dst.name}.tmp.{os.getpid()}")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        src.unlink()


def _archive_one_subagent(
    cfg: VaultConfig,
    state: StateSnapshot,
    cid: str,
    sub_archive: Path,
    log: logging.Logger,
) -> dict[str, object] | None:
    """Move one subagent (.db, companion .pb, -wal/-shm, brain/) into archive with rollback on lock."""
    db_path = state.disk_dbs[cid]
    dest = sub_archive / cid
    dest.mkdir(parents=True, exist_ok=True)
    moved_pairs: list[tuple[Path, Path]] = []
    try:
        _move_path(db_path, dest / db_path.name)
        moved_pairs.append((db_path, dest / db_path.name))
        companion_pb = state.disk_companions.get(cid)
        if companion_pb is not None and companion_pb.is_file():
            _move_path(companion_pb, dest / companion_pb.name)
            moved_pairs.append((companion_pb, dest / companion_pb.name))
        for suf in ("-wal", "-shm"):
            aux = db_path.with_name(db_path.name + suf)
            if aux.exists():
                _move_path(aux, dest / aux.name)
                moved_pairs.append((aux, dest / aux.name))
        bdir = cfg.brain_dir / cid
        if bdir.is_dir():
            bdest = dest / "brain"
            if bdest.exists():
                shutil.rmtree(bdest)
            _move_path(bdir, bdest)
            moved_pairs.append((bdir, bdest))
    except PermissionError as exc:
        for orig_p, dst_p in reversed(moved_pairs):
            try:
                _move_path(dst_p, orig_p)
            except OSError:
                pass
        try:
            dest.rmdir()
        except OSError:
            pass
        log.warning("clean: skipped locked subagent %s (%s)", cid, exc)
        return None

    row = state.summaries.get(cid)
    if row and row.raw_summary:
        atomic_write_bytes(dest / "raw_summary.pb", row.raw_summary)
    root_id = resolve_root_id(state, cid)
    root_row = state.summaries.get(root_id)
    tpath = dest / "brain" / ".system_generated" / "logs" / "transcript.jsonl"
    meta: dict[str, object] = {
        "cid": cid,
        "parent_id": row.parent_id if row else "",
        "root_id": root_id,
        "root_title": root_row.title if root_row else "",
        "agent_name": row.agent_name if row else "",
        "killed": row.killed if row else 0,
        "last_modified_time": row.last_modified_time if row else "",
        "archived_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "transcript": str(tpath) if tpath.is_file() else "",
    }
    atomic_write_bytes(dest / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
    return meta


def _purge_summary_db(summary_db: Path, purge_list: list[str], snap_dir: Path) -> None:
    """Remove purged IDs from SQLite summaries/FTS5/parents and verify FTS5 integrity inside tx."""
    conn = sqlite3.connect(summary_db, timeout=10)
    try:
        qc = conn.execute("PRAGMA quick_check").fetchone()[0]
        if qc != "ok":
            raise RuntimeError(f"conversation_summaries.db quick_check failed ({qc}); backup at {snap_dir}")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with conn:
            _delete_ids_chunked(
                conn, "DELETE FROM conversation_summaries WHERE conversation_id IN ({ph})", purge_list
            )
            if "conversations_fts" in tables:
                _delete_ids_chunked(
                    conn, "DELETE FROM conversations_fts WHERE conversation_id IN ({ph})", purge_list
                )
            if "conversation_parents" in tables:
                _delete_ids_chunked(
                    conn,
                    "DELETE FROM conversation_parents WHERE conversation_id IN ({ph}) OR parent_id IN ({ph})",
                    purge_list,
                    dual_param=True,
                )
            if "conversations_fts" in tables:
                # Must execute inside transaction before wal_checkpoint(TRUNCATE) to avoid lock error.
                conn.execute("INSERT INTO conversations_fts(conversations_fts) VALUES('integrity-check')")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def plan_clean(
    cfg: VaultConfig,
    policy: RetentionPolicy | None = None,
    *,
    include_ids: bool = False,
) -> dict[str, object]:
    """Read-only preview of Layer 2 offload and orphan-purge counts (no lock or offline gate)."""
    pol = policy or cfg.retention
    state = load_state(cfg)
    sel = select_offload_subagents(state, pol)
    offload_ids = sel.all_ids
    plan: dict[str, object] = {
        "total_dbs": len(state.disk_dbs),
        "protected_count": len(state.protected_on_disk),
        "offload_count": len(offload_ids),
        "projected_remaining": len(state.disk_dbs) - len(offload_ids),
        "offload_archive_roots": len(sel.explicit_archive),
        "offload_inactive_root_subs": len(sel.auto_normal),
        "offload_active_root_killed_overflow": len(sel.auto_overflow_killed),
        "offload_alive_overflow": len(sel.auto_overflow_alive),
        "offload_keep_roots_overflow": len(sel.keep_overflow),
        "purge_named_orphans": len(state.named_root_orphans),
        "purge_child_orphans": len(state.other_orphans),
        "cli_rows_preserved": len(state.cli_orphans),
        "policy": pol.to_dict(),
    }
    if include_ids:
        plan["offload_ids"] = offload_ids
    return plan


def run_clean(
    cfg: VaultConfig,
    log: logging.Logger,
    policy: RetentionPolicy | None = None,
    *,
    skip_gate: bool = False,
) -> dict[str, object]:
    """Offline Layer 2: offload eligible subagents, update index.jsonl, and purge dual-store ghosts."""
    pol = policy or cfg.retention
    with InstanceLock("op", cfg.lock_dir) as locked:
        if not locked:
            raise RuntimeError("Another session-vault operation is already in progress")
        if not skip_gate:
            gate_ok, gate_msg = verify_offline_gate(cfg)
            if not gate_ok:
                raise RuntimeError(f"Offline gate blocked clean: {gate_msg}")

        state = load_state(cfg)
        offload_ids = select_offload_subagents(state, pol).all_ids
        non_cli_orphans = [r.cid for r in state.named_root_orphans] + [r.cid for r in state.other_orphans]

        if not offload_ids and not non_cli_orphans:
            log.info("clean: nothing to offload or purge (active=%d)", len(state.disk_dbs))
            return {"status": "noop", "active_dbs": len(state.disk_dbs)}

        snap_dir = snapshot_meta_files(cfg, tag="clean")
        manifest = _export_lost_named_chats(cfg, state.named_root_orphans)
        sub_archive = cfg.subagent_archive_dir
        sub_archive.mkdir(parents=True, exist_ok=True)

        moved: set[str] = set()
        index_entries: list[dict[str, object]] = []
        skipped_locked = 0
        for cid in offload_ids:
            meta = _archive_one_subagent(cfg, state, cid, sub_archive, log)
            if meta is None:
                skipped_locked += 1
                continue
            index_entries.append(meta)
            moved.add(cid)

        if index_entries:
            records = {str(r["cid"]): r for r in read_index_entries(cfg.subagent_index_path, log=log) if "cid" in r}
            for rec in index_entries:
                records[str(rec["cid"])] = rec
            rewrite_index_entries(cfg.subagent_index_path, list(records.values()))

        purge_ids = moved | set(non_cli_orphans)
        purge_list = sorted(purge_ids)
        if purge_list and cfg.summary_db.is_file():
            _purge_summary_db(cfg.summary_db, purge_list, snap_dir)

        proto_removed = 0
        if purge_ids and cfg.summary_pb.is_file():
            filtered_pb, proto_removed = filter_summaries_proto(cfg.summary_pb.read_bytes(), purge_ids)
            atomic_write_bytes(cfg.summary_pb, filtered_pb)

        res = {
            "status": "cleaned",
            "meta_backup": str(snap_dir),
            "lost_manifest": str(manifest) if manifest else None,
            "moved_subagents": len(moved),
            "skipped_locked": skipped_locked,
            "purged_summaries": len(purge_list),
            "proto_removed": proto_removed,
            "active_dbs_remaining": len(state.disk_dbs) - len(moved),
        }
        log.info("clean: %s", res)
        return res
