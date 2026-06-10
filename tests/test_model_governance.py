import json

from chatp2p.cli import build_parser
from chatp2p.model_governance import (
    MODEL_GOVERNANCE_DEFAULT_REGISTRY_ID,
    MODEL_GOVERNANCE_PACK_REPORT_SCHEMA,
    MODEL_GOVERNANCE_REVIEW_REPORT_SCHEMA,
    MODEL_GOVERNANCE_REGISTRY_SCHEMA,
    MODEL_GOVERNANCE_REPORT_SCHEMA,
    ModelGovernanceConfig,
    ModelGovernancePackConfig,
    ModelGovernanceReviewConfig,
    default_model_governance_registry,
    run_model_governance,
    run_model_governance_pack,
    run_model_governance_review,
    validate_model_governance_registry,
)
from chatp2p.model_registry import default_model_registry


VALID_SHA_A = "a" * 64
VALID_SHA_B = "b" * 64


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


def test_model_governance_pack_dry_run_creates_non_editable_proposal(tmp_path):
    governance_path = tmp_path / "model-governance.json"
    model_registry_path = _write_qwen_registry(tmp_path)
    governance_path.write_text(json.dumps(default_model_governance_registry(), indent=2), encoding="utf-8")
    before = json.loads(governance_path.read_text(encoding="utf-8"))

    report = run_model_governance_pack(
        ModelGovernancePackConfig(
            governance_path=governance_path,
            model_registry_path=model_registry_path,
            model_id="qwen2.5-7b-instruct",
            out_path=tmp_path / "governance-pack.json",
        )
    )
    after = json.loads(governance_path.read_text(encoding="utf-8"))

    assert report["schema"] == MODEL_GOVERNANCE_PACK_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["operation"] == "add"
    assert report["summary"]["does_not_approve_model"] is True
    assert report["summary"]["model_registry_write"] is False
    assert report["summary"]["pack_status"] == "proposal"
    assert report["summary"]["core_weight_editable"] is False
    assert report["summary"]["recommended_next_action"] == "rerun_governance_pack_with_write_after_review"
    assert report["pack"]["status"] == "proposal"
    assert report["pack"]["base_model"] == "qwen2.5-7b-instruct"
    assert before == after
    assert (tmp_path / "governance-pack.json").exists()


def test_model_governance_pack_write_adds_proposal_and_backup(tmp_path):
    governance_path = tmp_path / "model-governance.json"
    model_registry_path = _write_qwen_registry(tmp_path)
    governance_path.write_text(json.dumps(default_model_governance_registry(), indent=2), encoding="utf-8")

    report = run_model_governance_pack(
        ModelGovernancePackConfig(
            governance_path=governance_path,
            model_registry_path=model_registry_path,
            model_id="qwen2.5-7b-instruct",
            write=True,
        )
    )
    governance = json.loads(governance_path.read_text(encoding="utf-8"))
    pack = governance["weight_packs"][-1]

    assert report["ok"] is True
    assert report["dry_run"] is False
    assert report["write"]["status"] == "written"
    assert report["summary"]["recommended_next_action"] == "review_and_promote_governance_pack_when_ready"
    assert pack["base_model"] == "qwen2.5-7b-instruct"
    assert pack["status"] == "proposal"
    assert pack["core_weight_editable"] is False
    assert pack["manifest_sha256"] == VALID_SHA_A
    assert pack["weights_sha256"] == VALID_SHA_B
    assert (tmp_path / "model-governance.json.bak").exists()


def test_model_governance_pack_can_explicitly_preview_approved_pack(tmp_path):
    governance_path = tmp_path / "model-governance.json"
    model_registry_path = _write_qwen_registry(tmp_path)
    governance_path.write_text(json.dumps(default_model_governance_registry(), indent=2), encoding="utf-8")

    report = run_model_governance_pack(
        ModelGovernancePackConfig(
            governance_path=governance_path,
            model_registry_path=model_registry_path,
            model_id="qwen2.5-7b-instruct",
            status="approved",
        )
    )

    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["pack"]["status"] == "approved"
    assert report["summary"]["does_not_approve_model"] is True


