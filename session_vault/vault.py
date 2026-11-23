"""Layer 3: Online WAL-safe incremental root vault backup, archive search, and offline restore."""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import time

from .config import VaultConfig
from .platform_ops import InstanceLock
from .proto import upsert_summary_proto
from .store import (
    PRUNE_CAP,
    StateSnapshot,
    atomic_write_bytes,
    load_state,
    read_index_entries,
    rewrite_index_entries,
    snapshot_meta_files,
    verify_offline_gate,
)


def _sync_tree_incremental(src: Path, dst: Path) -> int:
    copied = 0
    if not src.is_dir():
        return 0
    for root, _, files in os.walk(src):
        target_dir = dst / Path(root).relative_to(src)
        dir_created = False
        for fn in files:
            sp = Path(root) / fn
            dp = target_dir / fn
            try:
                sst = sp.stat()
                if dp.exists():
                    dst_st = dp.stat()
                    if dst_st.st_size == sst.st_size and dst_st.st_mtime_ns == sst.st_mtime_ns:
                        continue
                if not dir_created:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    dir_created = True
                shutil.copy2(sp, dp)
                copied += 1
            except (FileNotFoundError, PermissionError):
                continue
    return copied


def _copy_if_changed(src: Path, dst: Path) -> bool:
    if not src.is_file():
        return False
    try:
        sst = src.stat()
        if dst.is_file():
            dst_st = dst.stat()
            if dst_st.st_size == sst.st_size and dst_st.st_mtime_ns == sst.st_mtime_ns:
                return False
        shutil.copy2(src, dst)
        return True
    except (FileNotFoundError, PermissionError):
        return False


def _write_if_changed(dst: Path, data: bytes) -> bool:
    try:
        if dst.is_file() and dst.stat().st_size == len(data) and dst.read_bytes() == data:
            return False
    except OSError:
        pass
    atomic_write_bytes(dst, data)
    return True


def _backup_db_file(db_path: Path, dest_db: Path, cid: str) -> None:
    tmp_db = dest_db.with_name(f"{db_path.name}.tmp.{os.getpid()}")
    try:
        if db_path.suffix == ".db":
            src_conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
            dst_conn = sqlite3.connect(tmp_db)
            try:
                src_conn.backup(dst_conn)
                qc = dst_conn.execute("PRAGMA quick_check").fetchone()[0]
                if qc != "ok":
                    raise RuntimeError(f"Backup quick_check failed for {cid}: {qc}")
            finally:
                dst_conn.close()
                src_conn.close()
        else:
            shutil.copy2(db_path, tmp_db)
        os.replace(tmp_db, dest_db)
    except BaseException:
        tmp_db.unlink(missing_ok=True)
        raise


