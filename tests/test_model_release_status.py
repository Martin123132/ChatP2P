import json

from chatp2p.cli import build_parser
from chatp2p.model_governance import default_model_governance_registry
from chatp2p.model_registry import default_model_registry
from chatp2p.model_release_status import (
    MODEL_RELEASE_STATUS_REPORT_SCHEMA,
    ModelReleaseStatusConfig,
    run_model_release_status,
)


VALID_SHA_A = "a" * 64
VALID_SHA_B = "b" * 64


def test_model_release_status_missing_pack_recommends_candidate_pack(tmp_path):
    report = run_model_release_status(
        ModelReleaseStatusConfig(
            pack_dir=tmp_path / "missing-pack",
            governance_path=tmp_path / "model-governance.json",
            out_dir=tmp_path / "status",
            model_id="qwen2.5-7b-instruct",
        )
    )

    assert report["schema"] == MODEL_RELEASE_STATUS_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["status"] == "warn"
    assert report["summary"]["pipeline_stage"] == "candidate_pack_needed"
    assert report["summary"]["recommended_next_action"] == "run_model_candidate_pack"
    assert report["next_action"]["id"] == "candidate_pack"
    assert (tmp_path / "status" / "model-release-status.json").exists()
    assert (tmp_path / "status" / "model-release-status.md").exists()


def test_model_release_status_ready_candidate_points_to_bundle(tmp_path):
    pack_dir, governance_path = _write_pack(tmp_path, model=_ready_model())

    report = run_model_release_status(
        ModelReleaseStatusConfig(
            pack_dir=pack_dir,
            governance_path=governance_path,
            out_dir=tmp_path / "status",
        )
    )

    assert report["ok"] is True
    assert report["status"] == "pass"
    assert report["summary"]["release_ready"] is True
    assert report["summary"]["pipeline_stage"] == "release_bundle_needed"
    assert report["summary"]["recommended_next_action"] == "run_release_bundle_then_review_promotion"
    assert report["summary"]["write_flag_required_after_review"] is False


def test_model_release_status_runtime_and_artifact_blockers_are_ordered(tmp_path):
    pack_dir, governance_path = _write_pack(tmp_path, model=_ready_model(runtime_verified=False, artifact_ready=False))

    report = run_model_release_status(
        ModelReleaseStatusConfig(
            pack_dir=pack_dir,
            governance_path=governance_path,
            out_dir=tmp_path / "status",
        )
    )

    assert {"runtime", "artifacts"}.issubset(set(report["summary"]["blocked_gate_ids"]))
    assert report["summary"]["pipeline_stage"] == "runtime_check_needed"
    assert report["summary"]["recommended_next_action"] == "run_model_runtime_check"


def test_model_release_status_redacts_sensitive_report_values(tmp_path):
    pack_dir, governance_path = _write_pack(tmp_path, model=_ready_model(runtime_verified=False))
    runtime_report = tmp_path / "runtime.json"
    secret = "alpha" + "-token-release-status-secret-123456"
    runtime_report.write_text(
        json.dumps(
            {
                "schema": "chatp2p.model-runtime-check-report.v1",
                "ok": True,
                "status": "pass",
                "summary": {"runtime_verified": True, "recommended_next_action": f"use {secret}"},
                "private": {"admission_token": secret},
            }
        ),
        encoding="utf-8",
    )

    report = run_model_release_status(
        ModelReleaseStatusConfig(
            pack_dir=pack_dir,
            governance_path=governance_path,
            out_dir=tmp_path / "status",
            runtime_report_path=runtime_report,
        )
    )

    serialized = json.dumps(report)
    assert secret not in serialized
    assert "<redacted>" in serialized
    assert report["summary"]["recommended_next_action"] == "review_runtime_report_then_attach_verified_runtime"


