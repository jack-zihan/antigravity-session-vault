"""Integration tests for backup, offline clean (WAL/FTS5/rollback), search, restore, and config."""

from __future__ import annotations

import errno
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from session_vault.archive import _move_path, plan_clean, run_clean
from session_vault.config import RetentionPolicy, VaultConfig, load_config
from session_vault.proto import encode_field1_entry, extract_map_key, iter_proto_fields
from session_vault.store import CLI_SOURCE
from session_vault.vault import (
    count_restorable,
    discard_archived,
    list_restorable,
    run_backup,
    run_restore,
    search_archive,
)
from tests.helpers import iso_ago, make_summary_db


class TestCleanVaultRestore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = VaultConfig(
            data_dir=self.root / "data",
            archive_dir=self.root / "archive",
            vault_dir=self.root / "vault",
            log_file=self.root / "vault.log",
            lock_dir=self.root / "locks",
        )
        for d in (self.cfg.conv_dir, self.cfg.brain_dir, self.cfg.ann_dir, self.cfg.chap_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger("test_clean")
        self.log.addHandler(logging.NullHandler())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _populate_fixture(self) -> dict[str, str]:
        ids = {
            "root": "10000000-0000-0000-0000-000000000001",
            "sub_old": "20000000-0000-0000-0000-000000000002",
            "ghost": "30000000-0000-0000-0000-000000000003",
            "cli": "40000000-0000-0000-0000-000000000004",
        }
        for key in ("root", "sub_old"):
            cid = ids[key]
            conn = sqlite3.connect(self.cfg.conv_dir / f"{cid}.db")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE steps (id INT, body TEXT)")
            conn.execute("INSERT INTO steps VALUES (1, ?)", (f"data-{key}",))
            conn.commit()
            conn.close()
            tlog = self.cfg.brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
            tlog.parent.mkdir(parents=True, exist_ok=True)
            tlog.write_text(json.dumps({"step": 1, "key": key}) + "\n", encoding="utf-8")

        (self.cfg.ann_dir / f"{ids['root']}.pbtxt").write_text("pinned: true\n", encoding="utf-8")
        (self.cfg.chap_dir / f"{ids['root']}.pb").write_bytes(b"chapter-title")
        (self.cfg.conv_dir / f"{ids['sub_old']}.pb").write_bytes(b"sub-companion-pb")

        rows = [
            (ids["root"], "Kernel Investigation", iso_ago(days=5), "file:///repo", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "", "", 0, 0, 0, 20, b"\x0a\x04ROOT"),
            (ids["sub_old"], "", iso_ago(days=6), "file:///repo", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "research-worker", ids["root"], 1, 0, 1, 12, b"\x0a\x03SUB"),
            (ids["ghost"], "Evicted Historical Chat", iso_ago(days=30), "file:///old", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "", "", 0, 0, 0, 9, b"\x0a\x05GHOST"),
            (ids["cli"], "CLI Task", iso_ago(days=10), "file:///cli", "CASCADE_RUN_STATUS_IDLE", CLI_SOURCE, "", "", "", 0, 0, 0, 3, b"\x0a\x03CLI"),
        ]
        make_summary_db(self.cfg.summary_db, rows, wal=True)
        self.cfg.summary_pb.write_bytes(
            encode_field1_entry(ids["root"], b"\x0a\x04ROOT")
            + encode_field1_entry(ids["sub_old"], b"\x0a\x03SUB")
            + encode_field1_entry(ids["ghost"], b"\x0a\x05GHOST")
            + encode_field1_entry(ids["cli"], b"\x0a\x03CLI")
            + b"\x12\x08field2ok"
        )
        return ids

    def test_backup_dirty_skip_clean_search_and_restore_roundtrip(self) -> None:
        ids = self._populate_fixture()

        # 1. Layer 3 online WAL-safe backup & zero-rewrite second pass.
        b1 = run_backup(self.cfg, self.log)
        self.assertEqual((b1["protected_total"], b1["backed_db"]), (1, 1))
        with mock.patch("session_vault.vault.atomic_write_bytes") as awb:
            b2 = run_backup(self.cfg, self.log)
        self.assertEqual((b2["backed_db"], b2["skipped_unchanged_db"]), (0, 1))
        awb.assert_not_called()

        # 2. Read-only plan_clean omits offload_ids by default and includes when requested.
        p_brief = plan_clean(self.cfg)
        self.assertNotIn("offload_ids", p_brief)
        self.assertEqual(p_brief["offload_count"], 1)
        self.assertEqual(plan_clean(self.cfg, include_ids=True)["offload_ids"], [ids["sub_old"]])

        # 3. Layer 2 offline clean (FTS5 integrity-check + companion .pb + Field 2 / CLI preserved).
        with mock.patch("session_vault.archive.verify_offline_gate", return_value=(True, "ok")):
            res = run_clean(self.cfg, self.log, policy=RetentionPolicy(target_max=420, root_active_days=2, keep_idle_days=3))
        self.assertEqual((res["status"], res["moved_subagents"], res["purged_summaries"], res["proto_removed"]), ("cleaned", 1, 2, 2))
        self.assertFalse((self.cfg.conv_dir / f"{ids['sub_old']}.db").exists())
        self.assertFalse((self.cfg.conv_dir / f"{ids['sub_old']}.pb").exists())
        self.assertTrue((self.cfg.subagent_archive_dir / ids["sub_old"] / f"{ids['sub_old']}.pb").is_file())
        self.assertTrue((self.cfg.archive_dir / "lost_named_chats.md").is_file())

        conn = sqlite3.connect(self.cfg.summary_db)
        self.assertEqual({r[0] for r in conn.execute("SELECT conversation_id FROM conversation_summaries")}, {ids["root"], ids["cli"]})
        self.assertEqual({r[0] for r in conn.execute("SELECT conversation_id FROM conversations_fts")}, {ids["root"], ids["cli"]})
        conn.execute("INSERT INTO conversations_fts(conversations_fts) VALUES('integrity-check')")
        conn.close()

        pb_after = self.cfg.summary_pb.read_bytes()
        pb_keys = [extract_map_key(pl) for _, _, fn, wt, pl in iter_proto_fields(pb_after) if fn == 1 and wt == 2]
        self.assertEqual(pb_keys, [ids["root"], ids["cli"]])
        self.assertIn(b"\x12\x08field2ok", pb_after)

        # 4. Online search & restorable counts.
        hits = search_archive(self.cfg, "research-worker")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["cid"], ids["sub_old"])
        self.assertEqual(len(search_archive(self.cfg, root_id=ids["root"][:8])), 1)
        self.assertEqual(search_archive(self.cfg, "research-worker", limit=0), [])
        self.assertEqual(count_restorable(self.cfg), {"vault_roots": 1, "archived_subagents": 1})

        # 5. Offline restore of subagent auto-pins in keep_subagents and keeps companion .pb in conversations/.
        cfg_file = self.root / "restore_cfg.json"
        with mock.patch("session_vault.vault.verify_offline_gate", return_value=(True, "ok")):
            rres = run_restore(self.cfg, ids["sub_old"], self.log, config_path=cfg_file)
        self.assertEqual((rres["status"], rres["proto_updated"], rres["pinned_in_policy"]), ("restored", True, True))
        self.assertTrue((self.cfg.conv_dir / f"{ids['sub_old']}.db").is_file())
        self.assertTrue((self.cfg.conv_dir / f"{ids['sub_old']}.pb").is_file())
        self.assertFalse((self.cfg.chap_dir / f"{ids['sub_old']}.pb").exists())
        self.assertEqual(search_archive(self.cfg, "research-worker"), [])
        reloaded = load_config(cfg_file)
        self.assertIn(ids["sub_old"], reloaded.retention.keep_subagents)
        # Because restored subagent is auto-pinned, next clean will NOT immediately re-archive it.
        self.assertEqual(plan_clean(reloaded)["offload_count"], 0)

        # 6. Explicit discard removes ONLY cold archive folder (never live DB or root vault).
        dry_disc = discard_archived(self.cfg, cids=(ids["sub_old"],), confirm=False)
        self.assertEqual((dry_disc["status"], dry_disc["matched_count"]), ("dry_run", 1))
        done_disc = discard_archived(self.cfg, cids=(ids["sub_old"],), confirm=True, log=self.log)
        self.assertEqual((done_disc["status"], done_disc["removed_count"]), ("discarded", 1))
        self.assertFalse((self.cfg.subagent_archive_dir / ids["sub_old"]).exists())
        self.assertTrue((self.cfg.conv_dir / f"{ids['sub_old']}.db").is_file())
        self.assertTrue((self.cfg.vault_roots_dir / ids["root"]).is_dir())

    def test_clean_rollback_exdev_and_restore_safety_guards(self) -> None:
        ids = self._populate_fixture()

        def flaky_move(src: Path, dst: Path) -> None:
            if src.name == ids["sub_old"] and src.parent == self.cfg.brain_dir:
                raise PermissionError("simulated locked brain file")
            _move_path(src, dst)

        with (
            mock.patch("session_vault.archive.verify_offline_gate", return_value=(True, "ok")),
            mock.patch("session_vault.archive._move_path", side_effect=flaky_move),
        ):
            res = run_clean(self.cfg, self.log)
        self.assertEqual((res["moved_subagents"], res["skipped_locked"]), (0, 1))
        self.assertTrue((self.cfg.conv_dir / f"{ids['sub_old']}.db").is_file())
        self.assertFalse((self.cfg.subagent_archive_dir / ids["sub_old"]).exists())

        # Cross-filesystem EXDEV fallback moves both files and directories cleanly.
        exdev_src = self.root / "exdev_file.bin"
        exdev_dst = self.root / "exdev_moved.bin"
        exdev_src.write_bytes(b"payload")
        real_replace = __import__("os").replace
        fired = {"once": False}

        def fake_exdev(s: Path, d: Path) -> None:
            if not fired["once"]:
                fired["once"] = True
                raise OSError(errno.EXDEV, "cross-device")
            real_replace(s, d)

        with mock.patch("session_vault.archive.os.replace", side_effect=fake_exdev):
            _move_path(exdev_src, exdev_dst)
        self.assertFalse(exdev_src.exists())
        self.assertEqual(exdev_dst.read_bytes(), b"payload")

        # Restore rejects path traversal and corrupt meta is flagged in list_restorable.
        for bad_cid in ("", "..", "../evil", "a/b", "a\\b", "c:drive"):
            with self.assertRaises(ValueError):
                run_restore(self.cfg, bad_cid, self.log)

        corrupt_dir = self.cfg.vault_roots_dir / "corrupt-cid"
        corrupt_dir.mkdir(parents=True)
        (corrupt_dir / "vault_meta.json").write_text("{broken", encoding="utf-8")
        listed = list_restorable(self.cfg)["vault_roots"]
        self.assertTrue(any(e["cid"] == "corrupt-cid" and e.get("corrupt") for e in listed))

    def test_config_fail_fast_validation(self) -> None:
        cfg_file = self.root / "cfg.json"
        cfg_file.write_text(
            json.dumps({"archive_dir": str(self.root / "arch"), "retention": {"target_max": 350}}),
            encoding="utf-8",
        )
        loaded = load_config(cfg_file)
        self.assertEqual((loaded.archive_dir, loaded.retention.target_max), (self.root / "arch", 350))

        for bad in (
            {"retention": {"target_max": True}},
            {"retention": {"target_max": 0}},
            {"watcher": {"shield_interval_sec": 0}},
            {"retention": "not-a-dict"},
            {"archive_dir": "   "},
        ):
            cfg_file.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError, msg=f"should reject {bad}"):
                load_config(cfg_file)


if __name__ == "__main__":
    unittest.main()
