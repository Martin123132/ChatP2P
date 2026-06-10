import json

from chatp2p.cli import build_parser
from chatp2p.model_governance import default_model_governance_registry
from chatp2p.model_registry import default_model_registry
from chatp2p.model_release import (
    MODEL_RELEASE_CHECK_REPORT_SCHEMA,
    ModelReleaseCheckConfig,
    run_model_release_check,
)


VALID_SHA_A = "a" * 64
VALID_SHA_B = "b" * 64


def test_model_release_check_default_candidate_reports_blockers(tmp_path):
    registry_path = tmp_path / "model-registry.json"
    governance_path = tmp_path / "model-governance.json"
    registry_path.write_text(json.dumps(default_model_registry(), indent=2), encoding="utf-8")
    governance_path.write_text(json.dumps(default_model_governance_registry(), indent=2), encoding="utf-8")

    report = run_model_release_check(
        ModelReleaseCheckConfig(
            registry_path=registry_path,
            governance_path=governance_path,
            model_id="chatp2p-base-candidate-v0",
            out_path=tmp_path / "release-check.json",
        )
    )

    assert report["schema"] == MODEL_RELEASE_CHECK_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["status"] == "warn"
    assert report["summary"]["release_ready"] is False
    assert "license" in report["summary"]["blocked_gate_ids"]
    assert "eval_evidence" in report["summary"]["blocked_gate_ids"]
    assert report["summary"]["recommended_next_action"] == "confirm_model_license"
    assert report["summary"]["does_not_approve_model"] is True
    assert (tmp_path / "release-check.json").exists()


def test_model_release_check_ready_proposal_passes_without_approving(tmp_path):
    registry = default_model_registry()
    registry["models"][0] = _ready_model(status="proposal")
    governance = _ready_governance_registry()
    registry_path = tmp_path / "model-registry.json"
    governance_path = tmp_path / "model-governance.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    governance_path.write_text(json.dumps(governance, indent=2), encoding="utf-8")

    report = run_model_release_check(
        ModelReleaseCheckConfig(
            registry_path=registry_path,
            governance_path=governance_path,
            model_id="chatp2p-base-ready-v0",
        )
    )
    stored = json.loads(registry_path.read_text(encoding="utf-8"))

    assert report["ok"] is True
    assert report["status"] == "pass"
    assert report["summary"]["release_ready"] is True
    assert report["summary"]["failed_gate_count"] == 0
    assert report["summary"]["recommended_next_action"] == "promote_model_through_governance_release"
    assert report["summary"]["does_not_approve_model"] is True
    assert stored["models"][0]["status"] == "proposal"


def test_model_release_check_missing_model_fails(tmp_path):
    registry_path = tmp_path / "model-registry.json"
    governance_path = tmp_path / "model-governance.json"
    registry_path.write_text(json.dumps(default_model_registry()), encoding="utf-8")
    governance_path.write_text(json.dumps(default_model_governance_registry()), encoding="utf-8")

    report = run_model_release_check(
        ModelReleaseCheckConfig(
            registry_path=registry_path,
            governance_path=governance_path,
            model_id="missing-model",
        )
    )

    assert report["ok"] is False
    assert report["status"] == "fail"
    assert "model_exists" in report["summary"]["blocked_gate_ids"]
    assert any("model_id not found" in error for error in report["errors"])


def test_model_release_check_blocks_missing_governance_pack(tmp_path):
    registry = default_model_registry()
    registry["models"][0] = _ready_model(status="proposal")
    governance = default_model_governance_registry()
    registry_path = tmp_path / "model-registry.json"
    governance_path = tmp_path / "model-governance.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    governance_path.write_text(json.dumps(governance, indent=2), encoding="utf-8")

    report = run_model_release_check(
        ModelReleaseCheckConfig(
            registry_path=registry_path,
            governance_path=governance_path,
            model_id="chatp2p-base-ready-v0",
        )
    )

    assert report["ok"] is True
    assert report["status"] == "warn"
    assert report["summary"]["release_ready"] is False
    assert "governance_weight_pack" in report["summary"]["blocked_gate_ids"]
    assert report["summary"]["recommended_next_action"] == "approve_matching_governance_weight_pack"


def test_model_release_check_redacts_sensitive_registry_values(tmp_path):
    registry = default_model_registry()
    token = "alpha-token-model-release-secret-123456"
    registry["models"][0]["source_url"] = f"https://example.invalid/model?token={token}"
    registry_path = tmp_path / "model-registry.json"
    governance_path = tmp_path / "model-governance.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    governance_path.write_text(json.dumps(default_model_governance_registry(), indent=2), encoding="utf-8")

    report = run_model_release_check(
        ModelReleaseCheckConfig(
            registry_path=registry_path,
            governance_path=governance_path,
            model_id="chatp2p-base-candidate-v0",
        )
    )
    serialized = json.dumps(report)

    assert report["ok"] is False
    assert token not in serialized
    assert any("sensitive value detected" in error for error in report["errors"])


def test_model_release_check_parser_accepts_required_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "release-check",
            "--registry",
            "D:\\ChatP2PData\\model-registry.json",
            "--governance",
            "D:\\ChatP2PData\\model-governance.json",
            "--model-id",
            "chatp2p-base-ready-v0",
            "--out",
            "D:\\ChatP2PData\\model-release-check.json",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_release_check_command"
    assert args.command == "model"
    assert args.model_command == "release-check"
    assert args.model_id == "chatp2p-base-ready-v0"
    assert args.json is True


def _ready_model(*, status):
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


def _ready_governance_registry():
    registry = default_model_governance_registry()
    registry["weight_packs"][0] = {
        "id": "chatp2p-base-ready-v0-pack",
        "type": "base_model",
        "status": "approved",
        "base_model": "chatp2p-base-ready-v0",
        "license": "Example-Permissive-License",
        "domains": ["general", "coding"],
        "allowed_runtimes": ["ollama", "llama.cpp"],
        "manifest_sha256": VALID_SHA_A,
        "weights_sha256": VALID_SHA_B,
        "core_weight_editable": False,
        "promotion_gate": "passed_eval_and_governance_review",
    }
    return registry
