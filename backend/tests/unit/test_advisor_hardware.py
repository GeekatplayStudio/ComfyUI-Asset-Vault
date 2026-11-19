"""System Advisor hardware parsers, fed canned text.

Every parser in ``advisor/hardware.py`` is a pure function over text, which is
what lets the Windows (``nvidia-smi``), Linux (``/proc``, ``lspci``) and macOS
(``system_profiler``, ``sysctl``, ``ioreg``, ``vm_stat``) paths be pinned here on
any machine.  The last two tests run the real collectors once on the host: they
assert the *shape* of the profile and the live sample, never a value, because the
figures are whatever this machine reports.
"""

from __future__ import annotations

import json

import pytest

from app.advisor import hardware

GIB = hardware.GIB
MIB = hardware.MIB


# ---------------------------------------------------------------- nvidia-smi

NVIDIA_STATIC = (
    "0, NVIDIA GeForce RTX 4090, 24564, 566.36, 00000000:01:00.0, GPU-aaaa-1111\n"
    "1, NVIDIA GeForce RTX 3060, 12288, 566.36, 00000000:02:00.0, GPU-bbbb-2222\n"
)


def test_nvidia_static_reads_every_card_with_its_real_vram():
    gpus = hardware.parse_nvidia_static(NVIDIA_STATIC)
    assert [g["index"] for g in gpus] == [0, 1]
    first = gpus[0]
    assert first["name"] == "NVIDIA GeForce RTX 4090"
    assert first["vendor"] == "nvidia"
    assert first["backend"] == "cuda"
    # A "24 GB" card reports 24564 MiB - a little under 24 GiB, never rounded up.
    assert first["vram_total_bytes"] == 24564 * MIB
    assert 23.9 * GIB < first["vram_total_bytes"] < 24 * GIB
    assert first["driver"] == "566.36"
    assert first["uuid"] == "GPU-aaaa-1111"
    assert first["integrated"] is False
    assert first["unified"] is False
    assert first["vram_is_estimate"] is False
    assert first["source"] == "nvidia-smi"


def test_nvidia_static_skips_rows_it_cannot_size():
    text = ("0, NVIDIA GeForce RTX 4090, [N/A], 566.36, 00000000:01:00.0, GPU-a\n"
            "garbage\n"
            "\n"
            "1, NVIDIA RTX A2000, 6138, [N/A], 00000000:02:00.0, GPU-b\n")
    gpus = hardware.parse_nvidia_static(text)
    assert len(gpus) == 1
    assert gpus[0]["name"] == "NVIDIA RTX A2000"
    assert gpus[0]["vram_total_bytes"] == 6138 * MIB


@pytest.mark.parametrize("text", [None, "", "\n\n", "no commas here"])
def test_nvidia_parsers_return_nothing_for_empty_or_foreign_text(text):
    assert hardware.parse_nvidia_static(text) == []
    assert hardware.parse_nvidia_live(text) == []
    assert hardware.parse_nvidia_procs(text) == []


def test_nvidia_live_keeps_readings_and_turns_na_cells_into_none():
    text = ("0, 37, 8123, 24564, 61, 212.45, 44\n"
            "1, [N/A], [N/A], 12288, [N/A], [N/A], [N/A]\n")
    rows = hardware.parse_nvidia_live(text)
    assert len(rows) == 2
    assert rows[0] == {"index": 0, "util_percent": 37.0,
                       "vram_used_bytes": 8123 * MIB,
                       "vram_total_bytes": 24564 * MIB,
                       "temp_c": 61.0, "power_w": 212.45, "fan_percent": 44.0}
    assert rows[1]["vram_total_bytes"] == 12288 * MIB
    for key in ("util_percent", "vram_used_bytes", "temp_c", "power_w", "fan_percent"):
        assert rows[1][key] is None, key


def test_nvidia_live_drops_a_row_without_a_total():
    assert hardware.parse_nvidia_live("0, 10, 100, [N/A], 50, 20, 30\n") == []


def test_nvidia_procs_keep_the_basename_only_and_tolerate_na():
    text = ("GPU-aaaa-1111, 4242, C:\\ComfyUI\\python_embeded\\python.exe, 9211\n"
            "GPU-aaaa-1111, 77, /usr/bin/ollama, [N/A]\n")
    rows = hardware.parse_nvidia_procs(text)
    assert rows[0] == {"gpu_uuid": "GPU-aaaa-1111", "pid": 4242,
                       "name": "python.exe", "vram_bytes": 9211 * MIB}
    assert rows[1]["name"] == "ollama"
    assert rows[1]["vram_bytes"] is None


