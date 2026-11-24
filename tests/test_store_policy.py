"""Unit tests for store classification, Layer 1 shield, 4-tier retention, and atomic I/O."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from session_vault.archive import select_offload_subagents
from session_vault.config import RetentionPolicy, VaultConfig
from session_vault.shield import KILLED_CUTOFF_TS, SHIELD_CUTOFF_TS, run_shield
from session_vault.store import (
    CLI_SOURCE,
    atomic_write_bytes,
    load_state,
    parse_iso_ts,
    simulate_prune500,
    snapshot_meta_files,
    verify_offline_gate,
)
from tests.helpers import iso_ago, make_summary_db


def _touch_sqlite(path: Path, mtime: float | None = None) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INT)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class TestStoreAndPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = VaultConfig(
            data_dir=root / "data",
            archive_dir=root / "archive",
            vault_dir=root / "vault",
            log_file=root / "vault.log",
            lock_dir=root / "locks",
        )
        for d in (self.cfg.conv_dir, self.cfg.brain_dir, self.cfg.ann_dir, self.cfg.chap_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger("test_store")
        self.log.addHandler(logging.NullHandler())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_classifier_8_categories_companion_pb_and_shield_fast_path(self) -> None:
        now = time.time()
        ids = {
            "side_q": "11111111-1111-1111-1111-111111111111",
            "root": "22222222-2222-2222-2222-222222222222",
            "untitled_root": "33333333-3333-3333-3333-333333333333",
            "unindexed": "44444444-4444-4444-4444-444444444444",
            "wf_sub": "55555555-5555-5555-5555-555555555555",
            "swarm_killed": "66666666-6666-6666-6666-666666666666",
            "swarm_running": "77777777-7777-7777-7777-777777777777",
            "ghost_root": "88888888-8888-8888-8888-888888888888",
            "ghost_child": "99999999-9999-9999-9999-999999999999",
            "cli": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        }
        for key, age_sec in (
            ("root", 10 * 86400),
            ("side_q", 9 * 86400),
            ("untitled_root", 8 * 86400),
            ("unindexed", 60),
            ("wf_sub", 3600),
            ("swarm_killed", 3600),
            ("swarm_running", 60),
        ):
            _touch_sqlite(self.cfg.conv_dir / f"{ids[key]}.db", mtime=now - age_sec)
        # Companion .pb alongside .db must be tracked without replacing the .db entry.
        (self.cfg.conv_dir / f"{ids['swarm_killed']}.pb").write_bytes(b"companion")
        (self.cfg.ann_dir / f"{ids['root']}.pbtxt").write_text(
            f'pinned: true\nside_question_cascade_ids: "{ids["side_q"]}"\n', encoding="utf-8"
        )

        rows = [
            (ids["root"], "Main Chat", iso_ago(days=0.5), "file:///p", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "", "", 0, 0, 0, 42, b"r1"),
            (ids["untitled_root"], "", iso_ago(days=1), "file:///p", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "", "", 0, 0, 0, 2, b"r2"),
            (ids["wf_sub"], "", iso_ago(days=1), "file:///p", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "self", "", 0, 0, 0, 5, b"r3"),
            (ids["swarm_killed"], "", iso_ago(days=1), "file:///p", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "worker-1", ids["root"], 1, 0, 1, 8, b"r4"),
            (ids["swarm_running"], "", iso_ago(minutes=1), "file:///p", "CASCADE_RUN_STATUS_RUNNING", "DESKTOP", "", "worker-2", ids["root"], 1, 1, 0, 3, b"r5"),
            (ids["ghost_root"], "Old Evicted", iso_ago(days=20), "file:///p", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "", "", 0, 0, 0, 15, b"r6"),
            (ids["ghost_child"], "", iso_ago(days=20), "file:///p", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "worker", ids["ghost_root"], 1, 0, 1, 4, b"r7"),
            (ids["cli"], "CLI Session", iso_ago(days=2), "file:///p", "CASCADE_RUN_STATUS_IDLE", CLI_SOURCE, "", "", "", 0, 0, 0, 6, b"r8"),
        ]
        make_summary_db(self.cfg.summary_db, rows, parents=[(ids["side_q"], ids["root"])])

        state = load_state(self.cfg)
        self.assertTrue({ids["root"], ids["side_q"], ids["untitled_root"], ids["unindexed"]}.issubset(state.protected_ids))
        self.assertEqual(set(state.subagents.keys()), {ids["wf_sub"], ids["swarm_killed"], ids["swarm_running"]})
        self.assertEqual(state.disk_companions, {ids["swarm_killed"]: self.cfg.conv_dir / f"{ids['swarm_killed']}.pb"})
        self.assertEqual([r.cid for r in state.named_root_orphans], [ids["ghost_root"]])
        self.assertEqual([r.cid for r in state.other_orphans], [ids["ghost_child"]])
        self.assertEqual([r.cid for r in state.cli_orphans], [ids["cli"]])

        root_mtime = (self.cfg.conv_dir / f"{ids['root']}.db").stat().st_mtime
        res = run_shield(self.cfg, self.log)
        self.assertEqual((res["demoted_killed"], res["demoted_idle"], res["skipped_busy"]), (1, 1, 1))
        self.assertLess((self.cfg.conv_dir / f"{ids['swarm_killed']}.db").stat().st_mtime, KILLED_CUTOFF_TS)
        self.assertLess((self.cfg.conv_dir / f"{ids['wf_sub']}.db").stat().st_mtime, SHIELD_CUTOFF_TS)
        self.assertEqual((self.cfg.conv_dir / f"{ids['root']}.db").stat().st_mtime, root_mtime)
        self.assertEqual(simulate_prune500(state), (0, []))

        # Second pass must short-circuit already-shielded DBs before calling is_file_unshared.
        with mock.patch("session_vault.shield.is_file_unshared") as unshared:
            res2 = run_shield(self.cfg, self.log)
        self.assertEqual((res2["already_shielded"], res2["skipped_busy"]), (2, 1))
        unshared.assert_not_called()

    def test_smart_4_tier_oldest_first_safety_valve(self) -> None:
        cids = [
            "root-active", "root-old", "sub-running", "sub-alive-active",
            "sub-inactive-old", "sub-inactive-new",
            "sub-killed-1-oldest", "sub-killed-2-mid", "sub-killed-3-newest",
        ]
        for cid in cids:
            _touch_sqlite(self.cfg.conv_dir / f"{cid}.db")

        rows = [
            ("root-active", "Active", iso_ago(days=0.5), "", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "", "", 0, 0, 0, 10, b"a"),
            ("root-old", "Old", iso_ago(days=5), "", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "", "", 0, 0, 0, 10, b"b"),
            ("sub-running", "", iso_ago(minutes=1), "", "CASCADE_RUN_STATUS_RUNNING", "DESKTOP", "", "w", "root-active", 1, 1, 0, 1, b"c"),
            ("sub-alive-active", "", iso_ago(days=1), "", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "w", "root-active", 1, 0, 0, 1, b"d"),
            ("sub-inactive-old", "", iso_ago(days=8), "", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "w", "root-old", 1, 0, 1, 1, b"e"),
            ("sub-inactive-new", "", iso_ago(days=6), "", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "w", "root-old", 1, 0, 1, 1, b"f"),
            ("sub-killed-1-oldest", "", iso_ago(days=4), "", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "w", "root-active", 1, 0, 1, 1, b"g"),
            ("sub-killed-2-mid", "", iso_ago(days=2), "", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "w", "root-active", 1, 0, 1, 1, b"h"),
            ("sub-killed-3-newest", "", iso_ago(days=0.2), "", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "w", "root-active", 1, 0, 1, 1, b"i"),
        ]
        make_summary_db(self.cfg.summary_db, rows)
        state = load_state(self.cfg)

        sel = select_offload_subagents(state, RetentionPolicy(target_max=5, root_active_days=2, keep_idle_days=3))
        self.assertEqual(sel.auto_normal, ["sub-inactive-old", "sub-inactive-new"])
        self.assertEqual(sel.auto_overflow_killed, ["sub-killed-1-oldest", "sub-killed-2-mid"])
        self.assertEqual(sel.auto_overflow_alive, [])

        # Tighter target_max=3 spills into auto alive idle without touching running/roots.
        sel_b = select_offload_subagents(state, RetentionPolicy(target_max=3, root_active_days=2, keep_idle_days=3))
        self.assertEqual(sel_b.auto_overflow_killed, ["sub-killed-1-oldest", "sub-killed-2-mid", "sub-killed-3-newest"])
        self.assertEqual(sel_b.auto_overflow_alive, ["sub-alive-active"])

        # Importance-first: archive_roots offloads fresh active root first; keep_roots protects 5-day-old root;
        # keep_subagents never offloads even when target_max is tight.
        pol_imp = RetentionPolicy(
            target_max=5,
            root_active_days=2,
            keep_idle_days=3,
            keep_roots=("root-old",),
            archive_roots=("root-active",),
            keep_subagents=("sub-killed-3-newest",),
        )
        sel_imp = select_offload_subagents(state, pol_imp)
        self.assertEqual(
            sel_imp.explicit_archive,
            ["sub-killed-1-oldest", "sub-killed-2-mid", "sub-alive-active"],
        )
        self.assertNotIn("sub-killed-3-newest", sel_imp.all_ids)
        self.assertEqual(sel_imp.keep_overflow, ["sub-inactive-old"])
        self.assertNotIn("sub-inactive-new", sel_imp.all_ids)

        # Overlap between keep_roots and archive_roots must fail fast.
        with self.assertRaises(ValueError):
            RetentionPolicy(keep_roots=("root-old",), archive_roots=("root-old",))

    def test_parse_iso_ts_and_offline_gate(self) -> None:
        self.assertEqual(parse_iso_ts(""), 0.0)
        self.assertEqual(parse_iso_ts("0001-01-01T00:00:00Z"), 0.0)
        self.assertEqual(parse_iso_ts("not-a-timestamp"), 0.0)
        self.assertIsInstance(parse_iso_ts("1960-01-01T00:00:00"), float)
        self.assertGreater(parse_iso_ts("2026-11-22 12:34:56.123456789 +00:00"), 1_700_000_000)

        with mock.patch("session_vault.store.has_main_antigravity_process", return_value=(True, "ls running")):
            ok, msg = verify_offline_gate(self.cfg)
        self.assertFalse(ok)
        self.assertIn("ls running", msg)

        with (
            mock.patch("session_vault.store.has_main_antigravity_process", return_value=(False, "ok")),
            mock.patch("session_vault.store.is_file_unshared", return_value=True),
            mock.patch("session_vault.store.time.sleep"),
        ):
            self.assertEqual(verify_offline_gate(self.cfg), (True, "ok"))

    def test_atomic_write_concurrent_threads_and_transient_retry(self) -> None:
        target = self.cfg.archive_dir / "concurrent.json"
        payloads = [f'{{"worker": {i}}}\n'.encode("utf-8") for i in range(16)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda b: atomic_write_bytes(target, b), payloads))
        self.assertIn(target.read_bytes(), payloads)
        self.assertEqual(list(target.parent.glob("*.tmp.*")), [])

        # Transient Win32 PermissionError on first 2 replaces must retry and succeed.
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src: Path, dst: Path) -> None:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError("simulated AV scan lock")
            real_replace(src, dst)

        with mock.patch("session_vault.store.os.replace", side_effect=flaky_replace):
            atomic_write_bytes(target, b"recovered")
        self.assertEqual(target.read_bytes(), b"recovered")
        self.assertEqual(calls["n"], 3)

    def test_snapshot_meta_files_burst_caps_at_ten(self) -> None:
        self.cfg.summary_db.write_bytes(b"db")
        self.cfg.summary_pb.write_bytes(b"pb")
        snaps = [snapshot_meta_files(self.cfg, tag="clean") for _ in range(13)]
        self.assertEqual(len(set(snaps)), 13)
        remaining = list((self.cfg.archive_dir / "meta_backups").iterdir())
        self.assertEqual(len(remaining), 10)
        self.assertIn(snaps[-1], remaining)


if __name__ == "__main__":
    unittest.main()
