import json

from chatp2p.cli import build_parser
from chatp2p.model_governance import default_model_governance_registry
from chatp2p.model_registry import default_model_registry
from chatp2p.model_route_plan import (
    MODEL_ROUTE_PLAN_REPORT_SCHEMA,
    ModelRoutePlanConfig,
    run_model_route_plan,
)


VALID_SHA_A = "a" * 64
VALID_SHA_B = "b" * 64


def test_model_route_plan_missing_registry_uses_default_and_warns(tmp_path):
    report = run_model_route_plan(
        ModelRoutePlanConfig(
            registry_path=tmp_path / "missing-registry.json",
            governance_path=tmp_path / "missing-governance.json",
            out_dir=tmp_path / "route-plan",
            skip_network_checks=True,
        )
    )

    assert report["schema"] == MODEL_ROUTE_PLAN_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["status"] == "warn"
    assert report["summary"]["route_ready"] is False
    assert report["summary"]["recommended_next_action"] == "rerun_route_plan_with_network_checks"
    assert "model_registry_missing_using_builtin_default" in report["warnings"]
    assert (tmp_path / "route-plan" / "model-route-plan.json").exists()
    assert (tmp_path / "route-plan" / "model-route-plan.md").exists()


def test_model_route_plan_approved_model_is_route_ready_with_live_worker(monkeypatch, tmp_path):
    registry_path, governance_path = _write_registry_and_governance(tmp_path, _ready_model(status="approved"))

    monkeypatch.setattr("chatp2p.model_route_plan.CoordinatorClient", _fake_client(_snapshot_with_model()))

    report = run_model_route_plan(
        ModelRoutePlanConfig(
            registry_path=registry_path,
            governance_path=governance_path,
            out_dir=tmp_path / "route-plan",
            coordinator_url="http://127.0.0.1:8765",
        )
    )

    assert report["status"] == "pass"
    assert report["summary"]["route_ready"] is True
    assert report["summary"]["selected_model_id"] == "qwen2.5-7b-instruct"
    assert report["summary"]["recommended_chat_model"] == "qwen2.5-7b-instruct"
    assert report["summary"]["live_model_capable_worker_count"] == 1
    assert report["summary"]["recommended_next_action"] == "continue_chat_session_with_route_plan"
    assert report["models"][0]["routing"]["eligible_node_samples"][0]["node_id_redacted"].startswith("worker_fix")


def test_model_route_plan_network_skipped_requires_live_check(tmp_path):
    registry_path, governance_path = _write_registry_and_governance(tmp_path, _ready_model(status="approved"))

    report = run_model_route_plan(
        ModelRoutePlanConfig(
            registry_path=registry_path,
            governance_path=governance_path,
            out_dir=tmp_path / "route-plan",
            skip_network_checks=True,
        )
    )

    assert report["status"] == "warn"
    assert report["summary"]["selected_model_id"] == "qwen2.5-7b-instruct"
    assert report["summary"]["route_ready"] is False
    assert report["summary"]["recommended_next_action"] == "rerun_route_plan_with_network_checks"
    assert "network_not_checked" in report["models"][0]["blockers"]


def test_model_route_plan_prefers_requested_routeable_model(monkeypatch, tmp_path):
    registry = default_model_registry()
    registry["models"] = [
        _ready_model(model_id="alpha-model", status="approved"),
        _ready_model(model_id="beta-model", status="approved"),
    ]
    governance = _ready_governance_registry(model_ids=["alpha-model", "beta-model"])
    registry_path = tmp_path / "model-registry.json"
    governance_path = tmp_path / "model-governance.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    governance_path.write_text(json.dumps(governance, indent=2), encoding="utf-8")

    monkeypatch.setattr("chatp2p.model_route_plan.CoordinatorClient", _fake_client(_snapshot_with_model("beta-model")))

    report = run_model_route_plan(
        ModelRoutePlanConfig(
            registry_path=registry_path,
            governance_path=governance_path,
            out_dir=tmp_path / "route-plan",
            preferred_model="beta-model",
            coordinator_url="http://127.0.0.1:8765",
        )
    )

    assert report["status"] == "pass"
    assert report["summary"]["selected_model_id"] == "beta-model"
    assert report["summary"]["route_ready"] is True