# --------------------------------------------------------------------- macOS

def _displays(*items: dict) -> str:
    return json.dumps({"SPDisplaysDataType": list(items)})


def test_apple_unified_memory_is_a_share_of_ram_and_flagged_as_an_estimate():
    text = _displays({"_name": "kHW_AppleM2MaxItem", "sppci_model": "Apple M2 Max",
                      "spdisplays_vendor": "sppci_vendor_Apple", "sppci_cores": "38",
                      "spdisplays_mtlgpufamilysupport": "spdisplays_metal4"})
    for ram, share in ((16 * GIB, hardware.APPLE_GPU_SHARE_SMALL),
                       (64 * GIB, hardware.APPLE_GPU_SHARE_LARGE)):
        (gpu,) = hardware.parse_mac_displays(text, ram)
        assert gpu["vendor"] == "apple"
        assert gpu["backend"] == "mps"
        assert gpu["unified"] is True
        assert gpu["vram_is_estimate"] is True
        assert gpu["integrated"] is False
        assert gpu["vram_total_bytes"] == int(ram * share)
        assert gpu["vram_total_bytes"] < ram, "never the whole of system memory"
        assert gpu["cores"] == 38
        assert gpu["metal"] == "metal4"


def test_the_apple_gpu_share_steps_up_at_32_gib():
    assert hardware.apple_gpu_share(8 * GIB) == hardware.APPLE_GPU_SHARE_SMALL
    assert hardware.apple_gpu_share(32 * GIB) == hardware.APPLE_GPU_SHARE_LARGE
    assert 0 < hardware.APPLE_GPU_SHARE_SMALL < hardware.APPLE_GPU_SHARE_LARGE < 1


def test_an_intel_mac_with_a_discrete_radeon_reports_its_published_vram():
    text = _displays(
        {"sppci_model": "Intel UHD Graphics 630", "spdisplays_vendor": "Intel",
         "spdisplays_vram_shared": "1536 MB"},
        {"sppci_model": "AMD Radeon Pro 5500M", "spdisplays_vendor": "sppci_vendor_amd",
         "spdisplays_vram": "8 GB"})
    intel, amd = hardware.parse_mac_displays(text, 32 * GIB)
    assert intel["vendor"] == "intel"
    assert intel["integrated"] is True
    assert intel["backend"] == "cpu"
    assert intel["vram_total_bytes"] == 1536 * MIB
    assert amd["vendor"] == "amd"
    assert amd["backend"] == "mps"
    assert amd["vram_total_bytes"] == 8 * GIB
    assert amd["vram_is_estimate"] is False
    assert amd["unified"] is False
    assert [intel["index"], amd["index"]] == [0, 1]


@pytest.mark.parametrize("text", [None, "", "not json", "{}", '{"SPDisplaysDataType": ["x"]}'])
def test_mac_displays_never_raises_on_bad_input(text):
    assert hardware.parse_mac_displays(text, 16 * GIB) == []


def test_mac_sysctl_reads_the_six_values_in_the_order_they_were_asked_for():
    text = "Apple M2 Max\n34359738368\n12\n12\n8\n4\n"
    assert hardware.parse_mac_sysctl(text) == {
        "model": "Apple M2 Max", "memsize": 32 * GIB, "physical_cores": 12,
        "logical_cores": 12, "performance_cores": 8, "efficiency_cores": 4}


def test_mac_sysctl_on_an_intel_mac_leaves_the_missing_values_empty():
    parsed = hardware.parse_mac_sysctl("Intel(R) Core(TM) i9-9880H CPU @ 2.30GHz\n"
                                       "17179869184\n8\n16\n")
    assert parsed["memsize"] == 16 * GIB
    assert parsed["physical_cores"] == 8
    assert parsed["logical_cores"] == 16
    assert parsed["performance_cores"] is None
    assert parsed["efficiency_cores"] is None
    assert hardware.parse_mac_sysctl(None)["model"] is None


