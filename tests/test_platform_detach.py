"""Cross-platform tests for locks, hook contract, and detached process survival."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from session_vault.platform_ops import IS_WINDOWS, InstanceLock, is_pid_alive, wait_pid_exit

REPO_ROOT = Path(__file__).resolve().parent.parent


def _is_in_windows_job(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        in_job = wintypes.BOOL(False)
        return bool(k32.IsProcessInJob(h, None, ctypes.byref(in_job)) and in_job.value)
    finally:
        k32.CloseHandle(h)


class TestPlatformAndDetachment(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_instance_lock_same_and_cross_process_isolation(self) -> None:
        dir_a, dir_b = self.root / "locks_a", self.root / "locks_b"
        l1 = InstanceLock("watch", dir_a)
        l2 = InstanceLock("watch", dir_a)
        l_other = InstanceLock("watch", dir_b)

        self.assertTrue(l1.acquire())
        try:
            self.assertTrue(l2.is_held())
            self.assertFalse(l2.acquire())
            # Distinct lock_dir must be completely isolated (never collides with live watcher).
            self.assertTrue(l_other.acquire())
            l_other.release()

            # Real child OS process must see l1 as held and fail acquire().
            probe = (
                f"import pathlib, sys; sys.path.insert(0, {str(REPO_ROOT)!r}); "
                "from session_vault.platform_ops import InstanceLock; "
                f"lk = InstanceLock('watch', pathlib.Path({str(dir_a)!r})); "
                "print(lk.is_held(), lk.acquire())"
            )
            r = subprocess.run([sys.executable, "-B", "-c", probe], capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(r.stdout.strip(), "True False")
        finally:
            l1.release()
        self.assertFalse(l2.is_held())
        self.assertTrue(wait_pid_exit(-1, -50))

    def test_hook_fast_path_cli_skip_and_corrupt_config_fail_safe(self) -> None:
        hook_py = REPO_ROOT / "scripts" / "hook.py"
        env = os.environ.copy()
        env["PLUGIN_ROOT"] = str(REPO_ROOT)

        # CLI & IDE paths short-circuit immediately with {} and zero stderr.
        for path in (
            "/u/.gemini/antigravity-cli/brain/a/transcript.jsonl",
            "C:\\u\\.gemini\\antigravity-ide\\brain\\a\\transcript.jsonl",
        ):
            payload = json.dumps({"hookEventName": "SessionStart", "transcriptPath": path})
            r = subprocess.run([sys.executable, "-B", str(hook_py)], input=payload, capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual((r.returncode, r.stdout.strip(), r.stderr), (0, "{}", ""))

        # Corrupt config must fail-safe to {} rc=0 and log the error without crashing the agent turn.
        bad_cfg = self.root / "bad.json"
        bad_cfg.write_text("{not valid json", encoding="utf-8")
        env["SESSION_VAULT_CONFIG"] = str(bad_cfg)
        r = subprocess.run([sys.executable, "-B", str(hook_py)], input="{}", capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "{}"))
        self.assertTrue((self.root / "session-vault.log").is_file())

    def test_spawn_detached_survives_parent_tree_and_job_kill(self) -> None:
        """Verify spawn_detached survives KILL_ON_JOB_CLOSE + taskkill /T /F (Win) or SIGKILL pgrp (POSIX)."""
        pid_file = self.root / "detached.pid"
        beat_file = self.root / "heartbeat.txt"

        worker_code = (
            "import os, pathlib, time\n"
            f"p = pathlib.Path({str(beat_file)!r})\n"
            "for i in range(20):\n"
            "    p.write_text(f'{os.getpid()}:{i}:{time.time():.3f}\\n', encoding='utf-8')\n"
            "    time.sleep(0.2)\n"
        )
        parent_code = (
            "import pathlib, sys, time\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from session_vault.platform_ops import spawn_detached\n"
            f"pid = spawn_detached([sys.executable, '-B', '-c', {worker_code!r}])\n"
            f"pathlib.Path({str(pid_file)!r}).write_text(str(pid), encoding='utf-8')\n"
            "time.sleep(30)\n"
        )

        detached_pid: int | None = None
        try:
            if IS_WINDOWS:
                import ctypes
                from ctypes import wintypes

                k32 = ctypes.WinDLL("kernel32", use_last_error=True)

                class BASIC(ctypes.Structure):
                    _fields_ = [
                        ("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD),
                    ]

                class IO(ctypes.Structure):
                    _fields_ = [(f, ctypes.c_uint64) for f in ("a", "b", "c", "d", "e", "f")]

                class EXT(ctypes.Structure):
                    _fields_ = [
                        ("BasicLimitInformation", BASIC),
                        ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t),
                    ]

                k32.CreateJobObjectW.restype = ctypes.c_void_p
                k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
                k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                k32.OpenProcess.restype = ctypes.c_void_p
                k32.CloseHandle.argtypes = [ctypes.c_void_p]

                job = k32.CreateJobObjectW(None, None)
                self.assertTrue(job)
                info = EXT()
                info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                self.assertTrue(k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)))

                parent = subprocess.Popen([sys.executable, "-B", "-c", parent_code])
                ph = k32.OpenProcess(0x0100 | 0x0200 | 0x0001, False, parent.pid)
                if ph:
                    k32.AssignProcessToJobObject(job, ph)
                    k32.CloseHandle(ph)

                for _ in range(80):
                    if pid_file.is_file() and beat_file.is_file():
                        break
                    time.sleep(0.1)
                self.assertTrue(pid_file.is_file(), "parent did not write detached child PID")
                detached_pid = int(pid_file.read_text(encoding="utf-8").strip())

                subprocess.run(["taskkill", "/pid", str(parent.pid), "/T", "/F"], capture_output=True)
                k32.CloseHandle(job)
                parent.wait(timeout=5)
                before_beat = beat_file.read_text(encoding="utf-8")

                time.sleep(0.7)
                self.assertTrue(is_pid_alive(detached_pid), "detached child was killed with parent!")
                self.assertFalse(_is_in_windows_job(detached_pid), "detached child still inside a Job Object!")
                self.assertNotEqual(before_beat, beat_file.read_text(encoding="utf-8"))
            else:
                parent = subprocess.Popen([sys.executable, "-B", "-c", parent_code], start_new_session=True)
                for _ in range(80):
                    if pid_file.is_file() and beat_file.is_file():
                        break
                    time.sleep(0.1)
                self.assertTrue(pid_file.is_file())
                detached_pid = int(pid_file.read_text(encoding="utf-8").strip())

                os.killpg(parent.pid, signal.SIGKILL)
                parent.wait(timeout=5)
                before_beat = beat_file.read_text(encoding="utf-8")
                time.sleep(0.7)
                self.assertTrue(is_pid_alive(detached_pid))
                self.assertNotEqual(before_beat, beat_file.read_text(encoding="utf-8"))
        finally:
            if detached_pid and is_pid_alive(detached_pid):
                if IS_WINDOWS:
                    subprocess.run(["taskkill", "/pid", str(detached_pid), "/F"], capture_output=True)
                else:
                    try:
                        os.kill(detached_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
