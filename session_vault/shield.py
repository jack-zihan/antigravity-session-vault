"""Layer 1: Online mtime priority shield (UI-invisible crash/restart safety net)."""

from __future__ import annotations

import datetime
import logging
import os
import time

from .config import VaultConfig
from .platform_ops import is_file_unshared
from .store import load_state, parse_iso_ts

BASE_KILLED_TS = datetime.datetime(2010, 1, 1, tzinfo=datetime.timezone.utc).timestamp()
BASE_IDLE_TS = datetime.datetime(2015, 1, 1, tzinfo=datetime.timezone.utc).timestamp()
SHIELD_EPOCH_REF = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc).timestamp()
SHIELD_CUTOFF_TS = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc).timestamp()
KILLED_CUTOFF_TS = datetime.datetime(2014, 1, 1, tzinfo=datetime.timezone.utc).timestamp()
IDLE_SHIELD_COOLDOWN_SEC = 900


def run_shield(cfg: VaultConfig, log: logging.Logger) -> dict[str, int]:
    """Demote completed/idle subagent .db mtimes below all main conversations.

    Antigravity's sidebar sorts by summary.lastModifiedTime (inside agyhub_summaries_proto.pb),
    whereas PruneAll(500) evicts strictly by filesystem .db mtime. Demoting subagent mtimes
    to 2010 (killed) / 2015 (idle) ensures even a sudden power loss or crash restart never
    evicts a user's main conversation ahead of subagents.
    """
    state = load_state(cfg)
    now = time.time()
    demoted_killed = demoted_idle = skipped_busy = already_ok = 0

    for cid, row in state.subagents.items():
        if row.not_fully_idle != 0:
            skipped_busy += 1
            continue
        if row.killed == 1:
            cutoff_ts, base_ts = KILLED_CUTOFF_TS, BASE_KILLED_TS
        elif row.status == "CASCADE_RUN_STATUS_IDLE":
            cutoff_ts, base_ts = SHIELD_CUTOFF_TS, BASE_IDLE_TS
        else:
            skipped_busy += 1
            continue

        db_path = state.disk_dbs[cid]
        try:
            st = db_path.stat()
        except (FileNotFoundError, PermissionError):
            skipped_busy += 1
            continue

        if st.st_mtime < cutoff_ts:
            already_ok += 1
            continue

        sum_ts = parse_iso_ts(row.last_modified_time)
        if row.killed == 0 and now - max(st.st_mtime, sum_ts) < IDLE_SHIELD_COOLDOWN_SEC:
            skipped_busy += 1
            continue

        wal_path = db_path.with_name(db_path.name + "-wal")
        try:
            if wal_path.stat().st_size > 0:
                skipped_busy += 1
                continue
        except FileNotFoundError:
            pass
        except PermissionError:
            skipped_busy += 1
            continue

        if not is_file_unshared(db_path):
            skipped_busy += 1
            continue

        offset = max(0, min(int((sum_ts or st.st_mtime) - SHIELD_EPOCH_REF), 100_000_000))
        try:
            os.utime(db_path, (st.st_atime, base_ts + offset))
        except (FileNotFoundError, PermissionError):
            skipped_busy += 1
            continue

        if row.killed == 1:
            demoted_killed += 1
        else:
            demoted_idle += 1

    res = {
        "demoted_killed": demoted_killed,
        "demoted_idle": demoted_idle,
        "already_shielded": already_ok,
        "skipped_busy": skipped_busy,
    }
    log.info("shield: %s", res)
    return res