def test_mac_gpu_stats_reads_utilisation_and_memory_from_ioreg():
    text = ('  | "PerformanceStatistics" = {"In use system memory"=3516612608,'
            '"Alloc system memory"=9412345856,"Device Utilization %"=23,'
            '"Renderer Utilization %"=19}')
    assert hardware.parse_mac_gpu_stats(text) == {
        "util_percent": 23.0, "vram_used_bytes": 3516612608,
        "vram_alloc_bytes": 9412345856}
    assert hardware.parse_mac_gpu_stats(None) == {
        "util_percent": None, "vram_used_bytes": None, "vram_alloc_bytes": None}


def test_mac_swap_reads_total_and_used():
    text = "total = 3072.00M  used = 2240.25M  free = 831.75M  (encrypted)"
    total, used = hardware.parse_mac_swap(text)
    assert total == 3072 * MIB
    assert used == int(2240.25 * MIB)
    assert hardware.parse_mac_swap("total = 2.00G  used = 512.00K  free = 1.99G") == \
        (2 * GIB, 512 * 1024)
    assert hardware.parse_mac_swap(None) == (None, None)


def test_vm_stat_multiplies_page_counts_by_the_reported_page_size():
    text = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free:                               10000.\n"
            "Pages active:                            200000.\n"
            "Pages inactive:                           50000.\n"
            "Pages speculative:                         2000.\n"
            "Pages wired down:                         80000.\n"
            "Pages purgeable:                           3000.\n"
            '"Translation faults":                 123456789.\n'
            "Pages occupied by compressor:             40000.\n")
    parsed = hardware.parse_vm_stat(text)
    page = 16384
    assert parsed["free_bytes"] == (10000 + 2000) * page
    assert parsed["available_bytes"] == (10000 + 2000 + 50000 + 3000) * page
    assert parsed["wired_bytes"] == 80000 * page
    assert parsed["active_bytes"] == 200000 * page
    assert parsed["compressed_bytes"] == 40000 * page


def test_vm_stat_honours_a_4k_page_size_and_survives_empty_text():
    text = ("Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
            "Pages free:                                 100.\n")
    assert hardware.parse_vm_stat(text)["free_bytes"] == 100 * 4096
    assert hardware.parse_vm_stat(None)["available_bytes"] == 0


# --------------------------------------------------------------------- Linux

CPUINFO = """\
processor\t: 0
model name\t: AMD Ryzen 9 7950X 16-Core Processor
physical id\t: 0
core id\t\t: 0
flags\t\t: fpu sse4_2 avx avx2 fma f16c avx512f

processor\t: 1
model name\t: AMD Ryzen 9 7950X 16-Core Processor
physical id\t: 0
core id\t\t: 0
flags\t\t: fpu sse4_2 avx avx2 fma f16c avx512f

processor\t: 2
model name\t: AMD Ryzen 9 7950X 16-Core Processor
physical id\t: 0
core id\t\t: 1
flags\t\t: fpu sse4_2 avx avx2 fma f16c avx512f
"""


def test_proc_cpuinfo_counts_physical_cores_not_threads():
    parsed = hardware.parse_proc_cpuinfo(CPUINFO)
    assert parsed["model"] == "AMD Ryzen 9 7950X 16-Core Processor"
    assert parsed["physical_cores"] == 2, "three threads, two distinct (package, core)"
    assert {"avx2", "avx512f", "fma"} <= parsed["flags"]


def test_proc_cpuinfo_without_topology_reports_no_core_count():
    parsed = hardware.parse_proc_cpuinfo("Hardware\t: BCM2835\nFeatures\t: asimd fp\n")
    assert parsed["model"] == "BCM2835"
    assert parsed["flags"] == {"asimd", "fp"}
    assert parsed["physical_cores"] is None
    assert hardware.parse_proc_cpuinfo(None) == {"model": None, "flags": set(),
                                                 "physical_cores": None}


def test_proc_meminfo_converts_kib_to_bytes():
    text = ("MemTotal:       65741200 kB\n"
            "MemFree:         1200000 kB\n"
            "MemAvailable:   50000000 kB\n"
            "SwapTotal:       8388604 kB\n"
            "SwapFree:        8000000 kB\n"
            "HugePages_Total:       0\n")
    assert hardware.parse_proc_meminfo(text) == {
        "total_bytes": 65741200 * 1024, "available_bytes": 50000000 * 1024,
        "swap_total_bytes": 8388604 * 1024, "swap_free_bytes": 8000000 * 1024}


def test_proc_meminfo_falls_back_to_memfree_on_an_old_kernel():
    parsed = hardware.parse_proc_meminfo("MemTotal: 1000 kB\nMemFree: 400 kB\n")
    assert parsed["available_bytes"] == 400 * 1024
    assert parsed["swap_total_bytes"] is None


