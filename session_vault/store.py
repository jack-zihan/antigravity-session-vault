"""Conversation store reader, classifier, shared storage primitives, and offline gate."""

from __future__ import annotations

import collections
import dataclasses
import datetime
import itertools
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from typing import Any, Iterable

from .config import RetentionPolicy, VaultConfig, default_config_path, validate_cid
from .platform_ops import has_main_antigravity_process, is_file_unshared

PRUNE_CAP = 500
CLI_SOURCE = "CORTEX_TRAJECTORY_SOURCE_CLI"
SIDE_Q_RE = re.compile(r'side_question_cascade_ids\s*:\s*"([0-9a-f-]{36})"')
PINNED_RE = re.compile(r"\bpinned\s*:\s*true\b")

_TMP_COUNTER = itertools.count()


@dataclasses.dataclass
class SummaryRow:
    cid: str
    title: str
    last_modified_time: str
    workspace_uris: str
    status: str
    source: str
    project_id: str
    agent_name: str
    parent_id: str
    nesting_depth: int
    not_fully_idle: int
    killed: int
    step_count: int
    raw_summary: bytes | None


@dataclasses.dataclass
class StateSnapshot:
    disk_dbs: dict[str, Path]
    summaries: dict[str, SummaryRow]
    protected_ids: set[str]
    pinned_ids: set[str]
    side_question_ids: set[str]
    subagents: dict[str, SummaryRow]
    named_root_orphans: list[SummaryRow]
    other_orphans: list[SummaryRow]
    cli_orphans: list[SummaryRow]
    disk_companions: dict[str, Path] = dataclasses.field(default_factory=dict)

    @property
    def protected_on_disk(self) -> list[str]:
        return sorted(cid for cid in self.protected_ids if cid in self.disk_dbs)


