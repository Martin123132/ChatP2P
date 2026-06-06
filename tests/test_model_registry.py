import json

from chatp2p.cli import build_parser
from chatp2p.model_registry import (
    MODEL_REGISTRY_DEFAULT_ID,
    MODEL_REGISTRY_REPORT_SCHEMA,
    MODEL_REGISTRY_SCHEMA,
    ModelRegistryConfig,
    default_model_registry,
    run_model_registry,
    validate_model_registry,
)


VALID_SHA_A = "a" * 64
VALID_SHA_B = "b" * 64


def test_model_registry_default_registry_is_candidate_not_approved():
    registry = default_model_registry()
    validation = validate_model_registry(registry)

    assert registry["schema"] == MODEL_REGISTRY_SCHEMA
    assert registry["registry_id"] == MODEL_REGISTRY_DEFAULT_ID
    assert validation["ok"] is True
    assert validation["summary"]["model_count"] == 1
    assert validation["summary"]["approved_model_count"] == 0
    assert validation["summary"]["best_next_candidate"] == "chatp2p-base-candidate-v0"
    assert validation["model_readiness"][0]["approval_ready"] is False
    assert "license" in validation["model_readiness"][0]["missing_for_approval"]
    assert validation["summary"]["placeholder_hash_count"] == 2


def test_model_registry_init_writes_warn_report_without_private_material(tmp_path):
    registry_path = tmp_path / "model-registry.json"
    report_path = tmp_path / "model-registry-report.json"

    report = run_model_registry(
        ModelRegistryConfig(
            registry_path=registry_path,
            out_path=report_path,
            init=True,
        )
    )

    assert report["schema"] == MODEL_REGISTRY_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["status"] == "warn"
    assert report["init"]["status"] == "written"
    assert report["summary"]["recommended_next_action"] == "fill_first_candidate_metadata"
    assert registry_path.exists()
    assert report_path.exists()
    serialized = json.dumps(report)
    assert "alpha-token" not in serialized
    assert "credit-grant-token" not in serialized
    assert "BEGIN PRIVATE KEY" not in serialized


def test_model_registry_approved_model_requires_evidence(tmp_path):
    registry = default_model_registry()
    registry["models"][0]["status"] = "approved"
    registry_path = tmp_path / "bad-approved-model.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    report = run_model_registry(ModelRegistryConfig(registry_path=registry_path))
    errors = "\n".join(report["errors"])

    assert report["ok"] is False
    assert report["status"] == "fail"
    assert "cannot be approved until license is ready" in errors
    assert "cannot be approved until artifacts is ready" in errors
    assert "cannot be approved until governance is ready" in errors


def test_model_registry_approval_ready_candidate_is_recommended_for_governance(tmp_path):
    registry = default_model_registry()
    registry["models"][0] = _approval_ready_model(status="proposal")
    registry_path = tmp_path / "approval-ready-model.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    report = run_model_registry(ModelRegistryConfig(registry_path=registry_path))

    assert report["ok"] is True
    assert report["status"] == "warn"
    assert report["summary"]["approval_ready_count"] == 1
    assert report["summary"]["approved_model_count"] == 0
    assert report["summary"]["recommended_next_action"] == "submit_candidate_for_governance_approval"
    assert report["model_readiness"][0]["approval_ready"] is True
    assert report["model_readiness"][0]["recommended_next_action"] == "submit_candidate_for_governance_approval"


def test_model_registry_approved_model_passes_when_evidence_is_complete(tmp_path):
    registry = default_model_registry()
    registry["models"][0] = _approval_ready_model(status="approved")
    registry_path = tmp_path / "approved-model.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    report = run_model_registry(ModelRegistryConfig(registry_path=registry_path))

    assert report["ok"] is True
    assert report["status"] == "pass"
    assert report["summary"]["approved_model_count"] == 1
    assert report["summary"]["placeholder_hash_count"] == 0
    assert report["summary"]["recommended_next_action"] == "publish_model_registry_for_routing"


def test_model_registry_rejects_sensitive_values_and_redacts_safe_view(tmp_path):
    registry = default_model_registry()
    registry["models"][0]["source_url"] = "https://example.invalid/model?token=alpha-token-sensitive-fixture-123456"
    registry_path = tmp_path / "sensitive-model-registry.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    report = run_model_registry(ModelRegistryConfig(registry_path=registry_path))
    serialized = json.dumps(report)

    assert report["ok"] is False
    assert report["summary"]["sensitive_finding_count"] == 1
    assert any("sensitive value detected" in error for error in report["errors"])
    assert "alpha-token-sensitive-fixture-123456" not in serialized


def test_model_registry_parser_accepts_init_and_report_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "registry",
            "--registry",
            "D:\\ChatP2PData\\model-registry.json",
            "--out",
            "D:\\ChatP2PData\\model-registry-report.json",
            "--init",
            "--force",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_registry_command"
    assert args.command == "model"
    assert args.model_command == "registry"
    assert args.init is True
    assert args.force is True
    assert args.json is True


def _approval_ready_model(*, status):
    return {
        "id": "chatp2p-base-ready-v0",
        "status": status,
        "provider": "Example Open Model Lab",
        "project": "Example Open Chat",
        "family": "base_chat_model",
        "variant": "example-8b",
        "license": "Example-Permissive-License",
        "license_url": "https://example.invalid/license",
        "source_url": "https://example.invalid/model",
        "parameter_count_b": 8,
        "architecture": "dense_transformer",
        "context_length_tokens": 8192,
        "domains": ["general", "coding"],
        "runtimes": [
            {"id": "ollama", "support_status": "verified", "notes": "local smoke passed"},
            {"id": "llama.cpp", "support_status": "candidate", "notes": "quantization pending"},
        ],
        "hardware": {
            "min_ram_gb": 16,
            "min_vram_gb": 8,
            "recommended_capability_tier": "gaming_laptop",
        },
        "artifacts": {
            "manifest_sha256": VALID_SHA_A,
            "weights_sha256": VALID_SHA_B,
            "quantization": "q4_k_m",
        },
        "eval_plan": {
            "required_evaluations": [
                "domain_eval",
                "regression_eval",
                "safety_eval",
                "license_review",
                "local_smoke",
            ],
            "success_criteria": {
                "minimum_domain_pass_rate": 0.7,
                "no_known_license_blocker": True,
                "local_chat_smoke_passes": True,
            },
            "completed_evaluations": [
                "domain_eval",
                "regression_eval",
                "safety_eval",
                "license_review",
                "local_smoke",
            ],
        },
        "governance": {
            "proposal_id": "proposal-base-ready-v0",
            "review_status": "approved",
            "rollback_plan": "deprecate pack and revert default route",
            "approved_by": ["domain_steward_fixture"],
        },
    }
