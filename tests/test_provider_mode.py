import json

from chatp2p.cli import build_parser
from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.packets import NodeRegistration
from chatp2p.provider import (
    PROVIDER_CONFIG_SCHEMA,
    PROVIDER_EDGE_PROOF_REPORT_SCHEMA,
    ProviderConfig,
    ProviderEdgeProofConfig,
    add_provider_subscriber,
    bootstrap_provider_config,
    join_provider_node,
    load_provider_config,
    provider_capability_profile,
    provider_snapshot_summary,
    run_provider_edge_proof,
    select_provider_route,
    write_provider_config,
)
from chatp2p.worker import WorkerNode


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

    assert bootstrap.func.__name__ == "bootstrap_provider_command"
    assert subscriber.func.__name__ == "provider_create_subscriber_command"
    assert join.func.__name__ == "node_join_provider_command"
    assert proof.func.__name__ == "run_proof_provider_edge"
