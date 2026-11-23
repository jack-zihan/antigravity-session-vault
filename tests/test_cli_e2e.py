"""End-to-end CLI, manifest, and watcher lifecycle tests (isolated temp dirs)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from session_vault.cli import main as cli_main
from session_vault.config import VaultConfig
from session_vault.proto import encode_field1_entry
from session_vault.watcher import run_watch
from tests.helpers import iso_ago, make_summary_db

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestCliAndWatcherE2E(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg_path = self.root / "config.json"
        self.cfg = VaultConfig(
            data_dir=self.root / "data",
            archive_dir=self.root / "archive",
            vault_dir=self.root / "vault",
            log_file=self.root / "logs" / "vault.log",
            lock_dir=self.root / "locks",
        )
        for d in (self.cfg.conv_dir, self.cfg.brain_dir, self.cfg.ann_dir, self.cfg.chap_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.cfg_path.write_text(json.dumps(self.cfg.to_dict()), encoding="utf-8")

    def tearDown(self) -> None:
        log = logging.getLogger("session_vault")
        for h in list(log.handlers):
            h.close()
        log.handlers.clear()
        self.tmp.cleanup()

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", "-m", "session_vault", "--config", str(self.cfg_path), *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_plugin_manifests_valid_and_bounded(self) -> None:
        plugin = json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], "antigravity-session-vault")
        self.assertLessEqual(len(plugin.get("suggestedPrompts", [])), 3)
        self.assertTrue((REPO_ROOT / plugin["logo"]).is_file())

        hooks = json.loads((REPO_ROOT / "hooks.json").read_text(encoding="utf-8"))
        spec = hooks["session-vault-lifecycle"]
        self.assertIn("SessionStart", spec)
        self.assertIn("PreInvocation", spec)

    def test_cli_read_only_zero_logs_and_end_to_end_journey(self) -> None:
        root_id = "11111111-1111-1111-1111-111111111111"
        sub_id = "22222222-2222-2222-2222-222222222222"
        for cid in (root_id, sub_id):
            c = sqlite3.connect(self.cfg.conv_dir / f"{cid}.db")
            c.execute("CREATE TABLE s (x INT)")
            c.execute("INSERT INTO s VALUES (1)")
            c.commit()
            c.close()
            t = self.cfg.brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text('{"step": 1}\n', encoding="utf-8")

        rows = [
            (root_id, "Main Session", iso_ago(days=4), "", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "", "", 0, 0, 0, 10, b"R"),
            (sub_id, "", iso_ago(days=5), "", "CASCADE_RUN_STATUS_IDLE", "DESKTOP", "", "swarm-a", root_id, 1, 0, 1, 5, b"S"),
        ]
        make_summary_db(self.cfg.summary_db, rows)
        self.cfg.summary_pb.write_bytes(encode_field1_entry(root_id, b"R") + encode_field1_entry(sub_id, b"S"))

        # Read-only commands must NEVER create the log file or its parent directory.
        for cmd in (("status",), ("search", "swarm"), ("clean", "--dry-run"), ("restore", "--list"), ("config",)):
            r = self._run_cli(*cmd)
            self.assertEqual(r.returncode, 0, msg=f"{cmd} stderr={r.stderr}")
        self.assertFalse(self.cfg.log_file.parent.exists())

        # clean --dry-run is concise by default; --verbose includes offload_ids.
        dry_brief = json.loads(self._run_cli("clean", "--dry-run").stdout)
        self.assertNotIn("offload_ids", dry_brief)
        self.assertEqual(dry_brief["offload_count"], 1)
        dry_verb = json.loads(self._run_cli("clean", "--dry-run", "--verbose").stdout)
        self.assertEqual(dry_verb["offload_ids"], [sub_id])

        # Online mutating commands: shield + backup.
        self.assertEqual(json.loads(self._run_cli("shield").stdout)["demoted_killed"], 1)
        self.assertEqual(json.loads(self._run_cli("backup").stdout)["backed_db"], 1)
        self.assertTrue(self.cfg.log_file.is_file())

        # Offline clean -> search -> restore round-trip (via in-process cli_main with mocked offline gate).
        import io
        from contextlib import redirect_stdout

        with (
            mock.patch("session_vault.archive.verify_offline_gate", return_value=(True, "ok")),
            mock.patch("session_vault.vault.verify_offline_gate", return_value=(True, "ok")),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli_main(["--config", str(self.cfg_path), "clean"]), 0)
            hits = json.loads(self._run_cli("search", "swarm-a").stdout)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["cid"], sub_id)
            self.assertEqual(cli_main(["--config", str(self.cfg_path), "restore", sub_id]), 0)
            self.assertTrue((self.cfg.conv_dir / f"{sub_id}.db").is_file())

        # Invalid watcher interval must fail fast with exit code 1.
        r_bad = self._run_cli("watch", "--shield-sec", "0")
        self.assertEqual(r_bad.returncode, 1)

    def test_watcher_lifecycle_low_monotonic_restart_grace_and_exit_after_clean(self) -> None:
        log = logging.getLogger("test_watch")
        log.addHandler(logging.NullHandler())

        # Scenario: LS runs (shield+backup fire immediately even when monotonic < 1800),
        # LS exits -> inline restart check expires -> unified offline clean runs -> watcher exits.
        ls_seq = iter([(100, None), (None, None), (None, None)])
        clock = {"t": 10.0}

        def fake_monotonic() -> float:
            clock["t"] += 20.0
            return clock["t"]

        with (
            mock.patch("session_vault.watcher.find_ls_and_main_agy", side_effect=lambda: next(ls_seq, (None, None))),
            mock.patch("session_vault.watcher.wait_pid_exit", return_value=True),
            mock.patch("session_vault.watcher.time.monotonic", side_effect=fake_monotonic),
            mock.patch("session_vault.watcher.time.sleep"),
            mock.patch("session_vault.shield.run_shield") as m_shield,
            mock.patch("session_vault.vault.run_backup") as m_backup,
            mock.patch("session_vault.store.verify_offline_gate", return_value=(True, "ok")),
            mock.patch("session_vault.archive.run_clean") as m_clean,
        ):
            run_watch(self.cfg, log, shield_sec=120, backup_sec=1800, exit_after_clean=True)

        m_shield.assert_called_once()
        m_backup.assert_called_once()
        m_clean.assert_called_once_with(self.cfg, log, skip_gate=True)


if __name__ == "__main__":
    unittest.main()