def test_model_governance_pack_blocks_missing_hashes(tmp_path):
    governance_path = tmp_path / "model-governance.json"
    model_registry_path = _write_qwen_registry(tmp_path, hashed=False)
    governance_path.write_text(json.dumps(default_model_governance_registry(), indent=2), encoding="utf-8")

    report = run_model_governance_pack(
        ModelGovernancePackConfig(
            governance_path=governance_path,
            model_registry_path=model_registry_path,
            model_id="qwen2.5-7b-instruct",
            write=True,
        )
    )

    assert report["ok"] is False
    assert report["write"]["status"] == "blocked"
    assert any("manifest_sha256" in error for error in report["errors"])
    assert any("weights_sha256" in error for error in report["errors"])


def test_model_governance_pack_refuses_to_modify_existing_approved_pack(tmp_path):
    governance = default_model_governance_registry()
    governance["weight_packs"].append(
        {
            "id": "qwen2_5_7b_instruct_governance_pack_v0",
            "type": "base_model",
            "status": "approved",
            "base_model": "qwen2.5-7b-instruct",
            "license": "Apache-2.0",
            "domains": ["general", "coding"],
            "allowed_runtimes": ["ollama"],
            "manifest_sha256": VALID_SHA_A,
            "weights_sha256": VALID_SHA_B,
            "core_weight_editable": False,
            "promotion_gate": "already_approved",
        }
    )
    governance_path = tmp_path / "model-governance.json"
    governance_path.write_text(json.dumps(governance, indent=2), encoding="utf-8")
    model_registry_path = _write_qwen_registry(tmp_path)

    report = run_model_governance_pack(
        ModelGovernancePackConfig(
            governance_path=governance_path,
            model_registry_path=model_registry_path,
            model_id="qwen2.5-7b-instruct",
            write=True,
        )
    )

    assert report["ok"] is False
    assert report["write"]["status"] == "blocked"
    assert any("approved governance weight packs cannot be modified" in error for error in report["errors"])


def test_model_governance_pack_parser_accepts_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "governance-pack",
            "--governance",
            "D:\\ChatP2PData\\model-governance.json",
            "--registry",
            "D:\\ChatP2PData\\model-registry.json",
            "--model-id",
            "qwen2.5-7b-instruct",
            "--out",
            "D:\\ChatP2PData\\model-governance-pack.json",
            "--pack-id",
            "qwen2_5_7b_instruct_governance_pack_v0",
            "--status",
            "approved",
            "--promotion-gate",
            "manual_review_complete",
            "--write",
            "--no-backup",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_governance_pack_command"
    assert args.command == "model"
    assert args.model_command == "governance-pack"
    assert args.status == "approved"
    assert args.write is True
    assert args.no_backup is True
    assert args.json is True


def test_model_governance_review_dry_run_records_submitted_preview(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)
    before = json.loads(registry_path.read_text(encoding="utf-8"))

    report = run_model_governance_review(
        ModelGovernanceReviewConfig(
            registry_path=registry_path,
            model_id="qwen2.5-7b-instruct",
            out_path=tmp_path / "governance-review.json",
        )
    )
    after = json.loads(registry_path.read_text(encoding="utf-8"))

    assert report["schema"] == MODEL_GOVERNANCE_REVIEW_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["summary"]["review_status"] == "submitted"
    assert report["summary"]["does_not_approve_model"] is True
    assert report["summary"]["model_status_unchanged"] is True
    assert report["summary"]["recommended_next_action"] == "rerun_governance_review_with_write_after_review"
    assert report["governance_after"]["proposal_id"] == "qwen2_5_7b_instruct_governance_review_v0"
    assert report["governance_after"]["review_status"] == "submitted"
    assert before == after
    assert (tmp_path / "governance-review.json").exists()


