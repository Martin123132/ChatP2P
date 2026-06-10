import json

from chatp2p.cli import build_parser
from chatp2p.model_shortlist import (
    MODEL_SHORTLIST_REPORT_SCHEMA,
    ModelShortlistConfig,
    run_model_shortlist,
)


def test_model_shortlist_writes_report_and_recommends_first_candidate(tmp_path):
    report = run_model_shortlist(ModelShortlistConfig(out_dir=tmp_path / "shortlist"))

    assert report["schema"] == MODEL_SHORTLIST_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["status"] == "pass"
    assert report["summary"]["does_not_approve_model"] is True
    assert report["summary"]["recommended_model_id"] == "qwen2.5-7b-instruct"
    assert report["recommended"]["id"] == "qwen2.5-7b-instruct"
    assert report["recommended"]["blockers"] == []
    assert "--write" not in report["recommended"]["candidate_command"]
    assert (tmp_path / "shortlist" / "model-shortlist.json").exists()
    assert (tmp_path / "shortlist" / "model-shortlist.md").exists()


def test_model_shortlist_records_expected_candidate_blockers(tmp_path):
    report = run_model_shortlist(ModelShortlistConfig(out_dir=tmp_path / "shortlist"))
    candidates = {entry["id"]: entry for entry in report["candidates"]}

    assert set(candidates) == {
        "gemma-4-e4b-it",
        "llama-3.2-3b-instruct",
        "mistral-nemo-instruct-2407",
        "qwen2.5-7b-instruct",
    }
    assert "custom_license_review" in candidates["llama-3.2-3b-instruct"]["blockers"]
    assert "multimodal_scope_review" in candidates["gemma-4-e4b-it"]["blockers"]
    assert candidates["qwen2.5-7b-instruct"]["license"]["spdx"] == "Apache-2.0"


def test_model_shortlist_flags_candidates_above_size_threshold(tmp_path):
    report = run_model_shortlist(ModelShortlistConfig(out_dir=tmp_path / "shortlist", max_parameter_count_b=4.0))
    candidates = {entry["id"]: entry for entry in report["candidates"]}

    assert "too_large_for_default_threshold" in candidates["qwen2.5-7b-instruct"]["blockers"]
    assert "too_large_for_default_threshold" in candidates["mistral-nemo-instruct-2407"]["blockers"]


def test_model_shortlist_report_has_no_token_like_values(tmp_path):
    report = run_model_shortlist(ModelShortlistConfig(out_dir=tmp_path / "shortlist"))
    serialized = json.dumps(report)

    assert "admission_token" not in serialized
    assert "alpha-token-" not in serialized
    assert "PRIVATE KEY" not in serialized
    assert "tskey-" not in serialized


def test_model_shortlist_parser_accepts_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "shortlist",
            "--out",
            "D:\\ChatP2PData\\model-shortlist",
            "--max-parameter-count-b",
            "8",
            "--prefer-license",
            "Apache-2.0",
            "--include-noncommercial",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_shortlist_command"
    assert args.command == "model"
    assert args.model_command == "shortlist"
    assert args.max_parameter_count_b == 8
    assert args.include_noncommercial is True
    assert args.json is True