def test_lspci_keeps_display_devices_only_and_reads_the_vendor_id():
    text = (
        '00:00.0 "Host bridge [0600]" "Intel Corporation [8086]" "Device [a700]" -r01 "" ""\n'
        '00:02.0 "VGA compatible controller [0300]" "Intel Corporation [8086]" '
        '"Raptor Lake-S GT1 [UHD Graphics 770] [a780]" -r04 "ASUS [1043]" "Device [8882]"\n'
        '01:00.0 "VGA compatible controller [0300]" "NVIDIA Corporation [10de]" '
        '"GeForce RTX 4090 [2684]" -ra1 "ASUS [1043]" "Device [889c]"\n'
        '03:00.0 "Display controller [0380]" "Advanced Micro Devices, Inc. [AMD/ATI] [1002]" '
        '"Navi 31 [Radeon RX 7900 XT] [744c]" -rc8 "" ""\n'
        '04:00.0 "3D controller [0302]" "NVIDIA Corporation [10de]" "Device [20b0]" "" ""\n'
        "malformed line\n")
    rows = hardware.parse_lspci(text)
    assert [r["pci_vendor"] for r in rows] == ["8086", "10de", "1002", "10de"]
    assert rows[1]["name"] == "NVIDIA Corporation GeForce RTX 4090"
    assert "Radeon RX 7900 XT" in rows[2]["name"]
    assert all("Host bridge" not in r["name"] for r in rows)
    assert hardware.parse_lspci(None) == []


# ------------------------------------------------------ vendor / integrated

@pytest.mark.parametrize("name,pci,expected", [
    ("NVIDIA GeForce RTX 4090", None, "nvidia"),
    ("Quadro P2000", None, "nvidia"),
    ("AMD Radeon RX 7900 XTX", None, "amd"),
    ("Radeon Pro W6800", None, "amd"),
    ("Intel(R) UHD Graphics 770", None, "intel"),
    ("Intel(R) Arc(TM) A770 Graphics", None, "intel"),
    ("Apple M3 Pro", None, "apple"),
    ("Mystery Display Adapter", None, "unknown"),
    (None, None, "unknown"),
    # The PCI vendor id wins over whatever the name says.
    ("Mystery Display Adapter", "10de", "nvidia"),
    (None, "0x1002", "amd"),
    ("NVIDIA lookalike", "8086", "intel"),
    (None, "106B", "apple"),
    # An id that is not in the table falls back to the name.
    ("GeForce GTX 1080", "abcd", "nvidia"),
])
def test_gpu_vendor(name, pci, expected):
    assert hardware.gpu_vendor(name, pci) == expected


@pytest.mark.parametrize("name,vendor,vram,expected", [
    ("NVIDIA GeForce RTX 4090", "nvidia", 24 * GIB, False),
    ("NVIDIA GeForce MX150", "nvidia", 2 * GIB, False),
    ("Apple M2 Max", "apple", 21 * GIB, False),      # unified is its own case
    ("Intel(R) UHD Graphics 770", "intel", None, True),
    ("Intel(R) Iris(R) Xe Graphics", "intel", GIB, True),
    ("Intel(R) Arc(TM) A770 Graphics", "intel", 16 * GIB, False),
    ("AMD Radeon(TM) Graphics", "amd", 512 * MIB, True),
    ("AMD Radeon RX 7900 XTX", "amd", 24 * GIB, False),
    ("AMD Radeon RX 550", "amd", 512 * MIB, True),   # under 1 GiB dedicated = APU class
    ("AMD Radeon RX 7900 XTX", "amd", None, False),
    ("Microsoft Basic Render Driver", "unknown", None, True),
    ("VMware SVGA 3D", "unknown", None, True),
    ("Mystery Display Adapter", "unknown", 8 * GIB, False),
])
def test_is_integrated(name, vendor, vram, expected):
    assert hardware.is_integrated(name, vendor, vram) is expected


# -------------------------------------------------------------- accelerator

def _gpu(name, backend, vram, *, index=0, integrated=False, unified=False,
         estimate=False, vendor="nvidia"):
    return {"index": index, "name": name, "vendor": vendor, "backend": backend,
            "vram_total_bytes": vram, "integrated": integrated, "unified": unified,
            "vram_is_estimate": estimate}


