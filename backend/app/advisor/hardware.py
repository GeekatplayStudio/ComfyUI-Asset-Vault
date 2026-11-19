"""Hardware profile and live resource sample.

Two public calls:

``profile()``   the slow, cached inventory - CPU, memory, every GPU, every
                volume, the accelerator the advisor plans against;
``sample()``    the fast reading the dashboard polls - utilisation, memory in
                use, VRAM in use, temperatures, disk and network throughput.

Everything here is **measured**; nothing is inferred except two values that are
flagged as such: the GPU share of Apple unified memory (macOS does not publish a
fixed figure) and the hardware tier label.

``psutil`` is used when it is installed and every reading has a stdlib or
probe-based fallback, so an install that has not picked up the dependency yet
still gets a profile - just with fewer live readings.

All parsing is in pure functions that take text, so the Windows and Linux paths
are covered by tests that never need that hardware.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import platform
import re
import shutil
import sys
import threading
import time
from collections import deque

from ..core import db as dbmod
from . import probe

try:  # optional - see the module docstring
    import psutil
except ImportError:  # pragma: no cover - exercised on installs without psutil
    psutil = None

log = logging.getLogger("vault.advisor.hardware")

GIB = 1024 ** 3
MIB = 1024 ** 2

PROFILE_TTL_S = 300.0
SAMPLE_MIN_INTERVAL_S = 0.45
HISTORY_LEN = 180
TOP_PROCESS_TTL_S = 4.0
TOP_PROCESS_COUNT = 6

#: Share of Apple unified memory the GPU can address by default.  Metal's
#: recommended working set is about two thirds of RAM on small machines and
#: three quarters on large ones; this is an estimate and is labelled as one.
APPLE_GPU_SHARE_SMALL = 0.66
APPLE_GPU_SHARE_LARGE = 0.75

VENDOR_BY_PCI = {"10de": "nvidia", "1002": "amd", "1022": "amd", "8086": "intel",
                 "106b": "apple"}

#: Name fragments that mark a GPU as integrated (shares system memory).
_INTEGRATED_HINTS = ("uhd graphics", "hd graphics", "iris", "radeon(tm) graphics",
                     "radeon graphics", "vega", "microsoft basic", "llvmpipe",
                     "virtual", "vmware", "parallels", "hyper-v", "remote display")
#: Intel names that are discrete despite the vendor.
_INTEL_DISCRETE_HINTS = ("arc",)

_PSEUDO_FS = {"devfs", "autofs", "tmpfs", "devtmpfs", "proc", "sysfs", "overlay",
              "squashfs", "efivarfs", "ramfs", "cgroup", "cgroup2", "nullfs",
              "fuse.portal", "fuse.gvfsd-fuse", "tracefs", "debugfs", "securityfs",
              "pstore", "bpf", "configfs", "fusectl", "mqueue", "hugetlbfs"}

_lock = threading.RLock()
_profile_cache: tuple[float, dict, tuple] | None = None
_history: deque = deque(maxlen=HISTORY_LEN)
_last_sample: tuple[float, dict] | None = None
_io_state: dict = {}
_top_cache: tuple[float, list] | None = None
_gpu_proc_cache: tuple[float, list] | None = None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _int(value: object, default: int | None = None) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _float(value: object, default: float | None = None) -> float | None:
    try:
        text = str(value).strip()
        if not text or text.lower() in ("n/a", "[n/a]", "[not supported]",
                                        "not supported", "none"):
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def _read(path: str, limit: int = 64 * 1024) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return None


def gpu_vendor(name: str | None, pci_vendor: str | None = None) -> str:
    """``nvidia`` / ``amd`` / ``intel`` / ``apple`` / ``unknown``."""
    if pci_vendor:
        hit = VENDOR_BY_PCI.get(str(pci_vendor).lower().replace("0x", "")[-4:])
        if hit:
            return hit
    low = (name or "").lower()
    if any(k in low for k in ("nvidia", "geforce", "rtx", "gtx", "quadro", "tesla")):
        return "nvidia"
    if any(k in low for k in ("amd", "radeon", "firepro")):
        return "amd"
    if "intel" in low or "arc(" in low or low.startswith("arc "):
        return "intel"
    if "apple" in low:
        return "apple"
    return "unknown"


def is_integrated(name: str | None, vendor: str, vram_bytes: int | None) -> bool:
    """A GPU that borrows system RAM rather than owning dedicated memory."""
    low = (name or "").lower()
    if vendor == "apple":
        return False  # unified memory is handled as its own case
    if vendor == "nvidia":
        return False
    if vendor == "intel" and any(h in low for h in _INTEL_DISCRETE_HINTS):
        return False
    if any(h in low for h in _INTEGRATED_HINTS):
        return True
    if vendor == "intel":
        return True
    # An AMD adapter reporting under 1 GiB of dedicated memory is an APU.
    return vendor == "amd" and vram_bytes is not None and vram_bytes < GIB


# ---------------------------------------------------------------------------
# Parsers (pure)
# ---------------------------------------------------------------------------

def parse_nvidia_static(text: str | None) -> list[dict]:
    """``nvidia-smi --query-gpu=index,name,memory.total,driver_version,...``."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 3:
            continue
        total_mib = _float(cells[2])
        if total_mib is None:
            continue
        out.append({
            "index": _int(cells[0], len(out)),
            "name": cells[1],
            "vendor": "nvidia",
            "backend": "cuda",
            "vram_total_bytes": int(total_mib * MIB),
            "vram_is_estimate": False,
            "unified": False,
            "integrated": False,
            "driver": cells[3] if len(cells) > 3 and cells[3] else None,
            "uuid": cells[5] if len(cells) > 5 and cells[5] else None,
            "source": "nvidia-smi",
        })
    return out


