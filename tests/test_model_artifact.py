import hashlib
import json
from pathlib import Path

from chatp2p.cli import build_parser
from chatp2p.model_artifact import (
    MODEL_ARTIFACT_ATTACH_REPORT_SCHEMA,
    MODEL_ARTIFACT_MANIFEST_REPORT_SCHEMA,
    ModelArtifactAttachConfig,
    ModelArtifactManifestConfig,
    run_model_artifact_attach,
    run_model_artifact_manifest,
)
from chatp2p.model_registry import default_model_registry


VALID_SHA_A = "a" * 64
VALID_SHA_B = "b" * 64


def test_model_artifact_manifest_hashes_local_files_without_registry_write(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)
    manifest = tmp_path / "manifest.json"
    weights = tmp_path / "model.gguf"
    manifest.write_text('{"model":"qwen2.5-7b-instruct"}', encoding="utf-8")
    weights.write_bytes(b"fake local weights for hash evidence")

    report = run_model_artifact_manifest(
        ModelArtifactManifestConfig(
            registry_path=registry_path,
            model_id="qwen2.5-7b-instruct",
            out_dir=tmp_path / "artifacts",
            manifest_artifact=manifest,
            weights_artifact=weights,
            quantization="q4_k_m",
        )
    )
    stored_registry = json.loads(registry_path.read_text(encoding="utf-8"))

    assert report["schema"] == MODEL_ARTIFACT_MANIFEST_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["status"] == "pass"
    assert report["summary"]["artifact_hashes_complete"] is True
    assert report["summary"]["does_not_approve_model"] is True
    assert report["summary"]["registry_write"] is False
    assert report["summary"]["manifest_sha256"] == _sha256_text(manifest.read_bytes())
    assert report["summary"]["weights_sha256"] == _sha256_text(weights.read_bytes())
    assert report["summary"]["recommended_next_action"] == "review_then_update_candidate_artifacts"
    assert "--write" not in report["summary"]["candidate_update_preview"]
    assert stored_registry["models"][-1]["artifacts"]["weights_sha256"] == "TBD"
    assert (tmp_path / "artifacts" / "model-artifact-manifest.json").exists()
    assert (tmp_path / "artifacts" / "model-artifact-manifest.md").exists()


def test_model_artifact_manifest_accepts_supplied_hashes_without_files(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)

    report = run_model_artifact_manifest(
        ModelArtifactManifestConfig(
            registry_path=registry_path,
            model_id="qwen2.5-7b-instruct",
            out_dir=tmp_path / "artifacts",
            manifest_sha256=VALID_SHA_A,
            weights_sha256=VALID_SHA_B,
            quantization="q4_k_m",
            source_url="https://example.invalid/qwen-artifacts",
        )
    )

    assert report["ok"] is True
    assert report["status"] == "pass"
    assert report["artifacts_hashed"] == []
    assert report["summary"]["manifest_sha256"] == VALID_SHA_A
    assert report["summary"]["weights_sha256"] == VALID_SHA_B
    assert report["summary"]["candidate_update_preview"] is not None


def test_model_artifact_manifest_fails_on_hash_mismatch(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)
    weights = tmp_path / "model.gguf"
    weights.write_bytes(b"actual bytes")

    report = run_model_artifact_manifest(
        ModelArtifactManifestConfig(
            registry_path=registry_path,
            model_id="qwen2.5-7b-instruct",
            out_dir=tmp_path / "artifacts",
            weights_artifact=weights,
            weights_sha256=VALID_SHA_B,
            manifest_sha256=VALID_SHA_A,
            quantization="q4_k_m",
        )
    )

    assert report["ok"] is False
    assert report["status"] == "fail"
    assert report["summary"]["recommended_next_action"] == "fix_artifact_manifest_errors"
    assert any("does not match" in error for error in report["errors"])


def test_model_artifact_manifest_warns_when_hashes_are_incomplete(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)

    report = run_model_artifact_manifest(
        ModelArtifactManifestConfig(
            registry_path=registry_path,
            model_id="qwen2.5-7b-instruct",
            out_dir=tmp_path / "artifacts",
            manifest_sha256=VALID_SHA_A,
        )
    )

    assert report["ok"] is True
    assert report["status"] == "warn"
    assert report["summary"]["artifact_hashes_complete"] is False
    assert report["summary"]["recommended_next_action"] == "provide_weights_artifact_or_sha256"


def test_model_artifact_manifest_fails_for_missing_model_id(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)

    report = run_model_artifact_manifest(
        ModelArtifactManifestConfig(
            registry_path=registry_path,
            model_id="missing-model",
            out_dir=tmp_path / "artifacts",
            manifest_sha256=VALID_SHA_A,
            weights_sha256=VALID_SHA_B,
            quantization="q4_k_m",
        )
    )

    assert report["ok"] is False
    assert report["status"] == "fail"
    assert any("model_id not found in registry" in error for error in report["errors"])


def test_model_artifact_manifest_report_has_no_token_like_values(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)

    report = run_model_artifact_manifest(
        ModelArtifactManifestConfig(
            registry_path=registry_path,
            model_id="qwen2.5-7b-instruct",
            out_dir=tmp_path / "artifacts",
            manifest_sha256=VALID_SHA_A,
            weights_sha256=VALID_SHA_B,
            quantization="q4_k_m",
            source_url="https://example.invalid/model?token=alpha-token-artifact-secret-123456",
        )
    )
    serialized = json.dumps(report)

    assert "alpha-token-artifact-secret-123456" not in serialized
    assert "admission_token" not in serialized
    assert "PRIVATE KEY" not in serialized
    assert "tskey-" not in serialized