def test_model_governance_review_write_approved_keeps_model_status_and_backs_up(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)

    report = run_model_governance_review(
        ModelGovernanceReviewConfig(
            registry_path=registry_path,
            model_id="qwen2.5-7b-instruct",
            review_status="approved",
            rollback_plan="restore previous default model route",
            approved_by=("domain_steward_fixture",),
            write=True,
        )
    )
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    model = next(model for model in registry["models"] if model["id"] == "qwen2.5-7b-instruct")

    assert report["ok"] is True
    assert report["dry_run"] is False
    assert report["write"]["status"] == "written"
    assert report["summary"]["recommended_next_action"] == "run_model_release_check"
    assert report["model"]["status_before"] == "candidate"
    assert report["model"]["status_after"] == "candidate"
    assert report["governance_after"]["rollback_plan_present"] is True
    assert report["governance_after"]["approved_by_count"] == 1
    assert model["status"] == "candidate"
    assert model["governance"]["review_status"] == "approved"
    assert model["governance"]["rollback_plan"] == "restore previous default model route"
    assert model["governance"]["approved_by"] == ["domain_steward_fixture"]
    assert (tmp_path / "model-registry.json.bak").exists()


def test_model_governance_review_blocks_approved_without_required_evidence(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)

    report = run_model_governance_review(
        ModelGovernanceReviewConfig(
            registry_path=registry_path,
            model_id="qwen2.5-7b-instruct",
            review_status="approved",
            write=True,
        )
    )

    assert report["ok"] is False
    assert report["write"]["status"] == "blocked"
    assert any("rollback plan" in error for error in report["errors"])
    assert any("approver" in error for error in report["errors"])


def test_model_governance_review_refuses_to_modify_approved_model(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    for model in registry["models"]:
        if model["id"] == "qwen2.5-7b-instruct":
            model["status"] = "approved"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    report = run_model_governance_review(
        ModelGovernanceReviewConfig(
            registry_path=registry_path,
            model_id="qwen2.5-7b-instruct",
            review_status="submitted",
            write=True,
        )
    )

    assert report["ok"] is False
    assert report["write"]["status"] == "blocked"
    assert any("approved model entries cannot be modified" in error for error in report["errors"])


def test_model_governance_review_parser_accepts_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "governance-review",
            "--registry",
            "D:\\ChatP2PData\\model-registry.json",
            "--model-id",
            "qwen2.5-7b-instruct",
            "--out",
            "D:\\ChatP2PData\\model-governance-review.json",
            "--proposal-id",
            "qwen-review-v0",
            "--review-status",
            "approved",
            "--rollback-plan",
            "restore previous default model route",
            "--approved-by",
            "domain_steward_fixture",
            "--write",
            "--no-backup",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_governance_review_command"
    assert args.command == "model"
    assert args.model_command == "governance-review"
    assert args.review_status == "approved"
    assert args.approved_by == ["domain_steward_fixture"]
    assert args.write is True
    assert args.no_backup is True
    assert args.json is True


def _write_qwen_registry(tmp_path, *, hashed=True):
    registry = default_model_registry()
    registry["models"].append(
        {
            "id": "qwen2.5-7b-instruct",
            "status": "candidate",
            "provider": "Qwen",
            "project": "Qwen2.5-7B-Instruct",
            "family": "base_chat_model",
            "variant": "Qwen2.5-7B-Instruct",
            "license": "Apache-2.0",
            "license_url": "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct",
            "source_url": "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct",
            "parameter_count_b": 7.61,
            "architecture": "transformer",
            "context_length_tokens": 131072,
            "domains": ["general", "coding", "maths"],
            "runtimes": [
                {"id": "ollama", "support_status": "candidate", "notes": "local smoke pending"},
            ],
            "hardware": {
                "min_ram_gb": 16,
                "min_vram_gb": 8,
                "recommended_capability_tier": "gaming_laptop",
            },
            "artifacts": {
                "manifest_sha256": VALID_SHA_A if hashed else "TBD",
                "weights_sha256": VALID_SHA_B if hashed else "TBD",
                "quantization": "q4_k_m" if hashed else "TBD",
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
                "completed_evaluations": [],
            },
            "governance": {
                "proposal_id": None,
                "review_status": "not_submitted",
                "rollback_plan": None,
                "approved_by": [],
            },
        }
    )
    registry_path = tmp_path / "model-registry.json"
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    return registry_path
