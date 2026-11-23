"""Cross-platform OS primitives: process inspection, locks, and detached spawning."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _TH32CS_SNAPPROCESS = 0x00000002
    _SYNCHRONIZE = 0x00100000
    _GENERIC_READ = 0x80000000
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _ERROR_ALREADY_EXISTS = 183
    _WAIT_OBJECT_0 = 0x00000000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    _DETACHED_PROCESS = 0x00000008
    _CREATE_NEW_PROCESS_GROUP = 0x00000200
    _CREATE_NO_WINDOW = 0x08000000
    _CREATE_BREAKAWAY_FROM_JOB = 0x01000000

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    _k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32FirstW.restype = wintypes.BOOL
    _k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.restype = wintypes.BOOL
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.CreateMutexW.restype = ctypes.c_void_p
    _k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.OpenMutexW.restype = ctypes.c_void_p
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = ctypes.c_void_p
    _k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.restype = wintypes.BOOL
else:
    import fcntl


# --- Process Inspection ---


def _is_ls_name(name: str) -> bool:
    low = name.lower()
    if IS_WINDOWS:
        return low == "language_server.exe"
    return low == "language_server" or low.startswith("language_server_")


def _is_antigravity_name(name: str) -> bool:
    low = name.lower()
    if IS_WINDOWS:
        return low == "antigravity.exe"
    return low == "antigravity" or low == "antigravity helper"


def list_processes() -> list[tuple[int, int, str]]:
    """Return list of (pid, ppid, lowercase_process_name)."""
    if IS_WINDOWS:
        snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == _INVALID_HANDLE_VALUE:
            raise OSError(f"CreateToolhelp32Snapshot failed: {ctypes.get_last_error()}")
        procs: list[tuple[int, int, str]] = []
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            if _k32.Process32FirstW(snap, ctypes.byref(entry)):
                while True:
                    procs.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile.lower()))
                    if not _k32.Process32NextW(snap, ctypes.byref(entry)):
                        break
        finally:
            _k32.CloseHandle(snap)
        return procs

    out = subprocess.check_output(["ps", "-eo", "pid=,ppid=,comm="], text=True, timeout=5)
    procs = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        name = Path(parts[2].strip()).name.lower()
        procs.append((pid, ppid, name))
    return procs


def find_ls_and_main_agy() -> tuple[int | None, int | None]:
    """Return (language_server_pid, main_antigravity_pid) if Desktop LS is running."""
    procs = list_processes()
    by_pid = {pid: (ppid, name) for pid, ppid, name in procs}
    for pid, ppid, name in procs:
        if _is_ls_name(name):
            parent = by_pid.get(ppid)
            main_pid = ppid if (parent and _is_antigravity_name(parent[1])) else None
            return pid, main_pid
    return None, None


def has_main_antigravity_process() -> tuple[bool, str]:
    """Fail-closed check that neither language_server nor main Antigravity Desktop is running."""
    procs = list_processes()
    ls_pids = [pid for pid, _, name in procs if _is_ls_name(name)]
    if ls_pids:
        return True, f"language_server running (pids={ls_pids})"
    agy_pids = [pid for pid, _, name in procs if _is_antigravity_name(name)]
    if not agy_pids:
        return False, "ok"

    if IS_WINDOWS:
        try:
            out = subprocess.check_output(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='Antigravity.exe'\" "
                    "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
                ],
                text=True, timeout=8, creationflags=_CREATE_NO_WINDOW,
            ).strip()
            items = json.loads(out) if out else []
            if isinstance(items, dict):
                items = [items]
            non_sidecar = [
                it.get("ProcessId") for it in items
                if "sidecar_sdk" not in (it.get("CommandLine") or "")
            ]
            if non_sidecar:
                return True, f"main Antigravity.exe running (pids={non_sidecar})"
            return False, f"only sidecar_sdk Antigravity.exe present (pids={agy_pids})"
        except Exception as exc:
            return True, f"Antigravity.exe present and cmdline check failed ({exc})"

    # POSIX: inspect full command lines for the matched PIDs; allow leaked sidecar_sdk only.
    try:
        out = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True, timeout=5)
        agy_set = set(agy_pids)
        non_sidecar_posix: list[int] = []
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if pid in agy_set and "sidecar_sdk" not in parts[1]:
                non_sidecar_posix.append(pid)
        if non_sidecar_posix:
            return True, f"main Antigravity process running (pids={non_sidecar_posix})"
        return False, f"only sidecar_sdk Antigravity present (pids={agy_pids})"
    except Exception as exc:
        return True, f"Antigravity process present and cmdline check failed ({exc})"


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        h = _k32.OpenProcess(_SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            return _k32.WaitForSingleObject(h, 0) != _WAIT_OBJECT_0
        finally:
            _k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_pid_exit(pid: int, timeout_ms: int) -> bool:
    """Wait up to timeout_ms for pid to terminate. Returns True if exited."""
    timeout_ms = max(0, int(timeout_ms))
    if pid <= 0:
        return True
    if IS_WINDOWS:
        h = _k32.OpenProcess(_SYNCHRONIZE, False, pid)
        if not h:
            return True
        try:
            win_ms = min(timeout_ms, 0xFFFFFFFE)
            return _k32.WaitForSingleObject(h, win_ms) == _WAIT_OBJECT_0
        finally:
            _k32.CloseHandle(h)

    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while True:
        if not is_pid_alive(pid):
            return True
        rem = deadline - time.monotonic()
        if rem <= 0:
            return False
        time.sleep(min(0.25, rem))


def is_file_unshared(path: Path) -> bool:
    """Return True if path does not exist or no other process holds an incompatible handle/lock."""
    if not path.exists():
        return True
    if IS_WINDOWS:
        h = _k32.CreateFileW(
            str(path), _GENERIC_READ, 0, None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None
        )
        if h == _INVALID_HANDLE_VALUE or not h:
            return False
        _k32.CloseHandle(h)
        return True

    # POSIX: non-blocking advisory flock probe (paired with process gate).
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        except BlockingIOError:
            return False
    finally:
        os.close(fd)


# --- Singleton Instance Locks ---


class InstanceLock:
    """Cross-platform kernel-backed singleton lock (auto-released on crash/SIGKILL)."""

    def __init__(self, name: str, lock_dir: Path) -> None:
        self.name = name
        self.lock_dir = lock_dir
        self._handle: int | None = None
        self._fd: int | None = None

    @property
    def _win_mutex_name(self) -> str:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.name).strip("_") or "lock"
        safe_name = safe_name[:120]
        scope = hashlib.sha256(str(self.lock_dir.resolve()).encode("utf-8")).hexdigest()[:12]
        return f"Local\\AntigravitySessionVault_{safe_name}_{scope}"

    @property
    def _posix_lock_path(self) -> Path:
        return self.lock_dir / f"{self.name}.lock"

    def is_held(self) -> bool:
        """Fast non-creating check: True if another process currently holds this lock."""
        if IS_WINDOWS:
            h = _k32.OpenMutexW(_SYNCHRONIZE, False, self._win_mutex_name)
            if not h:
                return False
            _k32.CloseHandle(h)
            return True

        p = self._posix_lock_path
        if not p.exists():
            return False
        try:
            fd = os.open(str(p), os.O_RDWR)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
        finally:
            os.close(fd)

    def acquire(self) -> bool:
        if IS_WINDOWS:
            ctypes.set_last_error(0)
            h = _k32.CreateMutexW(None, False, self._win_mutex_name)
            err = ctypes.get_last_error()
            if not h:
                raise OSError(f"CreateMutexW({self._win_mutex_name}) failed: {err}")
            if err == _ERROR_ALREADY_EXISTS:
                _k32.CloseHandle(h)
                return False
            self._handle = h
            return True

        self.lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._posix_lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        self._fd = fd
        return True

    def release(self) -> None:
        if IS_WINDOWS:
            if self._handle:
                _k32.CloseHandle(self._handle)
                self._handle = None
        else:
            if self._fd is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._fd)
                    self._fd = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


# --- Detached Background Spawning ---


def preferred_background_python() -> str:
    """Return pythonw.exe on Windows when available (zero console flash), else sys.executable."""
    exe = Path(sys.executable)
    if IS_WINDOWS and exe.name.lower() == "python.exe":
        pyw = exe.with_name("pythonw.exe")
        if pyw.is_file():
            return str(pyw)
    return str(exe)


def spawn_detached(argv: list[str], cwd: Path | None = None) -> int:
    """Spawn a fully detached background process immune to parent Job Object / taskkill /T /F.

    Returns the spawned child's PID.
    """
    cwd_str = str(cwd) if cwd else None

    if not IS_WINDOWS:
        proc = subprocess.Popen(
            argv,
            cwd=cwd_str,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        return proc.pid

    # Windows path 1: direct breakaway from Job Object (5ms when allowed).
    flags = (
        _DETACHED_PROCESS
        | _CREATE_NEW_PROCESS_GROUP
        | _CREATE_NO_WINDOW
        | _CREATE_BREAKAWAY_FROM_JOB
    )
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd_str,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
        return proc.pid
    except PermissionError:
        pass

    # Windows path 2: parent is inside a KILL_ON_JOB_CLOSE Job Object with BREAKAWAY_OK=False
    # (verified on Antigravity Desktop v2.34.0). Delegate creation to WMI Win32_Process.Create,
    # which launches under WmiPrvSE.exe outside the caller's process tree and Job Object.
    cmdline = subprocess.list2cmdline(argv)
    env = os.environ.copy()
    env["SV_LAUNCH_CMD"] = cmdline
    if cwd_str:
        env["SV_LAUNCH_CWD"] = cwd_str
    else:
        env.pop("SV_LAUNCH_CWD", None)
    ps_script = (
        "$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly "
        "-Property @{ ShowWindow = [uint16]0 }; "
        "$argsMap = @{ CommandLine = $env:SV_LAUNCH_CMD; ProcessStartupInformation = $si }; "
        "if ($env:SV_LAUNCH_CWD) { $argsMap['CurrentDirectory'] = $env:SV_LAUNCH_CWD }; "
        "$res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments $argsMap; "
        "Write-Output \"$($res.ReturnValue) $($res.ProcessId)\""
    )
    res = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=_CREATE_NO_WINDOW,
    )
    parts = res.stdout.strip().split()
    if res.returncode != 0 or len(parts) != 2 or parts[0] != "0":
        raise RuntimeError(
            f"Detached CIM Win32_Process.Create failed: rc={res.returncode} "
            f"out={res.stdout.strip()!r} err={res.stderr.strip()!r}"
        )
    return int(parts[1])