# --- Shared Storage Primitives ---


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes via fsync'd sibling temp file + atomic replace (retries transient Win32 locks)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{next(_TMP_COUNTER)}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.005 * (attempt + 1))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_config(cfg: VaultConfig, config_path: Path | None = None) -> Path:
    """Atomically persist VaultConfig to config.json."""
    target = (config_path or default_config_path()).expanduser()
    atomic_write_bytes(target, (json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return target


def read_index_entries(index_path: Path, *, log: logging.Logger | None = None) -> list[dict[str, Any]]:
    """Read subagent index.jsonl entries, skipping malformed lines with an optional warning."""
    if not index_path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for lineno, line in enumerate(index_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            if log is not None:
                log.warning("index: skipped malformed line %d in %s (%s)", lineno, index_path, exc)
            continue
        if not isinstance(rec, dict):
            if log is not None:
                log.warning("index: skipped non-object line %d in %s", lineno, index_path)
            continue
        entries.append(rec)
    return entries


def rewrite_index_entries(index_path: Path, entries: list[dict[str, Any]]) -> None:
    """Atomically write subagent index.jsonl sorted newest-first by last_modified_time."""
    ordered = sorted(entries, key=lambda r: str(r.get("last_modified_time", "")), reverse=True)
    content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ordered)
    atomic_write_bytes(index_path, content.encode("utf-8"))


def snapshot_meta_files(cfg: VaultConfig, tag: str = "meta") -> Path:
    """Create a collision-safe snapshot of summary DB/WAL/SHM and proto files (keeps latest 10)."""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_tag = re.sub(r"[^A-Za-z0-9_-]+", "_", tag).strip("_") or "meta"
    meta_root = cfg.archive_dir / "meta_backups"
    meta_root.mkdir(parents=True, exist_ok=True)
    snap_dir = Path(tempfile.mkdtemp(prefix=f"{ts}_{next(_TMP_COUNTER):06d}_{safe_tag}_", dir=meta_root))
    for fn in (
        "conversation_summaries.db",
        "conversation_summaries.db-wal",
        "conversation_summaries.db-shm",
        "agyhub_summaries_proto.pb",
    ):
        src = cfg.data_dir / fn
        if src.is_file():
            shutil.copy2(src, snap_dir / fn)
    prior: list[tuple[int, str, Path]] = []
    for p in meta_root.iterdir():
        if p == snap_dir:
            continue
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(st.st_mode):
            prior.append((st.st_mtime_ns, p.name, p))
    prior.sort()
    for _, _, old in prior[:-9]:
        shutil.rmtree(old, ignore_errors=True)
    return snap_dir


# --- Classification, Tree Summaries & Offline Gate ---


def parse_iso_ts(raw: str) -> float:
    s = raw.strip()
    if not s or s.startswith("0001-01-01"):
        return 0.0
    s = s.replace(" ", "T", 1)
    s = re.sub(r"\s+([+-]\d{2}:?\d{2}|Z)$", r"\1", s)
    # Truncate nanoseconds (>6 fractional digits) for Python 3.10+ fromisoformat.
    s = re.sub(r"\.(\d{6})\d+", r".\1", s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except (ValueError, OSError):
        return 0.0


def scan_annotations(ann_dir: Path) -> tuple[set[str], set[str], set[str]]:
    ann_ids: set[str] = set()
    pinned: set[str] = set()
    side_qs: set[str] = set()
    if not ann_dir.is_dir():
        return ann_ids, pinned, side_qs
    for p in ann_dir.iterdir():
        if p.suffix != ".pbtxt" or not p.is_file():
            continue
        ann_ids.add(p.stem)
        txt = p.read_text(encoding="utf-8", errors="replace")
        if PINNED_RE.search(txt):
            pinned.add(p.stem)
        side_qs.update(SIDE_Q_RE.findall(txt))
    return ann_ids, pinned, side_qs


def load_state(cfg: VaultConfig) -> StateSnapshot:
    """Read disk trajectories, annotations, and summary DB (read-only) and classify all IDs."""
    disk_dbs: dict[str, Path] = {}
    disk_companions: dict[str, Path] = {}
    if cfg.conv_dir.is_dir():
        for p in cfg.conv_dir.iterdir():
            if p.suffix not in (".db", ".pb") or not p.is_file():
                continue
            cid = p.stem
            prev = disk_dbs.get(cid)
            if prev is None:
                disk_dbs[cid] = p
            elif p.suffix == ".db" and prev.suffix == ".pb":
                disk_companions[cid] = prev
                disk_dbs[cid] = p
            elif p.suffix == ".pb" and prev.suffix == ".db":
                disk_companions[cid] = p

    ann_ids, pinned_ids, side_question_ids = scan_annotations(cfg.ann_dir)

    summaries: dict[str, SummaryRow] = {}
    conv_parent_ids: set[str] = set()
    if cfg.summary_db.is_file():
        conn = sqlite3.connect(f"{cfg.summary_db.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            for r in conn.execute(
                "SELECT conversation_id, title, last_modified_time, workspace_uris, status, source, "
                "project_id, agent_name, parent_conversation_id, nesting_depth, not_fully_idle, "
                "killed, step_count, raw_summary FROM conversation_summaries"
            ):
                summaries[r[0]] = SummaryRow(
                    cid=r[0],
                    title=r[1] or "",
                    last_modified_time=str(r[2] or ""),
                    workspace_uris=r[3] or "",
                    status=r[4] or "",
                    source=r[5] or "",
                    project_id=r[6] or "",
                    agent_name=r[7] or "",
                    parent_id=r[8] or "",
                    nesting_depth=int(r[9] or 0),
                    not_fully_idle=int(r[10] or 0),
                    killed=int(r[11] or 0),
                    step_count=int(r[12] or 0),
                    raw_summary=r[13],
                )
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "conversation_parents" in tables:
                for cid, pid in conn.execute("SELECT conversation_id, parent_id FROM conversation_parents"):
                    conv_parent_ids.add(cid)
                    conv_parent_ids.add(pid)
        finally:
            conn.close()

    protected_ids: set[str] = ann_ids | side_question_ids | conv_parent_ids
    subagents: dict[str, SummaryRow] = {}
    for cid in disk_dbs:
        if cid in protected_ids:
            continue
        row = summaries.get(cid)
        if row is not None and row.title == "" and (row.parent_id != "" or row.nesting_depth > 0 or row.agent_name != ""):
            subagents[cid] = row
        else:
            protected_ids.add(cid)

    named_root_orphans: list[SummaryRow] = []
    other_orphans: list[SummaryRow] = []
    cli_orphans: list[SummaryRow] = []
    for cid, row in summaries.items():
        if cid in disk_dbs:
            continue
        if row.source == CLI_SOURCE:
            cli_orphans.append(row)
        elif row.title != "":
            named_root_orphans.append(row)
        else:
            other_orphans.append(row)

    return StateSnapshot(
        disk_dbs=disk_dbs,
        summaries=summaries,
        protected_ids=protected_ids,
        pinned_ids=pinned_ids,
        side_question_ids=side_question_ids,
        subagents=subagents,
        named_root_orphans=named_root_orphans,
        other_orphans=other_orphans,
        cli_orphans=cli_orphans,
        disk_companions=disk_companions,
    )


def resolve_root_id(state: StateSnapshot, cid: str) -> str:
    seen: set[str] = set()
    cur = cid
    while cur in state.summaries and state.summaries[cur].parent_id and cur not in seen:
        seen.add(cur)
        cur = state.summaries[cur].parent_id
    return cur


def resolve_id_prefix(raw_id: str, candidates: Iterable[str]) -> str:
    """Resolve an exact ID or unambiguous prefix against known candidates."""
    needle = validate_cid(raw_id, "conversation ID")
    pool = set(candidates)
    if needle in pool:
        return needle
    matches = sorted(c for c in pool if c.lower().startswith(needle.lower()))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous ID prefix {needle!r}; matches: {', '.join(m[:12] for m in matches[:5])}")
    return needle


def summarize_trees(cfg: VaultConfig, state: StateSnapshot | None = None) -> list[dict[str, object]]:
    """Summarize active & archived subagent counts grouped by root conversation for curation."""
    snap = state or load_state(cfg)
    pol = cfg.retention
    keep_set = set(pol.keep_roots)
    arch_set = set(pol.archive_roots)

    active_stats: dict[str, dict[str, int]] = collections.defaultdict(
        lambda: {"total": 0, "killed": 0, "alive": 0, "bytes": 0}
    )
    for cid, row in snap.subagents.items():
        rid = resolve_root_id(snap, cid)
        st = active_stats[rid]
        st["total"] += 1
        st["killed" if row.killed == 1 else "alive"] += 1
        try:
            st["bytes"] += snap.disk_dbs[cid].stat().st_size
        except OSError:
            pass

    archived_counts: dict[str, int] = collections.Counter()
    archived_titles: dict[str, str] = {}
    for rec in read_index_entries(cfg.subagent_index_path):
        rid = str(rec.get("root_id") or "")
        if not rid:
            continue
        archived_counts[rid] += 1
        if rec.get("root_title") and rid not in archived_titles:
            archived_titles[rid] = str(rec["root_title"])

    all_rids = set(active_stats) | set(archived_counts) | keep_set | arch_set
    rows: list[dict[str, object]] = []
    for rid in all_rids:
        rrow = snap.summaries.get(rid)
        st = active_stats.get(rid, {"total": 0, "killed": 0, "alive": 0, "bytes": 0})
        tag = "keep" if rid in keep_set else ("archive" if rid in arch_set else "auto")
        title = (rrow.title if rrow and rrow.title else archived_titles.get(rid, ""))
        rows.append(
            {
                "root_id": rid,
                "title": title,
                "policy": tag,
                "pinned": rid in snap.pinned_ids,
                "root_on_disk": rid in snap.disk_dbs,
                "last_modified_time": rrow.last_modified_time if rrow else "",
                "active_subagents": st["total"],
                "killed": st["killed"],
                "alive": st["alive"],
                "active_bytes": st["bytes"],
                "archived_subagents": archived_counts.get(rid, 0),
            }
        )
    rows.sort(
        key=lambda r: (
            int(r["active_subagents"]),
            int(r["archived_subagents"]),
            str(r["last_modified_time"]),
        ),
        reverse=True,
    )
    return rows


def simulate_prune500(state: StateSnapshot) -> tuple[int, list[str]]:
    """Simulate Antigravity Desktop's startup PruneAll(500) eviction order by .db mtime."""
    if len(state.disk_dbs) <= PRUNE_CAP:
        return 0, []
    items: list[tuple[float, str]] = []
    for cid, path in state.disk_dbs.items():
        try:
            items.append((path.stat().st_mtime, cid))
        except OSError:
            continue
    if len(items) <= PRUNE_CAP:
        return 0, []
    items.sort()
    victims = [cid for _, cid in items[: len(items) - PRUNE_CAP]]
    return len(victims), [cid for cid in victims if cid in state.protected_ids]


def verify_offline_gate(cfg: VaultConfig) -> tuple[bool, str]:
    """Fail-closed verification that Antigravity Desktop is completely offline and unlocked."""
    busy, reason = has_main_antigravity_process()
    if busy:
        return False, reason
    time.sleep(0.3)
    busy, reason = has_main_antigravity_process()
    if busy:
        return False, reason
    if not is_file_unshared(cfg.summary_db):
        return False, "conversation_summaries.db is locked by another process"
    if not is_file_unshared(cfg.summary_pb):
        return False, "agyhub_summaries_proto.pb is locked by another process"
    return True, "ok"
