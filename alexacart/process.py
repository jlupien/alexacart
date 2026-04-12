"""Cross-platform process utilities for killing Chrome instances."""

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"


def kill_pid(pid: int, force: bool = False) -> None:
    """Kill a process by PID. On Windows, always uses taskkill."""
    if _IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, timeout=5,
        )
    else:
        sig = signal.SIGKILL if force else signal.SIGTERM
        os.kill(pid, sig)


def find_and_kill_chrome(profile_dir: Path, force: bool = False) -> bool:
    """Find and kill Chrome processes using the given profile directory.

    Returns True if any processes were killed.
    """
    if _IS_WINDOWS:
        return _kill_chrome_windows(profile_dir)
    return _kill_chrome_posix(profile_dir, force)


def _kill_chrome_posix(profile_dir: Path, force: bool) -> bool:
    sig_flag = "-9" if force else "-15"
    try:
        result = subprocess.run(
            ["pgrep", "-f", str(profile_dir)],
            capture_output=True, text=True, timeout=5,
        )
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        if not pids:
            return False
        logger.info("Killing %d Chrome process(es) for %s", len(pids), profile_dir.name)
        subprocess.run(["kill", sig_flag] + pids, capture_output=True, timeout=5)
        return True
    except Exception as e:
        logger.debug("Chrome cleanup for %s: %s", profile_dir.name, e)
        return False


def _kill_chrome_windows(profile_dir: Path) -> bool:
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             f"commandline like '%{profile_dir}%'",
             "get", "processid"],
            capture_output=True, text=True, timeout=10,
        )
        pids = [p.strip() for p in result.stdout.strip().split("\n")
                if p.strip() and p.strip().isdigit()]
        if not pids:
            return False
        logger.info("Killing %d Chrome process(es) for %s", len(pids), profile_dir.name)
        for pid in pids:
            subprocess.run(
                ["taskkill", "/F", "/PID", pid],
                capture_output=True, timeout=5,
            )
        return True
    except Exception as e:
        logger.debug("Chrome cleanup for %s: %s", profile_dir.name, e)
        return False
