import json

from chatp2p.cli import build_parser
from chatp2p.model_candidate_pack import (
    MODEL_CANDIDATE_PACK_REPORT_SCHEMA,
    ModelCandidatePackConfig,
    run_model_candidate_pack,
)


def test_model_candidate_pack_builds_isolated_qwen_pack_without_live_registry_write(tmp_path):
    live_registry = tmp_path / "live-model-registry.json"

    report = run_model_candidate_pack(
        ModelCandidatePackConfig(
            out_dir=tmp_path / "pack",
            registry_path=live_registry,
            governance_path=tmp_path / "model-governance.json",
        )
    )

    assert report["schema"] == MODEL_CANDIDATE_PACK_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["status"] == "warn"
    assert report["summary"]["selected_model_id"] == "qwen2.5-7b-instruct"
    assert report["summary"]["does_not_approve_model"] is True
    assert report["summary"]["live_registry_modified"] is False
    assert report["summary"]["release_ready"] is False
    assert report["summary"]["recommended_next_action"] == "verify_local_runtime_for_candidate"
    assert {"runtime", "artifacts", "model_governance_review", "governance_weight_pack"}.issubset(
        set(report["summary"]["blocked_gate_ids"])
    )
    assert not live_registry.exists()
    assert (tmp_path / "pack" / "staging-model-registry.json").exists()
    assert (tmp_path / "pack" / "model-candidate-pack.json").exists()
    assert (tmp_path / "pack" / "model-candidate-pack.md").exists()


def test_model_candidate_pack_runs_candidate_eval_attach_and_release_steps(tmp_path):
    report = run_model_candidate_pack(ModelCandidatePackConfig(out_dir=tmp_path / "pack"))
    reports = report["reports"]

    assert reports["shortlist"]["status"] == "pass"
    assert reports["candidate_preview"]["summary"]["recommended_next_action"] == "rerun_candidate_intake_with_write"
    assert reports["candidate_staging_write"]["summary"]["recommended_next_action"] == "run_model_eval_and_verify_hashes"
    assert reports["eval"]["status"] == "pass"
    assert reports["eval"]["summary"]["all_required_evaluations_satisfied"] is True
    assert reports["eval_attach"]["summary"]["completed_evaluations_added"] == [
        "domain_eval",
        "regression_eval",
        "safety_eval",
        "license_review",
        "local_smoke",
    ]
    assert reports["release_check"]["summary"]["release_ready"] is False


def test_model_candidate_pack_can_select_specific_shortlist_candidate(tmp_path):
    report = run_model_candidate_pack(
        ModelCandidatePackConfig(
            out_dir=tmp_path / "pack",
            model_id="mistral-nemo-instruct-2407",
        )
    )

    assert report["ok"] is True
    assert report["summary"]["selected_model_id"] == "mistral-nemo-instruct-2407"
    assert report["selected_candidate"]["provider"] == "Mistral AI / NVIDIA"


def test_model_candidate_pack_fails_for_missing_shortlist_candidate(tmp_path):
    report = run_model_candidate_pack(
        ModelCandidatePackConfig(
            out_dir=tmp_path / "pack",
            model_id="not-a-real-shortlist-model",
        )
    )

    assert report["ok"] is False
    assert report["status"] == "fail"
    assert report["summary"]["selected_model_id"] is None
    assert any("model_id not found in shortlist" in error for error in report["errors"])


def test_model_candidate_pack_report_has_no_token_like_values(tmp_path):
    report = run_model_candidate_pack(ModelCandidatePackConfig(out_dir=tmp_path / "pack"))
    serialized = json.dumps(report)

    assert "admission_token" not in serialized
    assert "alpha-token-" not in serialized
    assert "PRIVATE KEY" not in serialized
    assert "tskey-" not in serialized


def test_model_candidate_pack_parser_accepts_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "candidate-pack",
            "--out",
            "D:\\ChatP2PData\\model-candidate-pack",
            "--registry",
            "D:\\ChatP2PData\\model-registry.json",
            "--governance",
            "D:\\ChatP2PData\\model-governance.json",
            "--model-id",
            "qwen2.5-7b-instruct",
            "--max-parameter-count-b",
            "8",
            "--prefer-license",
            "Apache-2.0",
            "--include-noncommercial",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_candidate_pack_command"
    assert args.command == "model"
    assert args.model_command == "candidate-pack"
    assert args.model_id == "qwen2.5-7b-instruct"
    assert args.include_noncommercial is True
    assert args.json is True
