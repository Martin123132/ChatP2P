import json

from chatp2p.cli import build_parser
from chatp2p.model_candidate import (
    MODEL_CANDIDATE_INTAKE_REPORT_SCHEMA,
    ModelCandidateIntakeConfig,
    run_model_candidate_intake,
)
from chatp2p.model_registry import default_model_registry


VALID_SHA_A = "a" * 64
VALID_SHA_B = "b" * 64


def test_model_candidate_dry_run_previews_new_candidate_without_writing(tmp_path):
    registry_path = tmp_path / "model-registry.json"

    report = run_model_candidate_intake(
        ModelCandidateIntakeConfig(
            registry_path=registry_path,
            model_id="example-open-8b",
            provider="Example Open Model Lab",
            project="Example Open Chat",
            variant="example-8b-q4",
            license="Example-Permissive-License",
            license_url="https://example.invalid/license",
            source_url="https://example.invalid/model",
            parameter_count_b=8,
            architecture="dense_transformer",
            context_length_tokens=8192,
            domains=("general", "coding"),
            runtimes=("ollama:verified:local smoke passed",),
            min_ram_gb=16,
            min_vram_gb=8,
            recommended_capability_tier="gaming_laptop",
            manifest_sha256=VALID_SHA_A,
            weights_sha256=VALID_SHA_B,
            quantization="q4_k_m",
            out_path=tmp_path / "candidate-report.json",
        )
    )

    assert report["schema"] == MODEL_CANDIDATE_INTAKE_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["operation"] == "add"
    assert report["summary"]["does_not_approve_model"] is True
    assert report["model"]["status_after"] == "candidate"
    assert report["candidate"]["source_url_present"] is True
    assert report["candidate"]["artifacts"]["weights_sha256_present"] is True
    assert not registry_path.exists()
    assert (tmp_path / "candidate-report.json").exists()


def test_model_candidate_write_creates_registry_and_backup_only_for_existing_file(tmp_path):
    registry_path = tmp_path / "model-registry.json"

    report = run_model_candidate_intake(
        ModelCandidateIntakeConfig(
            registry_path=registry_path,
            model_id="example-open-8b",
            provider="Example Open Model Lab",
            source_url="https://example.invalid/model",
            license="Example-Permissive-License",
            license_url="https://example.invalid/license",
            runtimes=("ollama:verified:local smoke passed",),
            manifest_sha256=VALID_SHA_A,
            weights_sha256=VALID_SHA_B,
            write=True,
        )
    )
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    model_ids = [model["id"] for model in registry["models"]]

    assert report["ok"] is True
    assert report["write"]["status"] == "written"
    assert "backup_path" not in report["write"]
    assert "example-open-8b" in model_ids
    assert registry["models"][-1]["status"] == "candidate"
    assert registry["models"][-1]["eval_plan"]["completed_evaluations"] == []


def test_model_candidate_updates_existing_candidate_without_approval(tmp_path):
    registry = default_model_registry()
    registry["models"][0]["id"] = "example-open-8b"
    registry["models"][0]["status"] = "candidate"
    registry_path = tmp_path / "model-registry.json"
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")

    report = run_model_candidate_intake(
        ModelCandidateIntakeConfig(
            registry_path=registry_path,
            model_id="example-open-8b",
            provider="Updated Lab",
            project="Updated Project",
            runtimes=("ollama:verified:local smoke passed",),
            write=True,
        )
    )
    updated = json.loads(registry_path.read_text(encoding="utf-8"))
    model = updated["models"][0]

    assert report["ok"] is True
    assert report["operation"] == "update"
    assert report["model"]["approval_status_changed"] is False
    assert report["write"]["status"] == "written"
    assert (tmp_path / "model-registry.json.bak").exists()
    assert model["status"] == "candidate"
    assert model["provider"] == "Updated Lab"
    assert model["project"] == "Updated Project"
    assert model["runtimes"][0]["support_status"] == "verified"


def test_model_candidate_preserves_existing_status_when_status_not_supplied(tmp_path):
    registry = default_model_registry()
    registry["models"][0]["id"] = "example-open-8b"
    registry["models"][0]["status"] = "proposal"
    registry["models"][0]["domains"] = ["general", "science"]
    registry_path = tmp_path / "model-registry.json"
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")

    report = run_model_candidate_intake(
        ModelCandidateIntakeConfig(
            registry_path=registry_path,
            model_id="example-open-8b",
            provider="Updated Lab",
            write=True,
        )
    )
    updated = json.loads(registry_path.read_text(encoding="utf-8"))
    model = updated["models"][0]

    assert report["ok"] is True
    assert model["status"] == "proposal"
    assert model["domains"] == ["general", "science"]


def test_model_candidate_refuses_to_modify_approved_model(tmp_path):
    registry = default_model_registry()
    registry["models"][0]["id"] = "example-open-8b"
    registry["models"][0]["status"] = "approved"
    registry_path = tmp_path / "model-registry.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    report = run_model_candidate_intake(
        ModelCandidateIntakeConfig(
            registry_path=registry_path,
            model_id="example-open-8b",
            provider="Updated Lab",
            write=True,
        )
    )

    assert report["ok"] is False
    assert report["write"]["status"] == "blocked"
    assert any("approved model entries cannot be modified" in error for error in report["errors"])


def test_model_candidate_redacts_sensitive_values(tmp_path):
    registry_path = tmp_path / "model-registry.json"
    generic_secret = "secret-" + ("x" * 30)

    report = run_model_candidate_intake(
        ModelCandidateIntakeConfig(
            registry_path=registry_path,
            model_id="example-open-8b",
            source_url="https://example.invalid/model?token=alpha-token-model-candidate-123456",
            notes=f'admission_token="{generic_secret}"',
            write=True,
        )
    )
    serialized = json.dumps(report)

    assert report["ok"] is False
    assert report["write"]["status"] == "blocked"
    assert "alpha-token-model-candidate-123456" not in serialized
    assert generic_secret not in serialized
    assert not registry_path.exists()


def test_model_candidate_rejects_bad_runtime_spec(tmp_path):
    report = run_model_candidate_intake(
        ModelCandidateIntakeConfig(
            registry_path=tmp_path / "model-registry.json",
            model_id="example-open-8b",
            runtimes=("ollama:not-real",),
            write=True,
        )
    )

    assert report["ok"] is False
    assert report["write"]["status"] == "blocked"
    assert any("runtime support status" in error for error in report["errors"])


def test_model_candidate_parser_accepts_metadata_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "candidate",
            "--registry",
            "D:\\ChatP2PData\\model-registry.json",
            "--model-id",
            "example-open-8b",
            "--provider",
            "Example Open Model Lab",
            "--project",
            "Example Open Chat",
            "--domain",
            "general",
            "--domain",
            "coding",
            "--runtime",
            "ollama:verified:local smoke passed",
            "--weights-sha256",
            VALID_SHA_B,
            "--write",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_candidate_command"
    assert args.command == "model"
    assert args.model_command == "candidate"
    assert args.model_id == "example-open-8b"
    assert args.domain == ["general", "coding"]
    assert args.runtime == ["ollama:verified:local smoke passed"]
    assert args.write is True
    assert args.json is True
