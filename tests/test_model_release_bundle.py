import json

from chatp2p.cli import build_parser
from chatp2p.model_governance import default_model_governance_registry
from chatp2p.model_registry import default_model_registry
from chatp2p.model_release_bundle import (
    MODEL_RELEASE_BUNDLE_REPORT_SCHEMA,
    ModelReleaseBundleConfig,
    run_model_release_bundle,
)


VALID_SHA_A = "a" * 64
VALID_SHA_B = "b" * 64


def test_model_release_bundle_ready_candidate_writes_dossier(tmp_path):
    registry_path, governance_path = _write_ready_inputs(tmp_path)
    runtime_report = _write_evidence(tmp_path, "runtime.json", "chatp2p.model-runtime-check-report.v1")
    artifact_report = _write_evidence(tmp_path, "artifact.json", "chatp2p.model-artifact-manifest-report.v1")
    eval_report = _write_evidence(tmp_path, "eval.json", "chatp2p.model-eval-report.v1")
    pack_report = _write_evidence(tmp_path, "governance-pack.json", "chatp2p.model-governance-pack-report.v1")
    review_report = _write_evidence(tmp_path, "governance-review.json", "chatp2p.model-governance-review-report.v1")

    report = run_model_release_bundle(
        ModelReleaseBundleConfig(
            registry_path=registry_path,
            governance_path=governance_path,
            model_id="chatp2p-base-ready-v0",
            out_dir=tmp_path / "bundle",
            runtime_report_path=runtime_report,
            artifact_report_path=artifact_report,
            eval_report_path=eval_report,
            governance_pack_report_path=pack_report,
            governance_review_report_path=review_report,
        )
    )

    assert report["schema"] == MODEL_RELEASE_BUNDLE_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["status"] == "pass"
    assert report["summary"]["release_ready"] is True
    assert report["summary"]["evidence_ok_count"] == 5
    assert report["summary"]["configured_evidence_count"] == 5
    assert report["summary"]["missing_or_error_evidence_ids"] == []
    assert report["summary"]["recommended_next_action"] == "review_release_bundle_then_run_release_promote"
    assert report["promotion"]["requires_explicit_write"] is True
    assert report["promotion"]["requires_confirm_release_ready"] is True
    assert any(gate["id"] == "runtime" and gate["status"] == "pass" for gate in report["gates"])
    assert (tmp_path / "bundle" / "model-release-bundle.json").exists()
    assert (tmp_path / "bundle" / "model-release-bundle.md").exists()


def test_model_release_bundle_warns_for_missing_configured_evidence(tmp_path):
    registry_path, governance_path = _write_ready_inputs(tmp_path)

    report = run_model_release_bundle(
        ModelReleaseBundleConfig(
            registry_path=registry_path,
            governance_path=governance_path,
            model_id="chatp2p-base-ready-v0",
            out_dir=tmp_path / "bundle",
            runtime_report_path=tmp_path / "missing-runtime.json",
        )
    )

    assert report["ok"] is True
    assert report["status"] == "warn"
    assert report["evidence"]["runtime_check"]["status"] == "missing"
    assert report["summary"]["missing_or_error_evidence_ids"] == ["runtime_check"]
    assert report["summary"]["recommended_next_action"] == "review_missing_release_evidence_runtime_check"


def test_model_release_bundle_reports_release_blockers_without_failing(tmp_path):
    registry_path = tmp_path / "model-registry.json"
    governance_path = tmp_path / "model-governance.json"
    registry_path.write_text(json.dumps(default_model_registry(), indent=2), encoding="utf-8")
    governance_path.write_text(json.dumps(default_model_governance_registry(), indent=2), encoding="utf-8")

    report = run_model_release_bundle(
        ModelReleaseBundleConfig(
            registry_path=registry_path,
            governance_path=governance_path,
            model_id="chatp2p-base-candidate-v0",
            out_dir=tmp_path / "bundle",
        )
    )

    assert report["ok"] is True
    assert report["status"] == "warn"
    assert report["summary"]["release_ready"] is False
    assert "license" in report["summary"]["blocked_gate_ids"]
    assert report["summary"]["recommended_next_action"] == "resolve_release_check_blockers"


def test_model_release_bundle_redacts_sensitive_evidence_summary(tmp_path):
    registry_path, governance_path = _write_ready_inputs(tmp_path)
    runtime_report = tmp_path / "runtime.json"
    secret = "alpha" + "-token-release-bundle-secret-123456"
    runtime_report.write_text(
        json.dumps(
            {
                "schema": "chatp2p.model-runtime-check-report.v1",
                "ok": True,
                "status": "pass",
                "summary": {"recommended_next_action": f"use {secret}"},
            }
        ),
        encoding="utf-8",
    )

    report = run_model_release_bundle(
        ModelReleaseBundleConfig(
            registry_path=registry_path,
            governance_path=governance_path,
            model_id="chatp2p-base-ready-v0",
            out_dir=tmp_path / "bundle",
            runtime_report_path=runtime_report,
        )
    )

    serialized = json.dumps(report)
    assert secret not in serialized
    assert "<redacted>" in serialized


def test_model_release_bundle_parser_accepts_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "release-bundle",
            "--registry",
            "D:\\ChatP2PData\\model-registry.json",
            "--governance",
            "D:\\ChatP2PData\\model-governance.json",
            "--model-id",
            "chatp2p-base-ready-v0",
            "--out",
            "D:\\ChatP2PData\\model-release-bundle",
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
            "--json",
        ]
    )

    assert args.func.__name__ == "model_release_bundle_command"
    assert args.command == "model"
    assert args.model_command == "release-bundle"
    assert args.model_id == "chatp2p-base-ready-v0"
    assert args.json is True


def _write_evidence(tmp_path, name, schema):
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {
                "schema": schema,
                "ok": True,
                "status": "pass",
                "summary": {
                    "does_not_approve_model": True,
                    "recommended_next_action": "continue_release_review",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_ready_inputs(tmp_path):
    registry = default_model_registry()
    registry["models"][0] = _ready_model()
    governance = _ready_governance_registry()
    registry_path = tmp_path / "model-registry.json"
    governance_path = tmp_path / "model-governance.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    governance_path.write_text(json.dumps(governance, indent=2), encoding="utf-8")
    return registry_path, governance_path


def _ready_model():
    return {
        "id": "chatp2p-base-ready-v0",
        "status": "proposal",
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
        "runtimes": [{"id": "ollama", "support_status": "verified", "notes": "local smoke passed"}],
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
        "allowed_runtimes": ["ollama"],
        "manifest_sha256": VALID_SHA_A,
        "weights_sha256": VALID_SHA_B,
        "core_weight_editable": False,
        "promotion_gate": "passed_eval_and_governance_review",
    }
    return registry
