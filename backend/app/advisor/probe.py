"""Single sanctioned call site for read-only hardware inventory commands.

This module exists so that ``hardware`` never imports ``subprocess`` itself.
It mirrors ``jobs/video_frame.py`` and ``enable/git_fetch.py``: one module, a
frozen table of argument vectors, and nothing a caller supplies ever reaches
the command line.

Rules this module holds to, asserted in ``tests/security/test_advisor_probe.py``:

* The **program and every argument are constants** in :data:`COMMANDS`.  The
  public entry point takes a *name* from that table and nothing else - there is
  no parameter through which a path, flag or query can be injected.
* ``shell=False`` and a list argv - always.
* The program is resolved with ``shutil.which`` (plus a short list of fixed
  system directories), so a missing tool is a clean ``None`` and not an error.
* ``stdin`` is ``DEVNULL``, there is a hard timeout, and output is capped.
* Every command only **reads** machine state.  Nothing here changes a setting,
  and none of these tools is given an argument that could make it do so.

A probe that is missing, slow or fails simply contributes nothing: the profile
is assembled from whatever answered, and says which probes did.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger("vault.advisor.probe")

#: Never keep more than this from one command.
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
#: Wall-clock ceiling for the slow inventory commands.
TIMEOUT_S = 8.0
#: Ceiling for the commands the live dashboard repeats.
FAST_TIMEOUT_S = 2.5

NVIDIA_STATIC_FIELDS = ("index", "name", "memory.total", "driver_version",
                        "pci.bus_id", "uuid")
NVIDIA_LIVE_FIELDS = ("index", "utilization.gpu", "memory.used", "memory.total",
                      "temperature.gpu", "power.draw", "fan.speed")
NVIDIA_PROC_FIELDS = ("gpu_uuid", "pid", "process_name", "used_memory")


@dataclass(frozen=True)
class Command:
    """One frozen inventory command."""

    program: str
    args: tuple[str, ...]
    platforms: tuple[str, ...]
    timeout_s: float = TIMEOUT_S
    #: Extra absolute directories searched when the program is not on PATH.
    fallback_dirs: tuple[str, ...] = ()


#: The whole vocabulary.  Adding a row is a security decision: it must stay a
#: read-only inventory query with no caller-controlled part.
COMMANDS: dict[str, Command] = {
    "nvidia_static": Command(
        "nvidia-smi",
        ("--query-gpu=" + ",".join(NVIDIA_STATIC_FIELDS),
         "--format=csv,noheader,nounits"),
        ("win32", "linux"),
        fallback_dirs=(r"C:\Windows\System32", "/usr/bin", "/usr/local/bin"),
    ),
    "nvidia_live": Command(
        "nvidia-smi",
        ("--query-gpu=" + ",".join(NVIDIA_LIVE_FIELDS),
         "--format=csv,noheader,nounits"),
        ("win32", "linux"),
        timeout_s=FAST_TIMEOUT_S,
        fallback_dirs=(r"C:\Windows\System32", "/usr/bin", "/usr/local/bin"),
    ),
    "nvidia_procs": Command(
        "nvidia-smi",
        ("--query-compute-apps=" + ",".join(NVIDIA_PROC_FIELDS),
         "--format=csv,noheader,nounits"),
        ("win32", "linux"),
        timeout_s=FAST_TIMEOUT_S,
        fallback_dirs=(r"C:\Windows\System32", "/usr/bin", "/usr/local/bin"),
    ),
    "mac_displays": Command(
        "system_profiler", ("SPDisplaysDataType", "-json"), ("darwin",),
        fallback_dirs=("/usr/sbin",),
    ),
    "mac_sysctl": Command(
        "sysctl",
        ("-n", "machdep.cpu.brand_string", "hw.memsize", "hw.physicalcpu",
         "hw.logicalcpu", "hw.perflevel0.physicalcpu",
         "hw.perflevel1.physicalcpu"),
        ("darwin",), timeout_s=FAST_TIMEOUT_S, fallback_dirs=("/usr/sbin",),
    ),
    "mac_swap": Command(
        "sysctl", ("-n", "vm.swapusage"), ("darwin",),
        timeout_s=FAST_TIMEOUT_S, fallback_dirs=("/usr/sbin",),
    ),
    "mac_vm_stat": Command(
        "vm_stat", (), ("darwin",), timeout_s=FAST_TIMEOUT_S,
        fallback_dirs=("/usr/bin",),
    ),
    "mac_gpu_stats": Command(
        "ioreg", ("-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"), ("darwin",),
        timeout_s=FAST_TIMEOUT_S, fallback_dirs=("/usr/sbin",),
    ),
    "linux_lspci": Command(
        "lspci", ("-mm", "-nn"), ("linux",),
        fallback_dirs=("/usr/bin", "/usr/sbin", "/sbin"),
    ),
}

_resolved: dict[str, str | None] = {}


def _platform() -> str:
    import sys

    return "linux" if sys.platform.startswith("linux") else sys.platform


def _resolve(command: Command) -> str | None:
    """Absolute path of the program, or ``None``.  Cached per program."""
    if command.program in _resolved:
        return _resolved[command.program]
    found = shutil.which(command.program)
    if found is None:
        for directory in command.fallback_dirs:
            for suffix in ("", ".exe"):
                candidate = os.path.join(directory, command.program + suffix)
                if os.path.isfile(candidate):
                    found = candidate
                    break
            if found:
                break
    _resolved[command.program] = found
    return found


def available(name: str) -> bool:
    command = COMMANDS.get(name)
    if command is None or _platform() not in command.platforms:
        return False
    return _resolve(command) is not None


def run(name: str) -> str | None:
    """Run one command from :data:`COMMANDS` and return its stdout, or ``None``.

    ``name`` is a key of the frozen table; anything else returns ``None``.
    Never raises: a probe that cannot answer is simply absent from the profile.
    """
    command = COMMANDS.get(name)
    if command is None or _platform() not in command.platforms:
        return None
    exe = _resolve(command)
    if exe is None:
        return None
    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - frozen argv, shell=False, fixed program
            [exe, *command.args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            shell=False,
            timeout=command.timeout_s,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        log.debug("probe %s timed out", name)
        return None
    except OSError as exc:
        log.debug("probe %s could not start: %s", name, exc)
        return None
    if proc.returncode != 0:
        log.debug("probe %s exited %s", name, proc.returncode)
        return None
    data = (proc.stdout or b"")[:MAX_OUTPUT_BYTES]
    log.debug("probe %s answered in %.0f ms", name,
              (time.monotonic() - started) * 1000)
    return data.decode("utf-8", "replace")


def reset_cache() -> None:
    """Forget resolved program paths (tests, and a manual hardware refresh)."""
    _resolved.clear()
