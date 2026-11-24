"""Event-driven lifecycle watcher (--exit-after-clean) and fast hook launcher."""

from __future__ import annotations

import logging
from pathlib import Path
import time

from .config import VaultConfig, WatcherSettings, default_config_path, default_home_dir, load_config
from .platform_ops import (
    InstanceLock,
    find_ls_and_main_agy,
    preferred_background_python,
    spawn_detached,
    wait_pid_exit,
)

OFFLINE_GATE_MAX_WAIT_SEC = 60.0
IDLE_GATE_POLL_SEC = 2.0
IDLE_POLL_SEC = 5.0
MIN_WAIT_SEC = 1.0
LS_RESTART_GRACE_SEC = 15.0
LS_RESTART_POLL_SEC = 0.4
ERROR_RETRY_SEC = 10.0


def ensure_watcher(
    cfg: VaultConfig,
    *,
    plugin_root: Path | None = None,
    config_path: Path | None = None,
    data_dir: Path | None = None,
    archive_dir: Path | None = None,
    vault_dir: Path | None = None,
    require_desktop_running: bool = True,
) -> dict[str, object]:
    """Fast-path singleton check; spawns detached self-terminating watcher if not already active."""
    if InstanceLock("watch", cfg.lock_dir).is_held():
        return {"status": "already_running"}

    if require_desktop_running and find_ls_and_main_agy()[0] is None:
        return {"status": "skipped_desktop_not_running"}

    root_dir = (plugin_root or Path(__file__).resolve().parent.parent).resolve()
    cmd = [preferred_background_python(), "-B", "-m", "session_vault"]
    resolved_cfg = config_path.expanduser() if config_path is not None else default_config_path()
    if resolved_cfg.is_file():
        cmd.extend(["--config", str(resolved_cfg.resolve())])

    home = default_home_dir()
    for flag, override, cfg_val, default_val in (
        ("--data-dir", data_dir, cfg.data_dir, Path.home() / ".gemini" / "antigravity"),
        ("--archive-dir", archive_dir, cfg.archive_dir, home / "archive"),
        ("--vault-dir", vault_dir, cfg.vault_dir, home / "vault"),
    ):
        if override is not None:
            cmd.extend([flag, str(override.expanduser().resolve())])
        elif not resolved_cfg.is_file() and cfg_val != default_val:
            cmd.extend([flag, str(cfg_val.expanduser().resolve())])

    cmd.extend(["watch", "--exit-after-clean"])
    return {"status": "spawned", "pid": spawn_detached(cmd, cwd=root_dir)}


def _wait_for_ls_restart(main_pid: int | None, log: logging.Logger) -> bool:
    """Wait briefly after LS exits to distinguish inline crash/self-restart from true Desktop quit."""
    deadline = time.monotonic() + LS_RESTART_GRACE_SEC
    while time.monotonic() < deadline:
        new_ls, _ = find_ls_and_main_agy()
        if new_ls is not None:
            log.info("watch: LS restarted inline (new_pid=%d); skipping offline clean", new_ls)
            return True
        if main_pid is not None:
            if wait_pid_exit(main_pid, int(LS_RESTART_POLL_SEC * 1000)):
                return False
        else:
            time.sleep(min(LS_RESTART_POLL_SEC, max(0.0, deadline - time.monotonic())))
    return False


def run_watch(
    cfg: VaultConfig,
    log: logging.Logger,
    *,
    config_path: Path | None = None,
    shield_sec: int | None = None,
    backup_sec: int | None = None,
    exit_after_clean: bool = False,
) -> None:
    """Monitor Antigravity Desktop: shield + backup online, run fast offline clean on exit."""
    settings = WatcherSettings(
        shield_interval_sec=shield_sec if shield_sec is not None else cfg.watcher.shield_interval_sec,
        backup_interval_sec=backup_sec if backup_sec is not None else cfg.watcher.backup_interval_sec,
    )
    s_sec, b_sec = settings.shield_interval_sec, settings.backup_interval_sec

    with InstanceLock("watch", cfg.lock_dir) as locked:
        if not locked:
            log.info("watch: another session-vault watcher is already running; exiting")
            return
        from .archive import run_clean
        from .shield import run_shield
        from .store import verify_offline_gate
        from .vault import run_backup

        log.info(
            "watch: started (shield=%ds, backup=%ds, exit_after_clean=%s, archive=%s, vault=%s)",
            s_sec, b_sec, exit_after_clean, cfg.archive_dir, cfg.vault_dir,
        )
        last_shield: float | None = None
        last_backup: float | None = None
        cleaned_while_idle = False
        idle_wait_started: float | None = None

        while True:
            try:
                ls_pid, main_pid = find_ls_and_main_agy()
                if ls_pid is None:
                    if cleaned_while_idle:
                        if exit_after_clean:
                            return
                        time.sleep(IDLE_POLL_SEC)
                        continue
                    gate_ok, gate_msg = verify_offline_gate(cfg)
                    if gate_ok:
                        active_pol = cfg.retention
                        try:
                            resolved_cfg = config_path.expanduser() if config_path is not None else default_config_path()
                            if resolved_cfg.is_file():
                                active_pol = load_config(resolved_cfg).retention
                        except Exception as exc:
                            log.warning("watch: failed to reload policy from config (%s); using startup policy", exc)
                        run_clean(cfg, log, policy=active_pol, skip_gate=True)
                        cleaned_while_idle = True
                        if exit_after_clean:
                            log.info("watch: offline clean complete; exiting (--exit-after-clean)")
                            return
                        time.sleep(IDLE_POLL_SEC)
                    elif exit_after_clean:
                        idle_wait_started = idle_wait_started or time.monotonic()
                        if time.monotonic() - idle_wait_started >= OFFLINE_GATE_MAX_WAIT_SEC:
                            log.info(
                                "watch: offline gate stayed busy (%s) for %.0fs; exiting",
                                gate_msg,
                                OFFLINE_GATE_MAX_WAIT_SEC,
                            )
                            return
                        time.sleep(IDLE_GATE_POLL_SEC)
                    else:
                        time.sleep(IDLE_POLL_SEC)
                    continue

                cleaned_while_idle = False
                idle_wait_started = None
                now = time.monotonic()
                if last_shield is None or now - last_shield >= s_sec:
                    run_shield(cfg, log)
                    last_shield = time.monotonic()
                if last_backup is None or now - last_backup >= b_sec:
                    run_backup(cfg, log)
                    last_backup = time.monotonic()

                now_after = time.monotonic()
                next_due = min(
                    max(MIN_WAIT_SEC, s_sec - (now_after - last_shield)),
                    max(MIN_WAIT_SEC, b_sec - (now_after - last_backup)),
                )
                if not wait_pid_exit(ls_pid, int(next_due * 1000)):
                    continue

                log.info("watch: language_server (pid=%d) exited; checking main_pid=%s", ls_pid, main_pid)
                if not _wait_for_ls_restart(main_pid, log):
                    # Next iteration sees ls_pid=None and runs the single unified offline-clean path.
                    continue
            except Exception:
                log.exception("watch: unexpected error in loop")
                time.sleep(ERROR_RETRY_SEC)