def test_model_artifact_manifest_parser_accepts_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "artifact-manifest",
            "--registry",
            "D:\\ChatP2PData\\model-candidate-pack\\staging-model-registry.json",
            "--model-id",
            "qwen2.5-7b-instruct",
            "--out",
            "D:\\ChatP2PData\\model-artifact-manifest",
            "--manifest-artifact",
            "D:\\ChatP2PData\\qwen\\manifest.json",
            "--weights-artifact",
            "D:\\ChatP2PData\\qwen\\model.gguf",
            "--artifact",
            "D:\\ChatP2PData\\qwen\\README.md",
            "--manifest-sha256",
            VALID_SHA_A,
            "--weights-sha256",
            VALID_SHA_B,
            "--quantization",
            "q4_k_m",
            "--source-url",
            "https://example.invalid/qwen",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_artifact_manifest_command"
    assert args.command == "model"
    assert args.model_command == "artifact-manifest"
    assert args.model_id == "qwen2.5-7b-instruct"
    assert args.json is True


def test_model_artifact_attach_dry_run_preserves_registry(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)
    artifact_report_path = _write_complete_artifact_report(tmp_path, registry_path)
    before = json.loads(registry_path.read_text(encoding="utf-8"))

    report = run_model_artifact_attach(
        ModelArtifactAttachConfig(
            registry_path=registry_path,
            artifact_report_path=artifact_report_path,
            out_path=tmp_path / "artifact-attach.json",
        )
    )
    after = json.loads(registry_path.read_text(encoding="utf-8"))

    assert report["schema"] == MODEL_ARTIFACT_ATTACH_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["summary"]["does_not_approve_model"] is True
    assert report["summary"]["change_count"] == 3
    assert report["summary"]["recommended_next_action"] == "rerun_attach_artifacts_with_write_after_review"
    assert report["model"]["status_before"] == "candidate"
    assert report["model"]["status_after"] == "candidate"
    assert before == after
    assert (tmp_path / "artifact-attach.json").exists()


def test_model_artifact_attach_write_updates_artifacts_and_backup(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)
    artifact_report_path = _write_complete_artifact_report(tmp_path, registry_path)

    report = run_model_artifact_attach(
        ModelArtifactAttachConfig(
            registry_path=registry_path,
            artifact_report_path=artifact_report_path,
            out_path=tmp_path / "artifact-attach.json",
            write=True,
        )
    )
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    qwen = registry["models"][-1]

    assert report["ok"] is True
    assert report["dry_run"] is False
    assert report["write"]["status"] == "written"
    assert report["summary"]["recommended_next_action"] == "run_model_release_check"
    assert qwen["status"] == "candidate"
    assert qwen["artifacts"]["manifest_sha256"] == VALID_SHA_A
    assert qwen["artifacts"]["weights_sha256"] == VALID_SHA_B
    assert qwen["artifacts"]["quantization"] == "q4_k_m"
    assert (tmp_path / "model-registry.json.bak").exists()


def test_model_artifact_attach_blocks_incomplete_artifact_report(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)
    artifact_report = run_model_artifact_manifest(
        ModelArtifactManifestConfig(
            registry_path=registry_path,
            model_id="qwen2.5-7b-instruct",
            out_dir=tmp_path / "artifacts",
            manifest_sha256=VALID_SHA_A,
            quantization="q4_k_m",
        )
    )

    report = run_model_artifact_attach(
        ModelArtifactAttachConfig(
            registry_path=registry_path,
            artifact_report_path=Path(artifact_report["artifacts"]["json"]),
        )
    )

    assert report["ok"] is False
    assert report["write"]["status"] == "dry_run"
    assert any("missing weights_sha256" in error for error in report["errors"])


def test_model_artifact_attach_refuses_approved_model(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["models"][-1]["status"] = "approved"
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    artifact_report_path = _write_complete_artifact_report(tmp_path, registry_path)

    report = run_model_artifact_attach(
        ModelArtifactAttachConfig(
            registry_path=registry_path,
            artifact_report_path=artifact_report_path,
            write=True,
        )
    )

    assert report["ok"] is False
    assert report["write"]["status"] == "blocked"
    assert any("approved model entries cannot be modified" in error for error in report["errors"])


def test_model_artifact_attach_parser_accepts_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "attach-artifacts",
            "--registry",
            "D:\\ChatP2PData\\model-registry.json",
            "--artifact-report",
            "D:\\ChatP2PData\\model-artifact-manifest\\model-artifact-manifest.json",
            "--out",
            "D:\\ChatP2PData\\model-artifact-attach.json",
            "--write",
            "--no-backup",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_artifact_attach_command"
    assert args.command == "model"
    assert args.model_command == "attach-artifacts"
    assert args.write is True
    assert args.no_backup is True
    assert args.json is True


def _write_qwen_registry(tmp_path):
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
                "manifest_sha256": "TBD",
                "weights_sha256": "TBD",
                "quantization": "TBD",
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


def _sha256_text(data):
    return hashlib.sha256(data).hexdigest()


def _write_complete_artifact_report(tmp_path, registry_path):
    report = run_model_artifact_manifest(
        ModelArtifactManifestConfig(
            registry_path=registry_path,
            model_id="qwen2.5-7b-instruct",
            out_dir=tmp_path / "artifacts",
            manifest_sha256=VALID_SHA_A,
            weights_sha256=VALID_SHA_B,
            quantization="q4_k_m",
        )
    )
    return Path(report["artifacts"]["json"])
