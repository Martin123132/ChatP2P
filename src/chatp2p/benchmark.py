"""Local node benchmark and capability classification."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .jsonio import read_json_file

from .ollama import DEFAULT_OLLAMA_BASE_URL, OllamaError, list_ollama_models

CAPABILITY_PROFILE_NAME = "node-capabilities.json"
CAPABILITY_TIERS = ["light", "standard", "gaming_laptop", "gpu_worker"]
DEFAULT_SUPPORTED_JOB_TYPES = ["eval.math.v1", "eval.deterministic.v1", "inference.echo.v1"]
OLLAMA_JOB_TYPE = "inference.ollama.v1"


def run_node_benchmark(
    cpu_duration_seconds: float = 0.25,
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL,
) -> dict[str, Any]:
    """Collect a small local capability report without third-party dependencies."""

    hardware = collect_hardware_profile()
    benchmark = {
        "cpu_duration_seconds": round(max(0.0, cpu_duration_seconds), 3),
        "cpu_iterations_per_second": measure_cpu_iterations(cpu_duration_seconds),
    }
    gpu = detect_gpu_profile()
    model_runtimes = detect_model_runtimes(ollama_base_url=ollama_base_url)
    report = {
        "schema": "chatp2p.node-benchmark.v1",
        "created_at": round(time.time(), 3),
        "hardware": hardware,
        "gpu": gpu,
        "benchmark": benchmark,
        "model_runtimes": model_runtimes,
    }
    report["capability_tier"] = classify_capability_tier(report)
    report["capabilities"] = capabilities_from_benchmark(report)
    return report


def collect_hardware_profile() -> dict[str, Any]:
    return {
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
        "system": platform.system(),
        "system_version": platform.version(),
        "cpu_count": os.cpu_count() or 1,
        "ram_total_mb": total_memory_mb(),
        "disk_free_mb": disk_free_mb(Path.home()),
    }


def measure_cpu_iterations(cpu_duration_seconds: float = 0.25) -> int:
    duration = max(0.0, cpu_duration_seconds)
    if duration == 0:
        return 0

    seed = b"chatp2p-benchmark"
    iterations = 0
    deadline = time.perf_counter() + duration
    while time.perf_counter() < deadline:
        seed = hashlib.sha256(seed).digest()
        iterations += 1
    return int(iterations / duration)


def total_memory_mb() -> int | None:
    if platform.system() == "Windows":
        return _windows_total_memory_mb()

    pages = getattr(os, "sysconf", lambda _name: None)("SC_PHYS_PAGES")
    page_size = getattr(os, "sysconf", lambda _name: None)("SC_PAGE_SIZE")
    if isinstance(pages, int) and isinstance(page_size, int) and pages > 0 and page_size > 0:
        return int((pages * page_size) / (1024 * 1024))
    return None


def disk_free_mb(path: Path) -> int | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return int(usage.free / (1024 * 1024))


def detect_gpu_profile() -> dict[str, Any]:
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        return {"available": False, "provider": None, "devices": [], "total_vram_mb": None}

    try:
        completed = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {"available": False, "provider": None, "devices": [], "total_vram_mb": None}

    devices = []
    total_vram_mb = 0
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        name, _, memory = line.partition(",")
        memory_mb = _parse_int(memory.strip())
        devices.append({"name": name.strip(), "vram_mb": memory_mb})
        if memory_mb is not None:
            total_vram_mb += memory_mb

    return {
        "available": bool(devices),
        "provider": "nvidia" if devices else None,
        "devices": devices,
        "total_vram_mb": total_vram_mb if devices else None,
    }


def detect_model_runtimes(ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL) -> dict[str, Any]:
    ollama = _runtime_entry("ollama")
    ollama["base_url"] = ollama_base_url
    if ollama["available"]:
        try:
            ollama["models"] = list_ollama_models(base_url=ollama_base_url)
            ollama["model_discovery_error"] = None
        except OllamaError as exc:
            ollama["models"] = []
            ollama["model_discovery_error"] = str(exc)
    else:
        ollama["models"] = []
        ollama["model_discovery_error"] = None
    return {
        "ollama": ollama,
        "llama_cpp": _first_runtime_entry(["llama-cli", "llama-server"]),
        "vllm": _python_module_available("vllm"),
    }


def classify_capability_tier(report: dict[str, Any]) -> str:
    hardware = report.get("hardware", {})
    gpu = report.get("gpu", {})
    benchmark = report.get("benchmark", {})
    cpu_count = int(hardware.get("cpu_count") or 1)
    ram_total_mb = hardware.get("ram_total_mb")
    ram_total_mb = int(ram_total_mb) if isinstance(ram_total_mb, int | float) else None
    cpu_score = int(benchmark.get("cpu_iterations_per_second") or 0)
    gpu_vram_mb = gpu.get("total_vram_mb")
    gpu_vram_mb = int(gpu_vram_mb) if isinstance(gpu_vram_mb, int | float) else 0

    if gpu.get("available") and gpu_vram_mb >= 16_000:
        return "gpu_worker"
    if gpu.get("available") or (cpu_count >= 8 and (ram_total_mb or 0) >= 16_000):
        return "gaming_laptop"
    if cpu_count >= 2 and (ram_total_mb is None or ram_total_mb >= 4_000) and cpu_score >= 1_000:
        return "standard"
    return "light"


def capabilities_from_benchmark(report: dict[str, Any]) -> dict[str, Any]:
    tier = report.get("capability_tier") or classify_capability_tier(report)
    hardware = dict(report.get("hardware", {}))
    hardware["capability_tier"] = tier
    model_runtimes = dict(report.get("model_runtimes", {}))
    ollama_models = list(model_runtimes.get("ollama", {}).get("models", []))
    supported_job_types = list(DEFAULT_SUPPORTED_JOB_TYPES)
    if model_runtimes.get("ollama", {}).get("available") and ollama_models:
        supported_job_types.append(OLLAMA_JOB_TYPE)
    return {
        "supported_job_types": supported_job_types,
        "ollama_models": ollama_models,
        "capability_tier": tier,
        "hardware": hardware,
        "benchmark": dict(report.get("benchmark", {})),
        "gpu": dict(report.get("gpu", {})),
        "model_runtimes": model_runtimes,
    }


def save_node_benchmark(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def load_node_capabilities(home: Path) -> dict[str, Any] | None:
    path = home / CAPABILITY_PROFILE_NAME
    if not path.exists():
        return None
    data = read_json_file(path, description="node capabilities file")
    if "capabilities" in data and isinstance(data["capabilities"], dict):
        return data["capabilities"]
    return capabilities_from_benchmark(data)


def tier_meets_requirement(node_tier: str | None, required_tier: str | None) -> bool:
    if not required_tier:
        return True
    if required_tier not in CAPABILITY_TIERS:
        return False
    effective_node_tier = node_tier if node_tier in CAPABILITY_TIERS else "light"
    return CAPABILITY_TIERS.index(effective_node_tier) >= CAPABILITY_TIERS.index(required_tier)


def _runtime_entry(command: str) -> dict[str, Any]:
    path = shutil.which(command)
    return {"available": path is not None, "path": path}


def _first_runtime_entry(commands: list[str]) -> dict[str, Any]:
    for command in commands:
        entry = _runtime_entry(command)
        if entry["available"]:
            entry["command"] = command
            return entry
    return {"available": False, "path": None, "command": None}


def _python_module_available(module_name: str) -> dict[str, Any]:
    try:
        __import__(module_name)
    except ImportError:
        return {"available": False}
    return {"available": True}


def _parse_int(raw: str) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


def _windows_total_memory_mb() -> int | None:
    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullTotalPhys / (1024 * 1024))