def test_model_route_plan_redacts_admission_token(monkeypatch, tmp_path):
    registry_path, governance_path = _write_registry_and_governance(tmp_path, _ready_model(status="approved"))
    secret = "alpha" + "-token-route-plan-secret-123456"
    monkeypatch.setattr("chatp2p.model_route_plan.CoordinatorClient", _fake_client(_snapshot_with_model()))

    report = run_model_route_plan(
        ModelRoutePlanConfig(
            registry_path=registry_path,
            governance_path=governance_path,
            out_dir=tmp_path / "route-plan",
            coordinator_url="http://127.0.0.1:8765",
            admission_token=secret,
        )
    )

    serialized = json.dumps(report)
    assert secret not in serialized
    assert "route-plan-secret" not in serialized
    assert report["config"]["admission_token_present"] is True


def test_model_route_plan_parser_accepts_flags(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "model",
            "route-plan",
            "--registry",
            str(tmp_path / "model-registry.json"),
            "--governance",
            str(tmp_path / "model-governance.json"),
            "--out",
            str(tmp_path / "model-route-plan"),
            "--preferred-model",
            "qwen2.5-7b-instruct",
            "--coordinator",
            "http://127.0.0.1:8765",
            "--admission-token",
            "test-token-placeholder",
            "--skip-network-checks",
            "--timeout-seconds",
            "2",
            "--job-type",
            "inference.chat.v1",
            "--runtime",
            "ollama",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_route_plan_command"
    assert args.command == "model"
    assert args.model_command == "route-plan"
    assert args.preferred_model == "qwen2.5-7b-instruct"
    assert args.skip_network_checks is True
    assert args.json is True


def _write_registry_and_governance(tmp_path, model):
    registry = default_model_registry()
    registry["models"][0] = model
    governance = _ready_governance_registry(model_ids=[model["id"]])
    registry_path = tmp_path / "model-registry.json"
    governance_path = tmp_path / "model-governance.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    governance_path.write_text(json.dumps(governance, indent=2), encoding="utf-8")
    return registry_path, governance_path


def _ready_model(*, model_id="qwen2.5-7b-instruct", status="proposal"):
    return {
        "id": model_id,
        "status": status,
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
            "proposal_id": f"{model_id}-governance-review-v0",
            "review_status": "approved",
            "rollback_plan": "restore previous approved model route",
            "approved_by": ["domain_steward_fixture"],
        },
    }


def _ready_governance_registry(*, model_ids):
    registry = default_model_governance_registry()
    registry["weight_packs"] = [
        {
            "id": f"{_safe_pack_id(model_id)}_governance_pack_v0",
            "type": "base_model",
            "status": "approved",
            "base_model": model_id,
            "license": "Apache-2.0",
            "domains": ["general", "coding"],
            "allowed_runtimes": ["ollama"],
            "manifest_sha256": VALID_SHA_A,
            "weights_sha256": VALID_SHA_B,
            "core_weight_editable": False,
            "promotion_gate": "passed_eval_and_governance_review",
        }
        for model_id in model_ids
    ]
    return registry


def _safe_pack_id(model_id):
    return "".join(char if char.isalnum() else "_" for char in model_id.lower()).strip("_")


def _snapshot_with_model(model_id="qwen2.5-7b-instruct"):
    return {
        "status": {
            "coordinator_id": "coordinator_fixture",
            "known_nodes": 1,
            "live_nodes": 1,
            "jobs": 0,
            "pending_jobs": 0,
            "queued_jobs": 0,
        },
        "nodes": [
            {
                "node_id": "worker_fixture_routeplan_001",
                "liveness_status": "live",
                "supported_job_types": ["inference.chat.v1", "eval.deterministic.v1"],
                "ollama_models": [model_id],
                "software": {"source_revision": "a" * 40, "source_dirty": False},
            }
        ],
    }


def _fake_client(snapshot):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def snapshot(self):
            return snapshot

    return FakeClient
