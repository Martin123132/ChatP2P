import json

from chatp2p.cli import build_parser
from chatp2p.model_eval import (
    MODEL_EVAL_ATTACH_REPORT_SCHEMA,
    MODEL_EVAL_REPORT_SCHEMA,
    ModelEvalAttachConfig,
    ModelEvalConfig,
    run_model_eval,
    run_model_eval_attach,
)
from chatp2p.model_registry import default_model_registry


VALID_SHA_A = "a" * 64
VALID_SHA_B = "b" * 64


def test_model_eval_fake_mode_writes_warn_report_and_does_not_approve(tmp_path):
    registry = default_model_registry()
    registry_path = tmp_path / "model-registry.json"
    original_registry_text = json.dumps(registry, indent=2, sort_keys=True)
    registry_path.write_text(original_registry_text, encoding="utf-8")
    out_dir = tmp_path / "model-eval"

    report = run_model_eval(
        ModelEvalConfig(
            registry_path=registry_path,
            model_id="chatp2p-base-candidate-v0",
            out_dir=out_dir,
            mode="fake",
        )
    )

    assert report["schema"] == MODEL_EVAL_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["status"] == "warn"
    assert report["summary"]["passed_checks"] == 5
    assert report["summary"]["blocked_checks"] == 1
    assert report["summary"]["does_not_approve_model"] is True
    assert report["summary"]["recommended_next_action"] == "confirm_model_license"
    assert report["summary"]["required_evaluations_satisfied"]["license_review"] is False
    assert report["evidence_for_registry"]["local_chat_smoke_passes"] is True
    assert report["evidence_for_registry"]["registry_update_required"] is True
    assert (out_dir / "model-eval-report.json").exists()
    assert (out_dir / "model-eval-report.md").exists()
    assert registry_path.read_text(encoding="utf-8") == original_registry_text


def test_model_eval_complete_candidate_passes_without_mutating_registry(tmp_path):
    registry = default_model_registry()
    registry["models"][0] = _complete_candidate(status="proposal")
    registry_path = tmp_path / "model-registry.json"
    original_registry_text = json.dumps(registry, indent=2, sort_keys=True)
    registry_path.write_text(original_registry_text, encoding="utf-8")

    report = run_model_eval(
        ModelEvalConfig(
            registry_path=registry_path,
            model_id="chatp2p-base-ready-v0",
            out_dir=tmp_path / "model-eval",
            mode="fake",
        )
    )

    assert report["ok"] is True
    assert report["status"] == "pass"
    assert report["summary"]["all_required_evaluations_satisfied"] is True
    assert report["summary"]["recommended_next_action"] == "attach_eval_evidence_to_model_registry"
    assert report["evidence_for_registry"]["no_known_license_blocker"] is True
    assert registry_path.read_text(encoding="utf-8") == original_registry_text


def test_model_eval_missing_model_fails(tmp_path):
    registry_path = tmp_path / "model-registry.json"
    registry_path.write_text(json.dumps(default_model_registry()), encoding="utf-8")

    report = run_model_eval(
        ModelEvalConfig(
            registry_path=registry_path,
            model_id="missing-model",
            out_dir=tmp_path / "model-eval",
            mode="fake",
        )
    )

    assert report["ok"] is False
    assert report["status"] == "fail"
    assert report["summary"]["recommended_next_action"] == "fix_model_registry"
    assert any("model_id not found" in error for error in report["errors"])


def test_model_eval_redacts_sensitive_registry_values(tmp_path):
    registry = default_model_registry()
    generic_secret = "secret-" + ("x" * 30)
    registry["notes"] = f'admission_token="{generic_secret}"'
    registry["models"][0]["source_url"] = (
        "https://example.invalid/model?token=alpha-token-model-eval-secret-123456"
    )
    registry_path = tmp_path / "model-registry.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    report = run_model_eval(
        ModelEvalConfig(
            registry_path=registry_path,
            model_id="chatp2p-base-candidate-v0",
            out_dir=tmp_path / "model-eval",
            mode="fake",
        )
    )

    serialized = json.dumps(report)
    assert report["ok"] is False
    assert generic_secret not in serialized
    assert "alpha-token-model-eval-secret-123456" not in serialized
    assert any("sensitive value detected" in error for error in report["errors"])


def test_model_eval_parser_accepts_required_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "eval",
            "--registry",
            "D:\\ChatP2PData\\model-registry.json",
            "--model-id",
            "chatp2p-base-candidate-v0",
            "--out",
            "D:\\ChatP2PData\\model-eval",
            "--mode",
            "fake",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_eval_command"
    assert args.command == "model"
    assert args.model_command == "eval"
    assert args.model_id == "chatp2p-base-candidate-v0"
    assert args.mode == "fake"
    assert args.json is True