def parse_nvidia_live(text: str | None) -> list[dict]:
    """``index, utilization.gpu, memory.used, memory.total, temp, power, fan``."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 4:
            continue
        used, total = _float(cells[2]), _float(cells[3])
        if total is None:
            continue
        out.append({
            "index": _int(cells[0], len(out)),
            "util_percent": _float(cells[1]),
            "vram_used_bytes": int(used * MIB) if used is not None else None,
            "vram_total_bytes": int(total * MIB),
            "temp_c": _float(cells[4]) if len(cells) > 4 else None,
            "power_w": _float(cells[5]) if len(cells) > 5 else None,
            "fan_percent": _float(cells[6]) if len(cells) > 6 else None,
        })
    return out


def parse_nvidia_procs(text: str | None) -> list[dict]:
    """``gpu_uuid, pid, process_name, used_memory`` - who holds the VRAM."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 4:
            continue
        used = _float(cells[3])
        name = cells[2].replace("\\", "/").rsplit("/", 1)[-1]
        out.append({"gpu_uuid": cells[0], "pid": _int(cells[1]),
                    "name": name[:80],
                    "vram_bytes": int(used * MIB) if used is not None else None})
    return out


def apple_gpu_share(ram_bytes: int) -> float:
    return APPLE_GPU_SHARE_LARGE if ram_bytes >= 32 * GIB else APPLE_GPU_SHARE_SMALL


def parse_mac_displays(text: str | None, ram_bytes: int) -> list[dict]:
    """``system_profiler SPDisplaysDataType -json``."""
    try:
        doc = json.loads(text or "")
    except ValueError:
        return []
    out: list[dict] = []
    for item in doc.get("SPDisplaysDataType") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("sppci_model") or item.get("_name") or "GPU")
        vendor_raw = str(item.get("spdisplays_vendor") or "")
        vendor = gpu_vendor(name + " " + vendor_raw)
        vram_text = str(item.get("spdisplays_vram") or
                        item.get("spdisplays_vram_shared") or "")
        match = re.search(r"([\d.]+)\s*(GB|MB)", vram_text, re.IGNORECASE)
        unified = vendor == "apple"
        if unified:
            vram = int(ram_bytes * apple_gpu_share(ram_bytes))
        elif match:
            vram = int(float(match.group(1)) *
                       (GIB if match.group(2).upper() == "GB" else MIB))
        else:
            vram = None
        out.append({
            "index": len(out),
            "name": name,
            "vendor": vendor,
            "backend": "mps" if vendor in ("apple", "amd") else "cpu",
            "vram_total_bytes": vram,
            "vram_is_estimate": unified,
            "unified": unified,
            "integrated": is_integrated(name, vendor, vram),
            "cores": _int(item.get("sppci_cores")),
            "metal": str(item.get("spdisplays_mtlgpufamilysupport") or "")
            .replace("spdisplays_", "") or None,
            "driver": None,
            "source": "system_profiler",
        })
    return out