def test_the_largest_single_gpu_is_the_planning_device():
    gpus = [_gpu("RTX 3060", "cuda", 12 * GIB, index=0),
            _gpu("RTX 4090", "cuda", 24 * GIB, index=1)]
    picked = hardware.pick_accelerator(gpus, 64 * GIB)
    assert picked["name"] == "RTX 4090"
    assert picked["gpu_index"] == 1
    assert picked["backend"] == "cuda"
    assert picked["vram_total_bytes"] == 24 * GIB, "one device, never the sum"
    assert picked["gpu_count"] == 2


def test_the_pooled_figure_sums_cards_of_the_same_backend_only():
    gpus = [_gpu("RTX 4090", "cuda", 24 * GIB, index=0),
            _gpu("RTX 3090", "cuda", 24 * GIB, index=1),
            _gpu("Radeon RX 7900 XT", "rocm", 20 * GIB, index=2, vendor="amd"),
            _gpu("UHD Graphics 770", "cpu", 2 * GIB, index=3, integrated=True,
                 vendor="intel")]
    picked = hardware.pick_accelerator(gpus, 128 * GIB)
    assert picked["vram_total_bytes"] == 24 * GIB
    assert picked["pooled_vram_bytes"] == 48 * GIB
    assert picked["gpu_count"] == 3, "the integrated adapter is not usable"


def test_integrated_graphics_alone_mean_cpu_only():
    gpus = [_gpu("Intel(R) UHD Graphics 770", "cpu", 2 * GIB, integrated=True,
                 vendor="intel")]
    picked = hardware.pick_accelerator(gpus, 32 * GIB)
    assert picked["backend"] == "cpu"
    assert picked["vram_total_bytes"] == 0
    assert picked["pooled_vram_bytes"] == 0
    assert picked["gpu_index"] is None
    assert picked["gpu_count"] == 0


@pytest.mark.parametrize("gpus", [
    [],
    [_gpu("Flagged integrated", "directml", 8 * GIB, integrated=True)],
    [_gpu("No size", "cuda", None)],
    [_gpu("Zero size", "cuda", 0)],
    [_gpu("No backend", None, 8 * GIB)],
])
def test_nothing_usable_means_cpu_only(gpus):
    assert hardware.pick_accelerator(gpus, 16 * GIB)["backend"] == "cpu"


def test_the_accelerator_carries_the_unified_and_estimate_flags():
    gpus = [_gpu("Apple M2 Max", "mps", 24 * GIB, unified=True, estimate=True,
                 vendor="apple")]
    picked = hardware.pick_accelerator(gpus, 32 * GIB)
    assert picked["unified"] is True
    assert picked["vram_is_estimate"] is True
    assert picked["vendor"] == "apple"


# --------------------------------------------------------------------- tiers

@pytest.mark.parametrize("vram_mib,tier", [
    (49140, "workstation"),   # 48 GB card
    (24564, "enthusiast"),    # a "24 GB" card reports under 24 GiB
    (16376, "high"),
    (12288, "performance"),
    (8188, "mainstream"),
    (6144, "entry"),
    (4096, "minimal"),
    (256, "minimal"),
])
def test_tier_for_a_discrete_gpu(vram_mib, tier):
    result = hardware.tier_for({"backend": "cuda", "vram_total_bytes": vram_mib * MIB},
                               64 * GIB)
    assert result["id"] == tier
    assert result["inferred"] is True, "a tier is a label, not a measurement"
    assert result["label"] and result["summary"]


def test_tier_for_cpu_only_ignores_any_vram_figure():
    result = hardware.tier_for({"backend": "cpu", "vram_total_bytes": 24 * GIB}, 64 * GIB)
    assert result["id"] == "cpu"
    assert result["inferred"] is True


def test_tiers_are_ordered_from_largest_to_smallest():
    floors = [row[0] for row in hardware.TIERS]
    assert floors == sorted(floors, reverse=True)
    assert len({row[1] for row in hardware.TIERS}) == len(hardware.TIERS)


# -------------------------------------------------------------- cpu features

def test_cpu_features_lists_only_what_the_flags_say():
    flags = {"fpu", "sse4_2", "avx", "avx2", "fma", "f16c", "avx512f", "avx_vnni"}
    assert hardware.cpu_features(flags, "x86_64") == [
        "AVX", "AVX2", "AVX-512", "AVX-VNNI", "FMA", "F16C", "SSE4.2"]
    assert hardware.cpu_features({"fpu", "sse2"}, "x86_64") == []