def test_model_eval_attach_dry_run_previews_registry_update_without_writing(tmp_path):
    registry = default_model_registry()
    registry_path = tmp_path / "model-registry.json"
    original_registry_text = json.dumps(registry, indent=2, sort_keys=True)
    registry_path.write_text(original_registry_text, encoding="utf-8")
    eval_report = run_model_eval(
        ModelEvalConfig(
            registry_path=registry_path,
            model_id="chatp2p-base-candidate-v0",
            out_dir=tmp_path / "model-eval",
            mode="fake",
        )
    )

    report = run_model_eval_attach(
        ModelEvalAttachConfig(
            registry_path=registry_path,
            eval_report_path=tmp_path / "model-eval" / "model-eval-report.json",
            out_path=tmp_path / "attach-report.json",
        )
    )

    assert eval_report["ok"] is True
    assert report["schema"] == MODEL_EVAL_ATTACH_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["summary"]["does_not_approve_model"] is True
    assert report["model"]["approval_status_changed"] is False
    assert report["summary"]["completed_evaluations_added"] == [
        "domain_eval",
        "regression_eval",
        "safety_eval",
        "local_smoke",
    ]
    assert report["write"]["status"] == "dry_run"
    assert registry_path.read_text(encoding="utf-8") == original_registry_text


def test_model_eval_attach_write_persists_eval_evidence_but_not_approval(tmp_path):
    registry = default_model_registry()
    registry_path = tmp_path / "model-registry.json"
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    run_model_eval(
        ModelEvalConfig(
            registry_path=registry_path,
            model_id="chatp2p-base-candidate-v0",
            out_dir=tmp_path / "model-eval",
            mode="fake",
        )
    )

    report = run_model_eval_attach(
        ModelEvalAttachConfig(
            registry_path=registry_path,
            eval_report_path=tmp_path / "model-eval" / "model-eval-report.json",
            write=True,
        )
    )
    updated = json.loads(registry_path.read_text(encoding="utf-8"))
    model = updated["models"][0]
    eval_plan = model["eval_plan"]

    assert report["ok"] is True
    assert report["dry_run"] is False
    assert report["write"]["status"] == "written"
    assert (tmp_path / "model-registry.json.bak").exists()
    assert model["status"] == "candidate"
    assert eval_plan["completed_evaluations"] == [
        "domain_eval",
        "regression_eval",
        "safety_eval",
        "local_smoke",
    ]
    assert eval_plan["success_criteria"]["minimum_domain_pass_rate"] == 1.0
    assert eval_plan["success_criteria"]["local_chat_smoke_passes"] is True
    assert eval_plan["success_criteria"]["no_known_license_blocker"] is False
    assert eval_plan["evidence_reports"][0]["report_json_name"] == "model-eval-report.json"


def test_model_eval_attach_complete_report_adds_license_review_without_approval(tmp_path):
    registry = default_model_registry()
    registry["models"][0] = _complete_candidate(status="proposal")
    registry["models"][0]["eval_plan"]["completed_evaluations"] = []
    registry_path = tmp_path / "model-registry.json"
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    run_model_eval(
        ModelEvalConfig(
            registry_path=registry_path,
            model_id="chatp2p-base-ready-v0",
            out_dir=tmp_path / "model-eval",
            mode="fake",
        )
    )

    report = run_model_eval_attach(
        ModelEvalAttachConfig(
            registry_path=registry_path,
            eval_report_path=tmp_path / "model-eval" / "model-eval-report.json",
            write=True,
            backup=False,
        )
    )
    updated = json.loads(registry_path.read_text(encoding="utf-8"))
    model = updated["models"][0]

    assert report["ok"] is True
    assert report["status"] == "warn"
    assert model["status"] == "proposal"
    assert model["eval_plan"]["completed_evaluations"] == [
        "domain_eval",
        "regression_eval",
        "safety_eval",
        "license_review",
        "local_smoke",
    ]
    assert model["eval_plan"]["success_criteria"]["no_known_license_blocker"] is True


def test_model_eval_attach_rejects_failed_eval_report(tmp_path):
    registry_path = tmp_path / "model-registry.json"
    registry_path.write_text(json.dumps(default_model_registry(), indent=2), encoding="utf-8")
    eval_report_path = tmp_path / "model-eval-report.json"
    eval_report_path.write_text(
        json.dumps(
            {
                "schema": MODEL_EVAL_REPORT_SCHEMA,
                "status": "fail",
                "errors": ["broken"],
                "config": {"model_id": "chatp2p-base-candidate-v0"},
                "summary": {},
            }
        ),
        encoding="utf-8",
    )

    report = run_model_eval_attach(
        ModelEvalAttachConfig(
            registry_path=registry_path,
            eval_report_path=eval_report_path,
            write=True,
        )
    )

    assert report["ok"] is False
    assert report["write"]["status"] == "blocked"
    assert any("eval report has failures" in error for error in report["errors"])


def test_model_attach_eval_parser_accepts_dry_run_and_write_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "attach-eval",
            "--registry",
            "D:\\ChatP2PData\\model-registry.json",
            "--eval-report",
            "D:\\ChatP2PData\\model-eval\\model-eval-report.json",
            "--out",
            "D:\\ChatP2PData\\model-eval-attach-report.json",
            "--write",
            "--no-backup",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_eval_attach_command"
    assert args.command == "model"
    assert args.model_command == "attach-eval"
    assert args.write is True
    assert args.no_backup is True
    assert args.json is True


def _complete_candidate(*, status):
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
