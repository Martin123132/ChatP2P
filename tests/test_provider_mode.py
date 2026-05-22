import json
import zipfile

from chatp2p.cli import build_parser
from chatp2p.alpha import AlphaInvite, AlphaJoinConfig, run_alpha_join, write_alpha_invite
from chatp2p.benchmark import CAPABILITY_PROFILE_NAME, capabilities_from_benchmark, save_node_benchmark
from chatp2p.client import CoordinatorClient
from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.http_api import create_coordinator_http_server
from chatp2p.node_runtime import stop_managed_process
from chatp2p.operator_config import OperatorConfig
from chatp2p.packets import NodeRegistration
from chatp2p.provider import (
    PROVIDER_CONFIG_SCHEMA,
    PROVIDER_EDGE_PROOF_REPORT_SCHEMA,
    PROVIDER_OPS_PACK_SCHEMA,
    PROVIDER_REMOTE_PROOF_REPORT_SCHEMA,
    PROVIDER_STATUS_REPORT_SCHEMA,
    ProviderConfig,
    ProviderEdgeProofConfig,
    ProviderOpsPackConfig,
    ProviderRemoteProofConfig,
    ProviderStatusConfig,
    add_provider_subscriber,
    apply_provider_metadata,
    bootstrap_provider_config,
    join_provider_node,
    load_provider_config,
    provider_capability_profile,
    provider_snapshot_summary,
    run_provider_edge_proof,
    run_provider_ops_pack,
    run_provider_remote_proof,
    run_provider_status,
    select_provider_route,
    write_provider_config,
)
from chatp2p.worker import WorkerNode
import threading


def _benchmark_report():
    report = {
        "schema": "chatp2p.node-benchmark.v1",
        "hardware": {
            "machine": "test",
            "processor": "test",
            "python_version": "3.13",
            "system": "test",
            "system_version": "test",
            "cpu_count": 4,
            "ram_total_mb": 8_000,
            "disk_free_mb": 100_000,
        },
        "gpu": {"available": False, "provider": None, "devices": [], "total_vram_mb": None},
        "benchmark": {"cpu_iterations_per_second": 10_000},
        "model_runtimes": {
            "ollama": {
                "available": False,
                "path": None,
                "base_url": "http://127.0.0.1:11434",
                "models": [],
            },
            "llama_cpp": {"available": False, "path": None, "command": None},
            "vllm": {"available": False},
        },
    }
    report["capability_tier"] = "standard"
    report["capabilities"] = capabilities_from_benchmark(report)
    return report


