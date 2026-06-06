import json

from chatp2p.cli import build_parser
from chatp2p.model_governance import (
    MODEL_GOVERNANCE_DEFAULT_REGISTRY_ID,
    MODEL_GOVERNANCE_REGISTRY_SCHEMA,
    MODEL_GOVERNANCE_REPORT_SCHEMA,
    ModelGovernanceConfig,
    default_model_governance_registry,
    run_model_governance,
    validate_model_governance_registry,
)


def test_model_governance_default_registry_is_strict_but_not_ready_to_serve():
    registry = default_model_governance_registry()
    validation = validate_model_governance_registry(registry)

    assert registry["schema"] == MODEL_GOVERNANCE_REGISTRY_SCHEMA
    assert registry["registry_id"] == MODEL_GOVERNANCE_DEFAULT_REGISTRY_ID
    assert validation["ok"] is True
    assert validation["summary"]["tier_count"] == 5
    assert validation["summary"]["adapter_submissions_enabled"] is True
    assert validation["summary"]["core_weight_edits_allowed"] is False
    assert validation["summary"]["approved_weight_pack_count"] == 0
    assert validation["summary"]["placeholder_hash_count"] == 2
    assert any("placeholder manifest_sha256" in warning for warning in validation["warnings"])


def test_model_governance_init_writes_report_without_private_material(tmp_path):
    registry_path = tmp_path / "model-governance.json"
    report_path = tmp_path / "model-governance-report.json"

    report = run_model_governance(
        ModelGovernanceConfig(
            registry_path=registry_path,
            out_path=report_path,
            init=True,
        )
    )

    assert report["schema"] == MODEL_GOVERNANCE_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["status"] == "warn"
    assert report["init"]["status"] == "written"
    assert report["summary"]["recommended_next_action"] == "choose_first_open_weight_base_model"
    assert registry_path.exists()
    assert report_path.exists()
    serialized = json.dumps(report)
    assert "alpha-token" not in serialized
    assert "credit-grant-token" not in serialized
    assert "BEGIN PRIVATE KEY" not in serialized


def test_model_governance_rejects_core_weight_editing(tmp_path):
    registry = default_model_governance_registry()
    registry["weight_pack_policy"]["core_weight_edits_allowed"] = True
    registry["weight_packs"][0]["core_weight_editable"] = True
    registry_path = tmp_path / "bad-governance.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    report = run_model_governance(ModelGovernanceConfig(registry_path=registry_path))

    assert report["ok"] is False
    assert report["status"] == "fail"
    assert "core weight edits must remain disabled" in json.dumps(report["errors"])
    assert "must not allow direct core weight edits" in json.dumps(report["errors"])


def test_model_governance_rejects_missing_safety_eval_and_redacts_sensitive_values(tmp_path):
    registry = default_model_governance_registry()
    registry["adapter_policy"]["required_evaluations"] = ["domain_eval", "regression_eval"]
    registry["notes"] = "alpha-token-sensitive-fixture-123456"
    registry_path = tmp_path / "sensitive-governance.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    report = run_model_governance(ModelGovernanceConfig(registry_path=registry_path))
    serialized = json.dumps(report)

    assert report["ok"] is False
    assert report["summary"]["sensitive_finding_count"] == 1
    assert "adapter_policy.required_evaluations missing: safety_eval" in report["errors"]
    assert any("sensitive value detected at notes" in error for error in report["errors"])
    assert "alpha-token-sensitive-fixture-123456" not in serialized


def test_model_governance_parser_accepts_init_and_report_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "governance",
            "--registry",
            "D:\\ChatP2PData\\model-governance.json",
            "--out",
            "D:\\ChatP2PData\\model-governance-report.json",
            "--init",
            "--force",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_governance_command"
    assert args.command == "model"
    assert args.model_command == "governance"
    assert args.init is True
    assert args.force is True
    assert args.json is True