def _backup_one_root(
    cfg: VaultConfig,
    state: StateSnapshot,
    cid: str,
    roots_dir: Path,
    log: logging.Logger,
) -> tuple[bool, int]:
    """Incrementally snapshot one protected root; returns (db_was_backed_up, copied_brain_files)."""
    db_path = state.disk_dbs[cid]
    st = db_path.stat()
    try:
        wst = db_path.with_name(db_path.name + "-wal").stat()
    except FileNotFoundError:
        wst = None
    wal_size = wst.st_size if wst else 0
    stamp = {
        "db_size": st.st_size,
        "db_mtime_ns": st.st_mtime_ns,
        "wal_size": wal_size,
        # Ignore mtime of a 0-byte -wal file created by SQLite's read-only WAL open protocol.
        "wal_mtime_ns": wst.st_mtime_ns if (wst and wal_size > 0) else 0,
    }

    dest = roots_dir / cid
    dest.mkdir(parents=True, exist_ok=True)
    meta_path = dest / "vault_meta.json"
    dest_db = dest / db_path.name

    prev_meta: dict[str, object] | None = None
    if meta_path.is_file():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prev_meta = loaded
            else:
                log.warning("backup: invalid non-object %s; forcing re-backup", meta_path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.warning("backup: unreadable %s (%s); forcing re-backup", meta_path, exc)

    db_dirty = not (prev_meta is not None and prev_meta.get("stamp") == stamp and dest_db.is_file())
    if db_dirty:
        _backup_db_file(db_path, dest_db, cid)

    brain_copied = _sync_tree_incremental(cfg.brain_dir / cid, dest / "brain")
    ann_copied = _copy_if_changed(cfg.ann_dir / f"{cid}.pbtxt", dest / f"{cid}.pbtxt")
    chap_copied = (
        _copy_if_changed(cfg.chap_dir / f"{cid}.pb", dest / f"{cid}.pb") if db_path.suffix == ".db" else False
    )
    row = state.summaries.get(cid)
    raw_written = (
        _write_if_changed(dest / "raw_summary.pb", row.raw_summary) if (row and row.raw_summary) else False
    )

    meta_core: dict[str, object] = {
        "cid": cid,
        "title": row.title if row else "",
        "last_modified_time": row.last_modified_time if row else "",
        "stamp": stamp,
    }
    meta_unchanged = (
        prev_meta is not None
        and bool(prev_meta.get("backed_up_at"))
        and all(prev_meta.get(k) == v for k, v in meta_core.items())
    )
    if db_dirty or brain_copied > 0 or ann_copied or chap_copied or raw_written or not meta_unchanged:
        meta = {**meta_core, "backed_up_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        atomic_write_bytes(meta_path, json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))

    return db_dirty, brain_copied


def run_backup(cfg: VaultConfig, log: logging.Logger) -> dict[str, int]:
    """Incrementally snapshot all protected roots & side questions via SQLite backup API."""
    state = load_state(cfg)
    roots_dir = cfg.vault_roots_dir
    roots_dir.mkdir(parents=True, exist_ok=True)

    backed_db = skipped_db = copied_brain_files = 0
    protected_on_disk = state.protected_on_disk
    for cid in protected_on_disk:
        db_dirty, brain_copied = _backup_one_root(cfg, state, cid, roots_dir, log)
        backed_db += int(db_dirty)
        skipped_db += int(not db_dirty)
        copied_brain_files += brain_copied

    res = {
        "protected_total": len(protected_on_disk),
        "backed_db": backed_db,
        "skipped_unchanged_db": skipped_db,
        "copied_brain_files": copied_brain_files,
    }
    log.info("backup: %s", res)
    return res


def search_archive(
    cfg: VaultConfig,
    query: str = "",
    *,
    root_id: str = "",
    limit: int = 20,
) -> list[dict[str, object]]:
    """Search archived subagents via index.jsonl (read-only, safe while Desktop is running)."""
    if limit <= 0:
        return []
    q = query.strip().lower()
    rid = root_id.strip().lower()
    hits: list[dict[str, object]] = []
    for rec in read_index_entries(cfg.subagent_index_path):
        if rid and not str(rec.get("root_id", "")).lower().startswith(rid):
            continue
        if q:
            haystack = " ".join(
                str(rec.get(k, "")) for k in ("cid", "parent_id", "root_id", "root_title", "agent_name")
            ).lower()
            if q not in haystack:
                continue
        hits.append(rec)
        if len(hits) >= limit:
            break
    return hits


def count_restorable(cfg: VaultConfig) -> dict[str, int]:
    """Fast directory-only count of restorable vault roots and archived subagents."""
    counts: dict[str, int] = {}
    for key, folder in (
        ("vault_roots", cfg.vault_roots_dir),
        ("archived_subagents", cfg.subagent_archive_dir),
    ):
        if not folder.is_dir():
            counts[key] = 0
            continue
        with os.scandir(folder) as it:
            counts[key] = sum(1 for entry in it if entry.is_dir())
    return counts


def list_restorable(cfg: VaultConfig) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {"vault_roots": [], "archived_subagents": []}
    for key, folder, meta_name in (
        ("vault_roots", cfg.vault_roots_dir, "vault_meta.json"),
        ("archived_subagents", cfg.subagent_archive_dir, "meta.json"),
    ):
        if not folder.is_dir():
            continue
        for item in sorted(folder.iterdir()):
            if not item.is_dir():
                continue
            mp = item / meta_name
            corrupt = False
            meta: dict[str, object] = {}
            if mp.is_file():
                try:
                    loaded = json.loads(mp.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        meta = loaded
                    else:
                        corrupt = True
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    corrupt = True
            entry: dict[str, object] = {
                "cid": item.name,
                "title": str(meta.get("title") or meta.get("root_title") or ""),
                "last_modified_time": str(meta.get("last_modified_time", "")),
            }
            if corrupt:
                entry["corrupt"] = True
            result[key].append(entry)
    return result


def _validate_cid(cid: str) -> str:
    if (
        not isinstance(cid, str)
        or not cid
        or cid.strip() != cid
        or cid in (".", "..")
        or ".." in cid
        or any(ch in cid for ch in ("/", "\\", "\x00", ":"))
        or Path(cid).name != cid
    ):
        raise ValueError(f"Invalid conversation ID: {cid!r}")
    return cid


def run_restore(cfg: VaultConfig, cid: str, log: logging.Logger) -> dict[str, object]:
    """Restore a vaulted root or archived subagent back into Antigravity Desktop (offline only)."""
    _validate_cid(cid)
    candidates = [cfg.vault_roots_dir / cid, cfg.subagent_archive_dir / cid]
    for cand in candidates:
        if cand.resolve().parent != cand.parent.resolve():
            raise ValueError(f"Invalid conversation ID path traversal: {cid!r}")
    src = next((p for p in candidates if p.is_dir()), None)
    if src is None:
        raise FileNotFoundError(f"Conversation {cid} not found in vault or subagent archive")

    with InstanceLock("op", cfg.lock_dir) as locked:
        if not locked:
            raise RuntimeError("Another session-vault operation is already in progress")
        gate_ok, gate_msg = verify_offline_gate(cfg)
        if not gate_ok:
            raise RuntimeError(f"Offline gate blocked restore: {gate_msg}")

        state = load_state(cfg)
        if len(state.disk_dbs) >= PRUNE_CAP - 20:
            raise RuntimeError(f"Active DB count ({len(state.disk_dbs)}) >= 480; run 'clean' before restore")

        snap_dir = snapshot_meta_files(cfg, tag="restore")
        db_src = next((p for p in (src / f"{cid}.db", src / f"{cid}.pb") if p.is_file()), None)
        if db_src is None:
            raise FileNotFoundError(f"No .db/.pb trajectory file inside {src}")

        cfg.conv_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        db_dst = cfg.conv_dir / db_src.name
        shutil.copy2(db_src, db_dst)
        os.utime(db_dst, (now, now))
        if src.parent == cfg.subagent_archive_dir and db_src.suffix == ".db":
            companion_pb = src / f"{cid}.pb"
            if companion_pb.is_file():
                pb_dst = cfg.conv_dir / companion_pb.name
                shutil.copy2(companion_pb, pb_dst)
                os.utime(pb_dst, (now, now))
        for suf in ("-wal", "-shm"):
            aux_src = src / f"{db_src.name}{suf}"
            aux_dst = cfg.conv_dir / f"{db_src.name}{suf}"
            if aux_src.is_file():
                shutil.copy2(aux_src, aux_dst)
            elif aux_dst.exists():
                aux_dst.unlink()

        bsrc = src / "brain"
        if bsrc.is_dir():
            bdst = cfg.brain_dir / cid
            if bdst.exists():
                shutil.rmtree(bdst)
            shutil.copytree(bsrc, bdst)

        sidecar_targets: list[tuple[str, Path]] = [(f"{cid}.pbtxt", cfg.ann_dir)]
        if src.parent == cfg.vault_roots_dir and db_src.suffix == ".db":
            sidecar_targets.append((f"{cid}.pb", cfg.chap_dir))
        for fname, target_dir in sidecar_targets:
            fp = src / fname
            if fp.is_file():
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fp, target_dir / fname)

        raw_pb_path = src / "raw_summary.pb"
        proto_updated = False
        if raw_pb_path.is_file() and cfg.summary_pb.is_file():
            updated = upsert_summary_proto(cfg.summary_pb.read_bytes(), cid, raw_pb_path.read_bytes())
            atomic_write_bytes(cfg.summary_pb, updated)
            proto_updated = True

        if src.parent == cfg.subagent_archive_dir and cfg.subagent_index_path.is_file():
            entries = read_index_entries(cfg.subagent_index_path, log=log)
            kept = [e for e in entries if str(e.get("cid")) != cid]
            if len(kept) != len(entries):
                rewrite_index_entries(cfg.subagent_index_path, kept)

        res = {
            "status": "restored",
            "cid": cid,
            "source": str(src),
            "meta_backup": str(snap_dir),
            "proto_updated": proto_updated,
        }
        log.info("restore: %s", res)
        return res