def test_cpu_features_on_arm():
    assert hardware.cpu_features({"asimd", "sve"}, "aarch64") == ["NEON", "SVE"]
    # macOS publishes no flag list; every arm64 CPU has NEON.
    assert hardware.cpu_features(set(), "arm64") == ["NEON"]
    assert hardware.cpu_features(set(), "ARM64") == ["NEON"]
    assert hardware.cpu_features(set(), "AMD64") == []


# ------------------------------------------------ the real collectors, once

PROFILE_KEYS = {"collected_at", "elapsed_ms", "os", "cpu", "memory", "gpus",
                "accelerator", "volumes", "tier", "notes", "probes", "psutil"}
SAMPLE_KEYS = {"t", "cpu", "memory", "gpus", "disk_io", "net_io", "volumes",
               "top_processes"}
ACCELERATOR_KEYS = {"backend", "name", "vendor", "vram_total_bytes",
                    "pooled_vram_bytes", "unified", "vram_is_estimate",
                    "gpu_index", "gpu_count"}


@pytest.fixture
def clean_hardware_caches():
    hardware.reset()
    yield
    hardware.reset()


def test_collect_profile_returns_the_documented_shape_on_this_machine(
        clean_hardware_caches):
    profile = hardware.collect_profile()
    assert set(profile) == PROFILE_KEYS
    assert set(profile["accelerator"]) == ACCELERATOR_KEYS
    assert {"platform", "name", "version", "arch", "python"} <= set(profile["os"])
    assert {"model", "vendor", "arch", "physical_cores", "logical_cores",
            "features"} <= set(profile["cpu"])
    assert profile["cpu"]["logical_cores"] >= 1
    assert set(profile["memory"]) == {"total_bytes", "available_bytes",
                                      "swap_total_bytes"}
    assert isinstance(profile["gpus"], list)
    assert isinstance(profile["volumes"], list)
    assert isinstance(profile["notes"], list)
    assert profile["tier"]["inferred"] is True
    assert isinstance(profile["psutil"], bool)
    for row in profile["probes"]:
        assert set(row) == {"name", "ok", "ms"}
    # Identifiers that could single out the machine never leave the collector.
    for gpu in profile["gpus"]:
        assert "uuid" not in gpu
        assert "device_path" not in gpu
    for volume in profile["volumes"]:
        assert {"mount", "total_bytes", "free_bytes", "used_bytes", "kind",
                "roles"} <= set(volume)
        assert volume["total_bytes"] > 0


def test_collect_profile_marks_the_volume_that_holds_a_named_path(
        clean_hardware_caches, tmp_path):
    profile = hardware.collect_profile({str(tmp_path): "Models"})
    assert any("Models" in v["roles"] for v in profile["volumes"])


def test_collect_sample_returns_the_documented_shape_on_this_machine(
        clean_hardware_caches):
    static = hardware.collect_profile()
    reading = hardware.collect_sample(static)
    assert set(reading) == SAMPLE_KEYS
    assert {"percent", "per_core", "freq_mhz", "load_1m", "temp_c"} == set(reading["cpu"])
    assert {"total_bytes", "used_bytes", "available_bytes", "percent",
            "swap_total_bytes", "swap_used_bytes"} == set(reading["memory"])
    assert set(reading["disk_io"]) == {"read_bps", "write_bps"}
    assert set(reading["net_io"]) == {"rx_bps", "tx_bps"}
    assert len(reading["gpus"]) == len(static["gpus"])
    for gpu in reading["gpus"]:
        assert {"index", "name", "util_percent", "vram_used_bytes",
                "vram_total_bytes", "temp_c", "power_w", "fan_percent",
                "processes"} <= set(gpu)
    for row in reading["top_processes"]:
        assert set(row) == {"pid", "name", "rss_bytes"}


def test_profile_is_cached_and_sample_can_carry_history(clean_hardware_caches):
    first = hardware.profile()
    assert hardware.profile() is first, "second call is served from the cache"
    reading = hardware.sample(history=True)
    assert set(reading) >= SAMPLE_KEYS
    assert isinstance(reading["history"], list)
    assert len(reading["history"]) == 1
    assert set(reading["history"][0]) == {"t", "cpu", "mem", "gpu", "vram", "disk_r",
                                          "disk_w", "net_rx", "net_tx"}
    assert "history" not in hardware.sample()