def test_model_release_status_parser_accepts_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "release-status",
            "--pack",
            "D:\\ChatP2PData\\model-candidate-pack",
            "--governance",
            "D:\\ChatP2PData\\model-governance.json",
            "--out",
            "D:\\ChatP2PData\\model-release-status",
            "--model-id",
            "qwen2.5-7b-instruct",
            "--runtime-report",
            "D:\\ChatP2PData\\model-runtime-check.json",
            "--artifact-report",
            "D:\\ChatP2PData\\model-artifact-manifest.json",
            "--eval-report",
            "D:\\ChatP2PData\\model-eval-report.json",
            "--governance-pack-report",
            "D:\\ChatP2PData\\model-governance-pack.json",
            "--governance-review-report",
            "D:\\ChatP2PData\\model-governance-review.json",
            "--bundle-report",
            "D:\\ChatP2PData\\model-release-bundle.json",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_release_status_command"
    assert args.command == "model"
    assert args.model_command == "release-status"
    assert args.model_id == "qwen2.5-7b-instruct"
    assert args.json is True


def _write_pack(tmp_path, *, model):
    pack_dir = tmp_path / "model-candidate-pack"
    pack_dir.mkdir()
    (pack_dir / "eval").mkdir()
    registry = default_model_registry()
    registry["models"][0] = model
    governance = _ready_governance_registry()
    registry_path = pack_dir / "staging-model-registry.json"
    governance_path = tmp_path / "model-governance.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    governance_path.write_text(json.dumps(governance, indent=2), encoding="utf-8")
    (pack_dir / "model-candidate-pack.json").write_text(
        json.dumps({"summary": {"selected_model_id": model["id"]}, "selected_candidate": {"id": model["id"]}}),
        encoding="utf-8",
    )
    (pack_dir / "eval" / "model-eval-report.json").write_text(
        json.dumps({"schema": "chatp2p.model-eval-report.v1", "ok": True, "status": "pass"}),
        encoding="utf-8",
    )
    (pack_dir / "eval-attach-report.json").write_text(
        json.dumps({"schema": "chatp2p.model-eval-attach-report.v1", "ok": True, "status": "pass"}),
        encoding="utf-8",
    )
    return pack_dir, governance_path


def _ready_model(*, artifact_ready=True, runtime_verified=True):
    return {
        "id": "qwen2.5-7b-instruct",
        "status": "proposal",
        "provider": "Qwen",
        "project": "Qwen2.5-7B-Instruct",
        "family": "base_chat_model",
        "variant": "Qwen2.5-7B-Instruct",
        "license": "Apache-2.0",
        "license_url": "https://example.invalid/license",
        "source_url": "https://example.invalid/model",
        "parameter_count_b": 7.61,
        "architecture": "transformer",
        "context_length_tokens": 131072,
        "domains": ["general", "coding"],
        "runtimes": [
            {
                "id": "ollama",
                "support_status": "verified" if runtime_verified else "candidate",
                "notes": "local smoke passed" if runtime_verified else "local smoke pending",
            }
        ],
        "hardware": {
            "min_ram_gb": 16,
            "min_vram_gb": 8,
            "recommended_capability_tier": "gaming_laptop",
        },
        "artifacts": {
            "manifest_sha256": VALID_SHA_A if artifact_ready else "TBD",
            "weights_sha256": VALID_SHA_B if artifact_ready else "TBD",
            "quantization": "q4_k_m" if artifact_ready else "TBD",
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
            "proposal_id": "qwen-governance-review-v0",
            "review_status": "approved",
            "rollback_plan": "restore previous approved model route",
            "approved_by": ["domain_steward_fixture"],
        },
    }


def _ready_governance_registry():
    registry = default_model_governance_registry()
    registry["weight_packs"][0] = {
        "id": "qwen-governance-pack-v0",
        "type": "base_model",
        "status": "approved",
        "base_model": "qwen2.5-7b-instruct",
        "license": "Apache-2.0",
        "domains": ["general", "coding"],
        "allowed_runtimes": ["ollama"],
        "manifest_sha256": VALID_SHA_A,
        "weights_sha256": VALID_SHA_B,
        "core_weight_editable": False,
        "promotion_gate": "passed_eval_and_governance_review",
    }
    return registry