def parse_mac_sysctl(text: str | None) -> dict:
    """The six ``sysctl -n`` values requested in ``probe.COMMANDS``, in order."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    keys = ("model", "memsize", "physical", "logical", "perf", "eff")
    raw = dict(zip(keys, lines, strict=False))
    return {
        "model": raw.get("model"),
        "memsize": _int(raw.get("memsize")),
        "physical_cores": _int(raw.get("physical")),
        "logical_cores": _int(raw.get("logical")),
        "performance_cores": _int(raw.get("perf")),
        "efficiency_cores": _int(raw.get("eff")),
    }


def parse_mac_gpu_stats(text: str | None) -> dict:
    """``ioreg -c IOAccelerator`` PerformanceStatistics -> utilisation + memory."""
    blob = text or ""
    util = re.search(r'"Device Utilization %"\s*=\s*(\d+)', blob)
    in_use = re.search(r'"In use system memory"\s*=\s*(\d+)', blob)
    alloc = re.search(r'"Alloc system memory"\s*=\s*(\d+)', blob)
    return {
        "util_percent": float(util.group(1)) if util else None,
        "vram_used_bytes": int(in_use.group(1)) if in_use else None,
        "vram_alloc_bytes": int(alloc.group(1)) if alloc else None,
    }


def parse_mac_swap(text: str | None) -> tuple[int | None, int | None]:
    """``vm.swapusage: total = 3072.00M  used = 2240.25M ...`` -> (total, used)."""
    def grab(label: str) -> int | None:
        match = re.search(label + r"\s*=\s*([\d.]+)([KMG])", text or "")
        if not match:
            return None
        scale = {"K": 1024, "M": MIB, "G": GIB}[match.group(2)]
        return int(float(match.group(1)) * scale)
    return grab("total"), grab("used")


def parse_vm_stat(text: str | None) -> dict:
    """macOS ``vm_stat`` -> byte counts for the fallback memory reading."""
    blob = text or ""
    size = re.search(r"page size of (\d+) bytes", blob)
    page = int(size.group(1)) if size else 16384
    pages: dict[str, int] = {}
    for line in blob.splitlines():
        match = re.match(r'"?([A-Za-z -]+)"?:\s+(\d+)\.', line.strip())
        if match:
            pages[match.group(1).strip().lower()] = int(match.group(2)) * page
    free = pages.get("pages free", 0) + pages.get("pages speculative", 0)
    reclaimable = pages.get("pages inactive", 0) + pages.get("pages purgeable", 0)
    return {"free_bytes": free, "available_bytes": free + reclaimable,
            "wired_bytes": pages.get("pages wired down", 0),
            "active_bytes": pages.get("pages active", 0),
            "compressed_bytes": pages.get("pages occupied by compressor", 0)}


def parse_proc_cpuinfo(text: str | None) -> dict:
    """Linux ``/proc/cpuinfo`` -> model name, flags, physical core count."""
    model = None
    flags: set[str] = set()
    cores: set[tuple[str, str]] = set()
    physical = core = "0"
    for line in (text or "").splitlines():
        key, _sep, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key in ("model name", "hardware", "cpu model") and not model and value:
            model = value
        elif key in ("flags", "features") and not flags:
            flags = set(value.split())
        elif key == "physical id":
            physical = value
        elif key == "core id":
            core = value
            cores.add((physical, core))
    return {"model": model, "flags": flags, "physical_cores": len(cores) or None}


def parse_proc_meminfo(text: str | None) -> dict:
    """Linux ``/proc/meminfo`` -> bytes."""
    values: dict[str, int] = {}
    for line in (text or "").splitlines():
        match = re.match(r"(\w+):\s+(\d+)\s*kB", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    return {"total_bytes": values.get("MemTotal"),
            "available_bytes": values.get("MemAvailable", values.get("MemFree")),
            "swap_total_bytes": values.get("SwapTotal"),
            "swap_free_bytes": values.get("SwapFree")}


def parse_lspci(text: str | None) -> list[dict]:
    """``lspci -mm -nn`` -> display-class devices only."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        fields = re.findall(r'"([^"]*)"', line)
        if len(fields) < 3:
            continue
        device_class = fields[0].lower()
        if not any(k in device_class for k in ("vga", "3d controller", "display")):
            continue
        vendor_id = re.search(r"\[([0-9a-fA-F]{4})\]", fields[1])
        name = re.sub(r"\s*\[[0-9a-fA-F]{4}\]\s*$", "", fields[2]).strip()
        vendor_name = re.sub(r"\s*\[[0-9a-fA-F]{4}\]\s*$", "", fields[1]).strip()
        out.append({"name": f"{vendor_name} {name}".strip(),
                    "pci_vendor": vendor_id.group(1).lower() if vendor_id else None})
    return out


def cpu_features(flags: set[str], machine: str) -> list[str]:
    """The instruction-set features that change which CPU backends are viable."""
    wanted = (("avx", "AVX"), ("avx2", "AVX2"), ("avx512f", "AVX-512"),
              ("avx_vnni", "AVX-VNNI"), ("fma", "FMA"), ("f16c", "F16C"),
              ("sse4_2", "SSE4.2"), ("asimd", "NEON"), ("sve", "SVE"))
    found = [label for key, label in wanted if key in flags]
    if not found and machine.lower() in ("arm64", "aarch64"):
        found = ["NEON"]
    return found


# ---------------------------------------------------------------------------
# Platform collectors
# ---------------------------------------------------------------------------

def _platform() -> str:
    return "linux" if sys.platform.startswith("linux") else sys.platform


def _windows_registry_gpus() -> list[dict]:  # pragma: no cover - Windows only
    """Display adapters from the class key, where VRAM is a true 64-bit value.

    ``Win32_VideoController.AdapterRAM`` is a 32-bit field that reports 4 GB for
    every modern card, so it is not used.  ``HardwareInformation.qwMemorySize``
    is what the driver itself writes and is correct for 8, 16, 24 GB+ cards.
    """
    try:
        import winreg
    except ImportError:
        return []
    base = (r"SYSTEM\CurrentControlSet\Control\Class"
            r"\{4d36e968-e325-11ce-bfc1-08002be10318}")
    out: list[dict] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return []
    with root:
        for index in range(32):
            try:
                sub_name = winreg.EnumKey(root, index)
            except OSError:
                break
            if not sub_name.isdigit():
                continue
            try:
                with winreg.OpenKey(root, sub_name) as sub:
                    def value(key: str, _sub=sub) -> object:
                        try:
                            return winreg.QueryValueEx(_sub, key)[0]
                        except OSError:
                            return None
                    name = value("DriverDesc")
                    if not name:
                        continue
                    qw = value("HardwareInformation.qwMemorySize")
                    legacy = value("HardwareInformation.MemorySize")
                    vram = None
                    if isinstance(qw, int) and qw > 0:
                        vram = int(qw)
                    elif isinstance(legacy, int) and legacy > 0:
                        vram = int(legacy)
                    elif isinstance(legacy, bytes) and legacy:
                        vram = int.from_bytes(legacy[:8], "little")
                    match = re.search(r"VEN_([0-9A-Fa-f]{4})",
                                      str(value("MatchingDeviceId") or ""))
                    out.append({"name": str(name), "vram_total_bytes": vram,
                                "driver": value("DriverVersion"),
                                "pci_vendor": match.group(1).lower() if match else None})
            except OSError:
                continue
    return out


def _windows_cpu_name() -> str | None:  # pragma: no cover - Windows only
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except (ImportError, OSError):
        return None


def _windows_memory() -> dict:  # pragma: no cover - Windows only
    import ctypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {}
    except (AttributeError, OSError):
        return {}
    return {"total_bytes": int(status.ullTotalPhys),
            "available_bytes": int(status.ullAvailPhys)}


def _linux_sysfs_gpus() -> list[dict]:
    """AMD / Intel cards from ``/sys/class/drm`` - VRAM where the driver says."""
    out: list[dict] = []
    base = "/sys/class/drm"
    try:
        cards = sorted(n for n in os.listdir(base) if re.fullmatch(r"card\d+", n))
    except OSError:
        return []
    for card in cards:
        device = os.path.join(base, card, "device")
        vendor_id = (_read(os.path.join(device, "vendor")) or "").strip()
        if not vendor_id:
            continue
        total = _int(_read(os.path.join(device, "mem_info_vram_total")))
        out.append({"card": card, "pci_vendor": vendor_id.replace("0x", "").lower(),
                    "vram_total_bytes": total, "device_path": device})
    return out


def _linux_sysfs_live(device_path: str) -> dict:
    return {
        "util_percent": _float(_read(os.path.join(device_path, "gpu_busy_percent"))),
        "vram_used_bytes": _int(_read(os.path.join(device_path, "mem_info_vram_used"))),
    }


def _memory() -> dict:
    """Total / available / swap, by the best source that answers."""
    if psutil is not None:
        with contextlib.suppress(Exception):
            vm = psutil.virtual_memory()
            sw = psutil.swap_memory()
            return {"total_bytes": int(vm.total), "available_bytes": int(vm.available),
                    "used_bytes": int(vm.total - vm.available),
                    "swap_total_bytes": int(sw.total), "swap_used_bytes": int(sw.used)}
    plat = _platform()
    info: dict = {}
    if plat == "linux":
        parsed = parse_proc_meminfo(_read("/proc/meminfo"))
        info = {"total_bytes": parsed.get("total_bytes"),
                "available_bytes": parsed.get("available_bytes"),
                "swap_total_bytes": parsed.get("swap_total_bytes")}
        if parsed.get("swap_total_bytes") is not None and \
                parsed.get("swap_free_bytes") is not None:
            info["swap_used_bytes"] = parsed["swap_total_bytes"] - parsed["swap_free_bytes"]
    elif plat == "darwin":
        sysctl = parse_mac_sysctl(probe.run("mac_sysctl"))
        vm = parse_vm_stat(probe.run("mac_vm_stat"))
        swap_total, swap_used = parse_mac_swap(probe.run("mac_swap"))
        info = {"total_bytes": sysctl.get("memsize"),
                "available_bytes": vm.get("available_bytes"),
                "swap_total_bytes": swap_total, "swap_used_bytes": swap_used}
    elif plat == "win32":  # pragma: no cover - Windows only
        info = _windows_memory()
    total, available = info.get("total_bytes"), info.get("available_bytes")
    if total is not None and available is not None:
        info["used_bytes"] = max(0, total - available)
    return info


def _cpu() -> dict:
    plat = _platform()
    machine = platform.machine() or ""
    logical = os.cpu_count() or 1
    physical = None
    perf = eff = None
    model = None
    flags: set[str] = set()
    max_mhz = None
    if psutil is not None:
        with contextlib.suppress(Exception):
            physical = psutil.cpu_count(logical=False)
        with contextlib.suppress(Exception):
            freq = psutil.cpu_freq()
            # Apple Silicon reports a nonsense single-digit figure here.
            if freq and freq.max and freq.max >= 100:
                max_mhz = float(freq.max)
    if plat == "darwin":
        sysctl = parse_mac_sysctl(probe.run("mac_sysctl"))
        model = sysctl.get("model")
        physical = sysctl.get("physical_cores") or physical
        logical = sysctl.get("logical_cores") or logical
        perf, eff = sysctl.get("performance_cores"), sysctl.get("efficiency_cores")
    elif plat == "linux":
        parsed = parse_proc_cpuinfo(_read("/proc/cpuinfo", 512 * 1024))
        model, flags = parsed.get("model"), parsed.get("flags") or set()
        physical = physical or parsed.get("physical_cores")
    elif plat == "win32":  # pragma: no cover - Windows only
        model = _windows_cpu_name()
    model = model or platform.processor() or machine or "Unknown CPU"
    low = model.lower()
    vendor = ("apple" if "apple" in low else "intel" if "intel" in low
              else "amd" if "amd" in low or "ryzen" in low or "epyc" in low
              else "arm" if machine.lower() in ("arm64", "aarch64") else "unknown")
    return {
        "model": " ".join(model.split()),
        "vendor": vendor,
        "arch": machine,
        "physical_cores": physical or logical,
        "logical_cores": logical,
        "performance_cores": perf,
        "efficiency_cores": eff,
        "max_mhz": max_mhz,
        "features": cpu_features(flags, machine),
    }


def _gpus(ram_bytes: int) -> tuple[list[dict], list[dict]]:
    """Every adapter found, plus the list of probes that were tried."""
    plat = _platform()
    probes: list[dict] = []
    gpus: list[dict] = []

    def attempt(name: str) -> str | None:
        started = time.monotonic()
        text = probe.run(name)
        probes.append({"name": name, "ok": text is not None,
                       "ms": int((time.monotonic() - started) * 1000)})
        return text

    if plat == "darwin":
        gpus = parse_mac_displays(attempt("mac_displays"), ram_bytes)
    else:
        nvidia = parse_nvidia_static(attempt("nvidia_static"))
        others: list[dict] = []
        if plat == "win32":  # pragma: no cover - Windows only
            raw = _windows_registry_gpus()
            probes.append({"name": "windows_registry", "ok": bool(raw), "ms": 0})
        else:
            raw = _linux_sysfs_gpus()
            probes.append({"name": "linux_sysfs", "ok": bool(raw), "ms": 0})
            names = parse_lspci(attempt("linux_lspci"))
            by_vendor: dict[str, list[str]] = {}
            for entry in names:
                by_vendor.setdefault(entry.get("pci_vendor") or "", []).append(entry["name"])
            for entry in raw:
                queue = by_vendor.get(entry.get("pci_vendor") or "") or []
                entry["name"] = queue.pop(0) if queue else None
        for entry in raw:
            vendor = gpu_vendor(entry.get("name"), entry.get("pci_vendor"))
            if vendor == "nvidia" and nvidia:
                continue  # nvidia-smi already reported it, with exact VRAM
            vram = entry.get("vram_total_bytes")
            integrated = is_integrated(entry.get("name"), vendor, vram)
            if vendor == "nvidia":
                backend = "cuda"
            elif integrated:
                backend = "cpu"
            elif plat == "win32":
                backend = "directml"
            elif vendor == "amd":
                backend = "rocm"
            elif vendor == "intel":
                backend = "ipex"
            else:
                backend = "cpu"
            others.append({
                "name": entry.get("name") or f"{vendor.upper()} graphics adapter",
                "vendor": vendor, "backend": backend,
                "vram_total_bytes": vram, "vram_is_estimate": False,
                "unified": False, "integrated": integrated,
                "driver": entry.get("driver"),
                "device_path": entry.get("device_path"),
                "source": "registry" if plat == "win32" else "sysfs",
            })
        gpus = nvidia + others
        for index, gpu in enumerate(gpus):
            gpu["index"] = index if gpu.get("source") != "nvidia-smi" else gpu["index"]
    return gpus, probes


def pick_accelerator(gpus: list[dict], ram_bytes: int) -> dict:
    """The device the advisor plans against, and the pooled figure for LLMs.

    A diffusion pipeline runs on **one** device, so the planning figure is the
    largest single GPU.  A GGUF runner can split layers across several cards,
    so ``pooled_vram_bytes`` adds up every discrete card of the same backend.
    """
    usable = [g for g in gpus if not g.get("integrated")
              and g.get("backend") not in (None, "cpu")
              and (g.get("vram_total_bytes") or 0) > 0]
    if not usable:
        return {"backend": "cpu", "name": "CPU only", "vendor": None,
                "vram_total_bytes": 0, "pooled_vram_bytes": 0, "unified": False,
                "vram_is_estimate": False, "gpu_index": None, "gpu_count": 0}
    best = max(usable, key=lambda g: g["vram_total_bytes"])
    pooled = sum(g["vram_total_bytes"] for g in usable
                 if g["backend"] == best["backend"])
    return {"backend": best["backend"], "name": best["name"],
            "vendor": best.get("vendor"),
            "vram_total_bytes": int(best["vram_total_bytes"]),
            "pooled_vram_bytes": int(pooled),
            "unified": bool(best.get("unified")),
            "vram_is_estimate": bool(best.get("vram_is_estimate")),
            "gpu_index": best.get("index"),
            "gpu_count": len(usable)}


TIERS = (
    (48, "workstation", "Workstation", "Runs the largest open models at full precision."),
    (24, "enthusiast", "Enthusiast", "Full-precision image models and mid-size video and language models."),
    (16, "high", "High end", "Most image models in full precision; video and large language models quantised."),
    (12, "performance", "Performance", "Current image models comfortably; video with quantised weights."),
    (8, "mainstream", "Mainstream", "SDXL-class models natively; newer models need quantised weights."),
    (6, "entry", "Entry", "SD 1.5 and SDXL with care; small language models."),
    (0.5, "minimal", "Minimal", "Small models only; expect offloading to system memory."),
)


def tier_for(accelerator: dict, ram_bytes: int) -> dict:
    """A plain-language class for this machine.  A label, not a measurement."""
    gib = (accelerator.get("vram_total_bytes") or 0) / GIB
    if accelerator.get("backend") == "cpu":
        return {"id": "cpu", "label": "CPU only",
                "summary": "No usable GPU was found. Small quantised language "
                           "models run on the processor; image and video "
                           "generation will be very slow.",
                "inferred": True}
    for floor, tier_id, label, summary in TIERS:
        if gib >= floor * 0.93:  # a "24 GB" card reports ~23.99 GiB or less
            return {"id": tier_id, "label": label, "summary": summary,
                    "inferred": True}
    return {"id": "minimal", "label": "Minimal", "summary": TIERS[-1][3],
            "inferred": True}


def _disk_kind(device: str, plat: str) -> str:
    if plat == "darwin":
        return "ssd" if (platform.machine() or "").lower() == "arm64" else "unknown"
    if plat == "linux":
        name = os.path.basename(device or "")
        base = re.sub(r"p?\d+$", "", name) if name.startswith(("nvme", "mmcblk")) \
            else re.sub(r"\d+$", "", name)
        if name.startswith("nvme"):
            return "nvme"
        rotational = (_read(f"/sys/block/{base}/queue/rotational") or "").strip()
        if rotational == "1":
            return "hdd"
        if rotational == "0":
            return "ssd"
    return "unknown"


def _volumes(marked_paths: dict[str, str] | None = None) -> list[dict]:
    """Mounted volumes with capacity.  ``marked_paths`` maps a path -> a role."""
    plat = _platform()
    seen: dict[tuple, dict] = {}
    candidates: list[tuple[str, str, str]] = []
    if psutil is not None:
        with contextlib.suppress(Exception):
            candidates.extend((part.mountpoint, part.device, part.fstype)
                              for part in psutil.disk_partitions(all=False))
    if not candidates:
        candidates.append((os.path.abspath(os.sep), "", ""))
    candidates.extend((path, "", "") for path in (marked_paths or {}))
    for mount, device, fstype in candidates:
        if fstype.lower() in _PSEUDO_FS:
            continue
        # macOS mounts a volume per simulator runtime, per snapshot and per
        # system slice; only the boot volume and real external drives matter.
        if plat == "darwin" and fstype and mount != "/" \
                and not mount.startswith("/Volumes/"):
            continue
        if plat == "linux" and mount.startswith(("/snap", "/boot", "/run", "/sys",
                                                  "/proc", "/dev")):
            continue
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        if usage.total <= 0:
            continue
        key = (usage.total, usage.free // (64 * MIB))
        if key in seen:
            # The same physical volume mounted twice (macOS / and Data).
            if len(mount) < len(seen[key]["mount"]) and fstype:
                seen[key]["mount"] = mount
            continue
        seen[key] = {"mount": mount, "device": device or None,
                     "fstype": fstype or None,
                     "total_bytes": int(usage.total), "free_bytes": int(usage.free),
                     "used_bytes": int(usage.total - usage.free),
                     "kind": _disk_kind(device, plat), "roles": []}
    volumes = list(seen.values())
    for path, role in (marked_paths or {}).items():
        norm = os.path.normcase(os.path.abspath(path))
        best = None
        for volume in volumes:
            mount = os.path.normcase(os.path.abspath(volume["mount"]))
            inside = (norm == mount or norm.startswith(mount.rstrip(os.sep) + os.sep)
                      or mount == os.path.abspath(os.sep))
            if inside and (best is None
                           or len(mount) > len(os.path.normcase(best["mount"]))):
                best = volume
        if best is not None and role not in best["roles"]:
            best["roles"].append(role)
    volumes.sort(key=lambda v: (not v["roles"], v["mount"]))
    return volumes


def _os_info() -> dict:
    plat = _platform()
    name = platform.system()
    version = platform.release()
    if plat == "darwin":
        name, version = "macOS", platform.mac_ver()[0] or version
    elif plat == "win32":
        name = "Windows"
        with contextlib.suppress(Exception):
            version = platform.win32_ver()[0] + " " + platform.win32_ver()[1]
    elif plat == "linux":
        with contextlib.suppress(Exception):
            release = platform.freedesktop_os_release()
            name = release.get("NAME") or name
            version = release.get("VERSION") or version
    return {"platform": plat, "name": name, "version": version.strip(),
            "arch": platform.machine(), "python": platform.python_version()}


# ---------------------------------------------------------------------------
# Public: profile
# ---------------------------------------------------------------------------

def collect_profile(marked_paths: dict[str, str] | None = None) -> dict:
    """Assemble the profile now.  Slow (hundreds of ms); prefer :func:`profile`."""
    started = time.monotonic()
    memory = _memory()
    ram = int(memory.get("total_bytes") or 0)
    cpu = _cpu()
    gpus, probes = _gpus(ram)
    accelerator = pick_accelerator(gpus, ram)
    notes: list[str] = []
    if accelerator["unified"]:
        notes.append(
            "Unified memory: the GPU shares system RAM, so the planning figure "
            "is an estimate of the share the GPU can address, and every model "
            "also competes with the rest of the system for the same pool.")
    if accelerator["backend"] == "directml":
        notes.append("This GPU runs through DirectML on Windows, which is slower "
                     "and less memory-efficient than CUDA; treat verdicts as optimistic.")
    if accelerator["backend"] == "cpu" and gpus:
        notes.append("Only integrated graphics were found; planning against "
                     "system memory and the processor.")
    if accelerator.get("gpu_count", 0) > 1:
        notes.append("Several GPUs found. Image and video pipelines use one; "
                     "language-model runners can split across all of them.")
    if psutil is None:
        notes.append("psutil is not installed, so per-core load, disk and network "
                     "throughput are unavailable. Run the dependency installer.")
    for gpu in gpus:
        gpu.pop("device_path", None)
        gpu.pop("uuid", None)
    return {
        "collected_at": dbmod.now_ms(),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "os": _os_info(),
        "cpu": cpu,
        "memory": {"total_bytes": ram,
                   "available_bytes": memory.get("available_bytes"),
                   "swap_total_bytes": memory.get("swap_total_bytes")},
        "gpus": gpus,
        "accelerator": accelerator,
        "volumes": _volumes(marked_paths),
        "tier": tier_for(accelerator, ram),
        "notes": notes,
        "probes": probes,
        "psutil": psutil is not None,
    }


def profile(*, refresh: bool = False,
            marked_paths: dict[str, str] | None = None) -> dict:
    """The cached hardware profile.  ``refresh`` re-probes immediately."""
    global _profile_cache
    with _lock:
        now = time.monotonic()
        # The marked paths are part of the key: configuring the ComfyUI folder
        # must relabel the volumes without waiting for the cache to expire.
        marks = tuple(sorted((marked_paths or {}).items()))
        if not refresh and _profile_cache and now - _profile_cache[0] < PROFILE_TTL_S \
                and _profile_cache[2] == marks:
            return _profile_cache[1]
        if refresh:
            probe.reset_cache()
        result = collect_profile(marked_paths)
        _profile_cache = (now, result, marks)
        return result


# ---------------------------------------------------------------------------
# Public: live sample
# ---------------------------------------------------------------------------

def _rates(key: str, counters: dict[str, float], now: float) -> dict[str, float | None]:
    """Per-second deltas against the previous reading of the same counters."""
    previous = _io_state.get(key)
    _io_state[key] = (now, counters)
    if not previous:
        return dict.fromkeys(counters)
    elapsed = now - previous[0]
    if elapsed <= 0:
        return dict.fromkeys(counters)
    return {name: max(0.0, (value - previous[1].get(name, value)) / elapsed)
            for name, value in counters.items()}


def _top_processes() -> list[dict]:
    """The few processes holding the most memory.  Names and sizes only."""
    global _top_cache
    if psutil is None:
        return []
    now = time.monotonic()
    if _top_cache and now - _top_cache[0] < TOP_PROCESS_TTL_S:
        return _top_cache[1]
    rows: list[dict] = []
    with contextlib.suppress(Exception):
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            info = proc.info
            mem = info.get("memory_info")
            if mem is None:
                continue
            rows.append({"pid": info.get("pid"),
                         "name": str(info.get("name") or "?")[:60],
                         "rss_bytes": int(mem.rss)})
    rows.sort(key=lambda r: r["rss_bytes"], reverse=True)
    rows = rows[:TOP_PROCESS_COUNT]
    _top_cache = (now, rows)
    return rows


def _nvidia_procs() -> list[dict]:
    """Who holds the VRAM.  Slower-moving than utilisation, so asked less often:
    starting a second process every second just to redraw the same list would
    cost more than the dashboard is worth."""
    global _gpu_proc_cache
    now = time.monotonic()
    if _gpu_proc_cache and now - _gpu_proc_cache[0] < TOP_PROCESS_TTL_S:
        return _gpu_proc_cache[1]
    rows = parse_nvidia_procs(probe.run("nvidia_procs"))
    _gpu_proc_cache = (now, rows)
    return rows


def _live_gpus(static: dict) -> list[dict]:
    plat = _platform()
    gpus = static.get("gpus") or []
    out: list[dict] = []
    if plat == "darwin":
        stats = parse_mac_gpu_stats(probe.run("mac_gpu_stats"))
        out.extend({"index": gpu.get("index"), "name": gpu.get("name"),
                    "util_percent": stats.get("util_percent"),
                    "vram_used_bytes": stats.get("vram_used_bytes"),
                    "vram_total_bytes": gpu.get("vram_total_bytes"),
                    "temp_c": None, "power_w": None, "fan_percent": None,
                    "processes": []} for gpu in gpus)
        return out
    live = {g["index"]: g for g in parse_nvidia_live(probe.run("nvidia_live"))}
    procs = _nvidia_procs() if live else []
    for gpu in gpus:
        reading = live.get(gpu.get("index")) if gpu.get("source") == "nvidia-smi" else None
        row = {"index": gpu.get("index"), "name": gpu.get("name"),
               "util_percent": None, "vram_used_bytes": None,
               "vram_total_bytes": gpu.get("vram_total_bytes"),
               "temp_c": None, "power_w": None, "fan_percent": None,
               "processes": []}
        if reading:
            row.update({k: v for k, v in reading.items() if k != "index"})
            if len(live) == 1:
                row["processes"] = [
                    {"pid": p["pid"], "name": p["name"], "vram_bytes": p["vram_bytes"]}
                    for p in sorted(procs, key=lambda p: p["vram_bytes"] or 0,
                                    reverse=True)[:TOP_PROCESS_COUNT]]
        out.append(row)
    if plat == "linux":
        sysfs = {g["card"]: g for g in _linux_sysfs_gpus()}
        amd = [g for g in sysfs.values() if gpu_vendor(None, g["pci_vendor"]) != "nvidia"]
        # Pair by order among the non-NVIDIA cards only, on both sides, so a
        # machine whose NVIDIA card was found without nvidia-smi stays aligned.
        targets = [r for r, g in zip(out, gpus, strict=False)
                   if g.get("source") == "sysfs" and g.get("vendor") != "nvidia"]
        for row, entry in zip(targets, amd, strict=False):
            row.update(_linux_sysfs_live(entry["device_path"]))
    return out


def collect_sample(static: dict | None = None) -> dict:
    """One live reading.  Cheap enough to poll about once a second."""
    static = static or profile()
    now = time.monotonic()
    logical = (static.get("cpu") or {}).get("logical_cores") or os.cpu_count() or 1
    cpu: dict = {"percent": None, "per_core": [], "freq_mhz": None,
                 "load_1m": None, "temp_c": None}
    if psutil is not None:
        with contextlib.suppress(Exception):
            per_core = psutil.cpu_percent(interval=None, percpu=True)
            cpu["per_core"] = [round(float(v), 1) for v in per_core]
            if per_core:
                cpu["percent"] = round(sum(per_core) / len(per_core), 1)
        with contextlib.suppress(Exception):
            freq = psutil.cpu_freq()
            if freq and freq.current and freq.current >= 100:
                cpu["freq_mhz"] = round(float(freq.current))
        with contextlib.suppress(Exception):
            temps = psutil.sensors_temperatures()  # Linux only
            for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
                if temps.get(key):
                    cpu["temp_c"] = round(max(t.current for t in temps[key]), 1)
                    break
    with contextlib.suppress(AttributeError, OSError):
        cpu["load_1m"] = round(os.getloadavg()[0], 2)
    if cpu["percent"] is None and cpu["load_1m"] is not None:
        # Without psutil, load average over core count is the honest fallback.
        cpu["percent"] = round(min(100.0, cpu["load_1m"] / logical * 100.0), 1)

    memory = _memory()
    total = memory.get("total_bytes") or 0
    used = memory.get("used_bytes")
    mem = {"total_bytes": total, "used_bytes": used,
           "available_bytes": memory.get("available_bytes"),
           "percent": round(used / total * 100.0, 1) if total and used is not None else None,
           "swap_total_bytes": memory.get("swap_total_bytes"),
           "swap_used_bytes": memory.get("swap_used_bytes")}

    disk_io: dict = {"read_bps": None, "write_bps": None}
    net_io: dict = {"rx_bps": None, "tx_bps": None}
    if psutil is not None:
        with contextlib.suppress(Exception):
            counters = psutil.disk_io_counters()
            if counters:
                rates = _rates("disk", {"read_bps": counters.read_bytes,
                                        "write_bps": counters.write_bytes}, now)
                disk_io = {k: (round(v) if v is not None else None)
                           for k, v in rates.items()}
        with contextlib.suppress(Exception):
            counters = psutil.net_io_counters()
            if counters:
                rates = _rates("net", {"rx_bps": counters.bytes_recv,
                                       "tx_bps": counters.bytes_sent}, now)
                net_io = {k: (round(v) if v is not None else None)
                          for k, v in rates.items()}

    volumes = []
    for volume in static.get("volumes") or []:
        try:
            usage = shutil.disk_usage(volume["mount"])
        except OSError:
            continue
        volumes.append({"mount": volume["mount"], "total_bytes": int(usage.total),
                        "free_bytes": int(usage.free),
                        "used_bytes": int(usage.total - usage.free),
                        "percent": round((usage.total - usage.free) / usage.total * 100.0, 1)
                        if usage.total else None,
                        "kind": volume.get("kind"), "roles": volume.get("roles") or []})

    return {"t": dbmod.now_ms(), "cpu": cpu, "memory": mem,
            "gpus": _live_gpus(static), "disk_io": disk_io, "net_io": net_io,
            "volumes": volumes, "top_processes": _top_processes()}


def _history_point(reading: dict) -> dict:
    """The few numbers a sparkline needs - never the whole sample."""
    gpu = (reading.get("gpus") or [{}])[0] if reading.get("gpus") else {}
    vram_total = gpu.get("vram_total_bytes") or 0
    vram_used = gpu.get("vram_used_bytes")
    return {"t": reading["t"],
            "cpu": reading["cpu"].get("percent"),
            "mem": reading["memory"].get("percent"),
            "gpu": gpu.get("util_percent"),
            "vram": round(vram_used / vram_total * 100.0, 1)
            if vram_total and vram_used is not None else None,
            "disk_r": reading["disk_io"].get("read_bps"),
            "disk_w": reading["disk_io"].get("write_bps"),
            "net_rx": reading["net_io"].get("rx_bps"),
            "net_tx": reading["net_io"].get("tx_bps")}


def sample(*, history: bool = False) -> dict:
    """The live reading, rate-limited so several open tabs share one probe."""
    global _last_sample
    with _lock:
        now = time.monotonic()
        if _last_sample and now - _last_sample[0] < SAMPLE_MIN_INTERVAL_S:
            reading = _last_sample[1]
        else:
            reading = collect_sample()
            _last_sample = (now, reading)
            _history.append(_history_point(reading))
        result = dict(reading)
        if history:
            result["history"] = list(_history)
        return result


def reset() -> None:
    """Drop every cache (tests)."""
    global _profile_cache, _last_sample, _top_cache, _gpu_proc_cache
    with _lock:
        _profile_cache = None
        _last_sample = None
        _top_cache = None
        _gpu_proc_cache = None
        _history.clear()
        _io_state.clear()
        probe.reset_cache()


# psutil's first cpu_percent() call always reports 0.0 because it has nothing to
# diff against; priming it here means the first dashboard sample is real.
if psutil is not None:  # pragma: no branch
    with contextlib.suppress(Exception):
        psutil.cpu_percent(interval=None, percpu=True)
