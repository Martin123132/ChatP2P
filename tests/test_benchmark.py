import json

from chatp2p.benchmark import (
    capabilities_from_benchmark,
    classify_capability_tier,
    load_node_capabilities,
    run_node_benchmark,
    save_node_benchmark,
    tier_meets_requirement,
)
from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.worker import WorkerNode


def _benchmark_report(
    *,
    cpu_count=4,
    ram_total_mb=8_000,
    cpu_score=10_000,
    gpu_vram_mb=0,
    ollama_available=False,
):
    gpu_available = gpu_vram_mb > 0
    report = {
        "hardware": {
            "cpu_count": cpu_count,
            "ram_total_mb": ram_total_mb,
            "system": "TestOS",
        },
        "gpu": {
            "available": gpu_available,
            "provider": "nvidia" if gpu_available else None,
            "devices": [{"name": "Test GPU", "vram_mb": gpu_vram_mb}] if gpu_available else [],
            "total_vram_mb": gpu_vram_mb if gpu_available else None,
        },
        "benchmark": {"cpu_iterations_per_second": cpu_score},
        "model_runtimes": {
            "ollama": {"available": ollama_available, "path": "/usr/bin/ollama" if ollama_available else None}
        },
    }
    report["capability_tier"] = classify_capability_tier(report)
    return report


def test_capability_tiers_classify_common_machine_shapes():
    assert classify_capability_tier(_benchmark_report(cpu_count=1, ram_total_mb=2_000, cpu_score=500)) == "light"
    assert classify_capability_tier(_benchmark_report()) == "standard"
    assert classify_capability_tier(_benchmark_report(cpu_count=8, ram_total_mb=16_000)) == "gaming_laptop"
    assert classify_capability_tier(_benchmark_report(gpu_vram_mb=24_000)) == "gpu_worker"


def test_saved_benchmark_loads_as_worker_capabilities(tmp_path):
    report = _benchmark_report(cpu_count=8, ram_total_mb=16_000, ollama_available=True)
    report["capabilities"] = capabilities_from_benchmark(report)

    save_node_benchmark(report, tmp_path / "node-capabilities.json")
    capabilities = load_node_capabilities(tmp_path)

    assert capabilities is not None
    assert capabilities["capability_tier"] == "gaming_laptop"
    assert capabilities["hardware"]["capability_tier"] == "gaming_laptop"
    assert "eval.deterministic.v1" in capabilities["supported_job_types"]
    assert "inference.ollama.v1" in capabilities["supported_job_types"]


def test_node_benchmark_report_is_json_serializable():
    report = run_node_benchmark(cpu_duration_seconds=0)

    assert report["schema"] == "chatp2p.node-benchmark.v1"
    assert report["capability_tier"] in {"light", "standard", "gaming_laptop", "gpu_worker"}
    assert report["capabilities"]["capability_tier"] == report["capability_tier"]
    json.dumps(report)


def test_worker_uses_saved_capability_profile():
    capabilities = capabilities_from_benchmark(_benchmark_report(cpu_count=8, ram_total_mb=16_000))
    worker = WorkerNode(
        identity=NodeIdentity.generate(prefix="worker"),
        capability_profile=capabilities,
    )

    assert worker.capabilities()["capability_tier"] == "gaming_laptop"


def test_coordinator_respects_min_capability_tier_requirement():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    light_identity = NodeIdentity.generate(prefix="worker")
    gaming_identity = NodeIdentity.generate(prefix="worker")

    coordinator.register_node(
        light_identity.public(),
        capabilities=capabilities_from_benchmark(_benchmark_report(cpu_count=1, ram_total_mb=2_000, cpu_score=500)),
    )
    coordinator.register_node(
        gaming_identity.public(),
        capabilities=capabilities_from_benchmark(_benchmark_report(cpu_count=8, ram_total_mb=16_000)),
    )
    job = coordinator.create_job(
        job_type="eval.deterministic.v1",
        payload={
            "task": "arithmetic",
            "operation": "add",
            "operands": [3, 4],
            "expected": 7,
        },
        resource_requirements={"cpu": "tiny", "min_capability_tier": "gaming_laptop"},
    )

    assert coordinator.lease_next_job(light_identity.node_id) is None
    leased = coordinator.lease_next_job(gaming_identity.node_id)
    assert leased is not None
    assert leased.job_id == job.job_id
    assert tier_meets_requirement("gaming_laptop", "standard") is True
    assert tier_meets_requirement("light", "standard") is False