def _start_public_alpha(token="alpha-token-123"):
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    operator_config = OperatorConfig(public_alpha=True, admission_token=token)
    server = create_coordinator_http_server(
        coordinator,
        host="127.0.0.1",
        port=0,
        operator_config=operator_config,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _save_provider_benchmark(home, provider, role):
    report = _benchmark_report()
    report["capabilities"] = apply_provider_metadata(
        capabilities=report["capabilities"],
        config=provider,
        node_role=role,
    )
    save_node_benchmark(report, home / CAPABILITY_PROFILE_NAME)


def test_provider_config_creation_and_subscriber_round_trip(tmp_path):
    config_path = tmp_path / "provider-config.json"

    bootstrap = bootstrap_provider_config(
        config_path=config_path,
        provider_name="Demo Fibre AI",
        region="Hull",
        provider_id="provider_demo",
    )

    assert bootstrap["ok"] is True
    loaded = load_provider_config(config_path)
    assert loaded.schema == PROVIDER_CONFIG_SCHEMA
    assert loaded.provider_id == "provider_demo"
    assert loaded.provider_name == "Demo Fibre AI"
    assert loaded.default_routing_policy == [
        "prefer_local",
        "prefer_provider_edge",
        "prefer_trusted_peer",
        "fallback_placeholder",
    ]

    created = add_provider_subscriber(
        config_path=config_path,
        subscriber_id="sub_demo_001",
        plan="Broadband AI Plus",
    )

    assert created["status"] == "subscriber_created"
    reloaded = load_provider_config(config_path)
    assert reloaded.subscribers["sub_demo_001"]["plan"] == "Broadband AI Plus"


def test_provider_config_loader_accepts_bom_and_reports_missing_file(tmp_path):
    config_path = tmp_path / "provider-config.json"
    provider = ProviderConfig.create(
        provider_name="Demo Fibre AI",
        region="Hull",
        provider_id="provider_demo",
    )
    config_path.write_text(json.dumps(provider.to_dict()), encoding="utf-8-sig")

    loaded = load_provider_config(config_path)

    assert loaded.provider_id == "provider_demo"
    try:
        load_provider_config(tmp_path / "missing-provider-config.json")
    except FileNotFoundError as exc:
        assert "provider config file not found" in str(exc)
    else:
        raise AssertionError("Expected missing provider config to fail")


def test_join_provider_writes_role_capability_profile(tmp_path):
    config_path = tmp_path / "provider-config.json"
    config = ProviderConfig.create(
        provider_name="Demo Fibre AI",
        region="Hull",
        provider_id="provider_demo",
    ).with_subscriber("sub_demo_001", "Broadband AI Plus")
    write_provider_config(config_path, config)

    report = join_provider_node(
        provider_config_path=config_path,
        subscriber_id="sub_demo_001",
        home=tmp_path / ".mesh",
        node_role="subscriber_gateway",
    )

    assert report["ok"] is True
    assert report["node"]["node_role"] == "subscriber_gateway"
    profile = json.loads((tmp_path / ".mesh" / "node-capabilities.json").read_text(encoding="utf-8"))
    assert profile["provider_id"] == "provider_demo"
    assert profile["subscriber_id"] == "sub_demo_001"
    assert profile["node_role"] == "subscriber_gateway"
    assert "eval.deterministic.v1" in profile["supported_job_types"]


def test_provider_route_selection_uses_policy_and_route_budgets():
    config = ProviderConfig.create(provider_name="Demo Fibre AI", region="Hull", provider_id="provider_demo")
    local = WorkerNode(
        identity=NodeIdentity.generate(prefix="subscriber_gateway"),
        capability_profile=provider_capability_profile(
            config=config,
            node_role="subscriber_gateway",
            subscriber_id="sub_demo_001",
        ),
    )
    edge = WorkerNode(
        identity=NodeIdentity.generate(prefix="provider_edge_worker"),
        capability_profile=provider_capability_profile(config=config, node_role="provider_edge_worker"),
    )
    peer = WorkerNode(
        identity=NodeIdentity.generate(prefix="contributor_worker"),
        capability_profile=provider_capability_profile(config=config, node_role="contributor_worker"),
    )

    route_counts = {"local": 0, "provider_edge": 0, "peer": 0, "fallback_placeholder": 0}
    route_budgets = {"local": 1, "provider_edge": 1, "peer": None, "fallback_placeholder": None}

    first = select_provider_route(
        provider=config,
        subscriber_id="sub_demo_001",
        local_workers=[local],
        edge_workers=[edge],
        peer_workers=[peer],
        route_counts=route_counts,
        route_budgets=route_budgets,
    )
    assert first["route"] == "local"
    route_counts["local"] += 1

    second = select_provider_route(
        provider=config,
        subscriber_id="sub_demo_001",
        local_workers=[local],
        edge_workers=[edge],
        peer_workers=[peer],
        route_counts=route_counts,
        route_budgets=route_budgets,
    )
    assert second["route"] == "provider_edge"
    route_counts["provider_edge"] += 1

    third = select_provider_route(
        provider=config,
        subscriber_id="sub_demo_001",
        local_workers=[local],
        edge_workers=[edge],
        peer_workers=[peer],
        route_counts=route_counts,
        route_budgets=route_budgets,
    )
    assert third["route"] == "peer"


def test_provider_snapshot_includes_role_counts():
    config = ProviderConfig.create(provider_name="Demo Fibre AI", region="Hull", provider_id="provider_demo")
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    worker = WorkerNode(
        identity=NodeIdentity.generate(prefix="subscriber_gateway"),
        capability_profile=provider_capability_profile(
            config=config,
            node_role="subscriber_gateway",
            subscriber_id="sub_demo_001",
        ),
    )
    registration = NodeRegistration.create(node=worker.identity, capabilities=worker.capabilities())

    assert coordinator.register_signed_node(registration)
    snapshot = coordinator.snapshot()

    assert snapshot["nodes"][0]["node_role"] == "subscriber_gateway"
    assert snapshot["provider"]["subscriber_nodes"]["live"] == 1
    assert provider_snapshot_summary(snapshot)["subscriber_nodes"]["live"] == 1


def test_provider_status_report_summarizes_live_roles_without_token(tmp_path):
    token = "alpha-token-123"
    server, thread, coordinator_url = _start_public_alpha(token)
    provider_config_path = tmp_path / "provider-config.json"
    provider = ProviderConfig.create(
        provider_name="Demo Fibre AI",
        region="Hull",
        provider_id="provider_demo",
    ).with_subscriber("sub_demo_001", "Broadband AI Plus")
    write_provider_config(provider_config_path, provider)
    report_path = tmp_path / "provider-status.json"

    edge = WorkerNode(
        identity=NodeIdentity.generate(prefix="provider_edge_worker"),
        capability_profile=provider_capability_profile(config=provider, node_role="provider_edge_worker"),
    )
    peer = WorkerNode(
        identity=NodeIdentity.generate(prefix="contributor_worker"),
        capability_profile=provider_capability_profile(config=provider, node_role="contributor_worker"),
    )
    client = CoordinatorClient(coordinator_url, admission_token=token)

    try:
        assert client.register(NodeRegistration.create(node=edge.identity, capabilities=edge.capabilities()))["accepted"]
        assert client.register(NodeRegistration.create(node=peer.identity, capabilities=peer.capabilities()))["accepted"]
        client.create_job(
            job_type="eval.deterministic.v1",
            payload={
                "task": "arithmetic",
                "operation": "add",
                "operands": [1, 2],
                "expected": 3,
            },
            resource_requirements={
                "provider_mode": True,
                "provider_id": provider.provider_id,
                "provider_route": "provider_edge",
                "subscriber_id": "sub_demo_001",
            },
        )
        report = run_provider_status(
            ProviderStatusConfig(
                provider_config_path=provider_config_path,
                coordinator_url=coordinator_url,
                admission_token=token,
                expected_worker_id=peer.identity.node_id,
                report_path=report_path,
            )
        )
    finally:
        _stop_server(server, thread)

    assert report["schema"] == PROVIDER_STATUS_REPORT_SCHEMA
    assert report["status"] == "pass"
    assert report["summary"]["provider_edge_workers"]["live"] == 1
    assert report["summary"]["contributor_workers"]["live"] == 1
    assert report["summary"]["jobs_routed"]["provider_edge"] == 1
    assert report["expected_worker"]["node_role"] == "contributor_worker"
    assert report_path.exists()
    assert token not in json.dumps(report)


def test_provider_edge_proof_report_passes_and_is_serializable(tmp_path):
    config_path = tmp_path / "provider-config.json"
    config = ProviderConfig.create(
        provider_name="Demo Fibre AI",
        region="Hull",
        provider_id="provider_demo",
    )
    write_provider_config(config_path, config)
    report_path = tmp_path / "provider-edge-proof.json"

    report = run_provider_edge_proof(
        ProviderEdgeProofConfig(
            provider_config_path=config_path,
            subscribers=3,
            edge_workers=1,
            peer_workers=1,
            verifier_workers=1,
            jobs=9,
            report_path=report_path,
            timeout_seconds=30,
        )
    )

    assert report["schema"] == PROVIDER_EDGE_PROOF_REPORT_SCHEMA
    assert report["status"] == "pass"
    assert report["jobs_created"] == 9
    assert report["jobs_verified"] == 9
    assert report["jobs_disputed"] == 0
    assert report["route_counts"]["local"] >= 1
    assert report["route_counts"]["provider_edge"] >= 1
    assert report["route_counts"]["peer"] >= 1
    assert report["route_counts"]["fallback_placeholder"] == 0
    assert report["credit_summary"]["provider_internal_credits"] >= 1
    assert report["provider_snapshot"]["subscriber_nodes"]["live"] == 3
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "pass"
    json.dumps(report)


def test_provider_ops_pack_builds_summary_handoff_and_zip(tmp_path):
    config_path = tmp_path / "provider-config.json"
    config = ProviderConfig.create(
        provider_name="Demo Fibre AI",
        region="Hull",
        provider_id="provider_demo",
    )
    write_provider_config(config_path, config)
    out_dir = tmp_path / "provider-ops-pack"
    zip_path = tmp_path / "provider-ops-pack.zip"

    report = run_provider_ops_pack(
        ProviderOpsPackConfig(
            provider_config_path=config_path,
            out_dir=out_dir,
            subscribers=3,
            edge_workers=1,
            peer_workers=1,
            verifier_workers=1,
            jobs=9,
            timeout_seconds=30,
            zip_path=zip_path,
        )
    )

    assert report["schema"] == PROVIDER_OPS_PACK_SCHEMA
    assert report["status"] == "pass"
    assert report["proof"]["jobs_verified"] == 9
    assert report["proof"]["route_counts"]["fallback_placeholder"] == 0
    assert (out_dir / "provider-edge-proof.json").exists()
    assert (out_dir / "provider-ops-pack-summary.json").exists()
    assert (out_dir / "provider-ops-pack-summary.md").exists()
    assert (out_dir / "provider-handoff.md").exists()
    assert zip_path.exists()

    summary = json.loads((out_dir / "provider-ops-pack-summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "pass"
    assert summary["artifacts"]["zip"]["status"] == "created"

    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    assert "provider-ops-pack/provider-edge-proof.json" in names
    assert "provider-ops-pack/provider-ops-pack-summary.json" in names
    assert "provider-ops-pack/provider-handoff.md" in names


def test_provider_remote_proof_runs_on_public_alpha_with_provider_roles(tmp_path):
    token = "alpha-token-123"
    server, thread, coordinator_url = _start_public_alpha(token)
    provider_config_path = tmp_path / "provider-config.json"
    provider = ProviderConfig.create(
        provider_name="Demo Fibre AI",
        region="Hull",
        provider_id="provider_demo",
    ).with_subscriber("sub_demo_001", "Broadband AI Plus")
    write_provider_config(provider_config_path, provider)
    invite_path = tmp_path / "alpha-invite.json"
    write_alpha_invite(invite_path, AlphaInvite.create(coordinator=coordinator_url, admission_token=token))
    operator_home = tmp_path / ".mesh-operator"
    partner_home = tmp_path / ".mesh-partner"
    report_path = tmp_path / "provider-remote-proof.json"
    _save_provider_benchmark(operator_home, provider, "provider_edge_worker")
    _save_provider_benchmark(partner_home, provider, "contributor_worker")

    try:
        operator_join = run_alpha_join(
            AlphaJoinConfig(
                invite_path=invite_path,
                home=operator_home,
                worker_interval=0.2,
                startup_timeout_seconds=10.0,
                force=True,
            )
        )
        partner_join = run_alpha_join(
            AlphaJoinConfig(
                invite_path=invite_path,
                home=partner_home,
                worker_interval=0.2,
                startup_timeout_seconds=10.0,
                force=True,
            )
        )
        report = run_provider_remote_proof(
            ProviderRemoteProofConfig(
                provider_config_path=provider_config_path,
                coordinator_url=coordinator_url,
                admission_token=token,
                expected_worker_id=partner_join["worker_node_id"],
                subscriber_id="sub_demo_001",
                jobs=3,
                min_live_workers=2,
                timeout_seconds=15.0,
                poll_interval=0.2,
                report_path=report_path,
            )
        )
        snapshot = CoordinatorClient(coordinator_url, admission_token=token).snapshot()
    finally:
        stop_managed_process(home=operator_home, role="worker")
        stop_managed_process(home=partner_home, role="worker")
        _stop_server(server, thread)

    expected_node = next(node for node in snapshot["nodes"] if node["node_id"] == partner_join["worker_node_id"])
    assert operator_join["ok"] is True
    assert partner_join["ok"] is True
    assert expected_node["node_role"] == "contributor_worker"
    assert report["schema"] == PROVIDER_REMOTE_PROOF_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["criteria"]["verified_jobs"]["actual"] == 3
    assert report["criteria"]["expected_worker_results"]["actual"] >= 1
    assert report["expected_worker"]["node_role"] == "contributor_worker"
    assert report["requested_route_counts"] == {
        "fallback_placeholder": 0,
        "local": 1,
        "peer": 1,
        "provider_edge": 1,
    }
    assert report_path.exists()
    assert token not in json.dumps(report)


def test_provider_cli_commands_parse(tmp_path):
    parser = build_parser()

    bootstrap = parser.parse_args(
        [
            "operator",
            "bootstrap-provider",
            "--config",
            str(tmp_path / "provider-config.json"),
            "--provider-name",
            "Demo Fibre AI",
            "--region",
            "Hull",
        ]
    )
    subscriber = parser.parse_args(
        [
            "provider",
            "create-subscriber",
            "--config",
            str(tmp_path / "provider-config.json"),
            "--subscriber-id",
            "sub_demo_001",
            "--plan",
            "Broadband AI Plus",
        ]
    )
    join = parser.parse_args(
        [
            "node",
            "join-provider",
            "--provider-config",
            str(tmp_path / "provider-config.json"),
            "--subscriber-id",
            "sub_demo_001",
        ]
    )
    proof = parser.parse_args(
        [
            "proof",
            "provider-edge",
            "--provider-config",
            str(tmp_path / "provider-config.json"),
            "--jobs",
            "3",
        ]
    )
    ops_pack = parser.parse_args(
        [
            "operator",
            "provider-ops-pack",
            "--provider-config",
            str(tmp_path / "provider-config.json"),
            "--out",
            str(tmp_path / "provider-ops-pack"),
            "--jobs",
            "3",
        ]
    )
    remote_proof = parser.parse_args(
        [
            "operator",
            "provider-remote-proof",
            "--invite",
            str(tmp_path / "alpha-invite.json"),
            "--provider-config",
            str(tmp_path / "provider-config.json"),
            "--expected-worker-id",
            "worker_test",
            "--jobs",
            "3",
            "--report",
            str(tmp_path / "provider-remote-proof.json"),
        ]
    )
    provider_status = parser.parse_args(
        [
            "operator",
            "provider-status",
            "--invite",
            str(tmp_path / "alpha-invite.json"),
            "--provider-config",
            str(tmp_path / "provider-config.json"),
            "--expected-worker-id",
            "worker_test",
            "--report",
            str(tmp_path / "provider-status.json"),
        ]
    )

    assert bootstrap.func.__name__ == "bootstrap_provider_command"
    assert subscriber.func.__name__ == "provider_create_subscriber_command"
    assert join.func.__name__ == "node_join_provider_command"
    assert proof.func.__name__ == "run_proof_provider_edge"
    assert ops_pack.func.__name__ == "provider_ops_pack_command"
    assert remote_proof.func.__name__ == "provider_remote_proof_command"
    assert provider_status.func.__name__ == "provider_status_command"
