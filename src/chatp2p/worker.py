"""Local worker node implementation."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .benchmark import capabilities_from_benchmark, collect_hardware_profile
from .crypto import NodeIdentity
from .packets import JobPacket, JobResult


def collect_hardware_attestation() -> dict[str, Any]:
    """Collect a small, non-invasive local capability report."""

    return collect_hardware_profile()


@dataclass
class WorkerNode:
    identity: NodeIdentity
    capability_profile: dict[str, Any] | None = None

    def capabilities(self) -> dict[str, Any]:
        if self.capability_profile is not None:
            return self.capability_profile
        return capabilities_from_benchmark(
            {
                "hardware": collect_hardware_attestation(),
                "gpu": {"available": False, "provider": None, "devices": [], "total_vram_mb": None},
                "benchmark": {"cpu_iterations_per_second": 0},
                "model_runtimes": {},
            }
        )

    def run_job(self, job: JobPacket) -> JobResult:
        if not job.verify_signature():
            raise ValueError("Refusing unsigned or tampered job packet")
        if not job.verify_payload_hash():
            raise ValueError("Refusing job with mismatched payload hash")
        if job.deadline < time.time():
            raise ValueError("Refusing expired job")

        started = time.perf_counter()
        output = self._dispatch(job)
        runtime = time.perf_counter() - started

        return JobResult.create(
            node=self.identity,
            job=job,
            output=output,
            metrics={"accepted": True},
            runtime_seconds=runtime,
            hardware_attestation=collect_hardware_attestation(),
        )

    def _dispatch(self, job: JobPacket) -> dict[str, Any]:
        if job.job_type == "eval.math.v1":
            return self._run_math_eval(job.payload)
        if job.job_type == "eval.deterministic.v1":
            return self._run_deterministic_eval(job.payload)
        if job.job_type == "inference.echo.v1":
            return {
                "answer": job.payload.get("prompt", ""),
                "confidence": 1.0,
                "notes": "Echo inference is a transport and signing smoke test.",
            }
        raise ValueError(f"Unsupported job type: {job.job_type}")

    def _run_math_eval(self, payload: dict[str, Any]) -> dict[str, Any]:
        expression = payload.get("expression")
        expected = payload.get("expected")
        if expression != "2 + 2":
            return {
                "passed": False,
                "answer": None,
                "confidence": 0.0,
                "error": "This MVP worker only supports the first deterministic eval.",
            }

        answer = 4
        return {
            "passed": answer == expected,
            "answer": answer,
            "expected": expected,
            "confidence": 1.0,
            "reasoning": "Integer arithmetic: 2 + 2 = 4.",
        }

    def _run_deterministic_eval(self, payload: dict[str, Any]) -> dict[str, Any]:
        task = payload.get("task")
        expected = payload.get("expected")

        if task == "arithmetic":
            answer = self._run_arithmetic(payload)
        elif task == "number_theory" and payload.get("check") == "is_prime":
            answer = self._is_prime(int(payload["value"]))
        elif task == "text" and payload.get("operation") == "normalize_whitespace":
            answer = " ".join(str(payload.get("value", "")).split())
        else:
            return {
                "passed": False,
                "answer": None,
                "expected": expected,
                "confidence": 0.0,
                "error": f"Unsupported deterministic eval payload: {payload}",
            }

        return {
            "passed": answer == expected,
            "answer": answer,
            "expected": expected,
            "confidence": 1.0,
            "reasoning": f"Deterministic worker executed task '{task}'.",
        }

    def _run_arithmetic(self, payload: dict[str, Any]) -> int | float:
        operands = payload.get("operands", [])
        if len(operands) != 2:
            raise ValueError("Arithmetic eval requires exactly two operands")
        left, right = operands
        operation = payload.get("operation")
        if operation == "add":
            return left + right
        if operation == "subtract":
            return left - right
        if operation == "multiply":
            return left * right
        if operation == "divide":
            return left / right
        raise ValueError(f"Unsupported arithmetic operation: {operation}")

    def _is_prime(self, value: int) -> bool:
        if value < 2:
            return False
        if value == 2:
            return True
        if value % 2 == 0:
            return False
        divisor = 3
        while divisor * divisor <= value:
            if value % divisor == 0:
                return False
            divisor += 2
        return True
