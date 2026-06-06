import json
import threading
import time
from urllib.request import Request, urlopen

from chatp2p.chat_gateway import (
    CHAT_GATEWAY_READINESS_SCHEMA,
    CHAT_GATEWAY_REPORT_SCHEMA,
    CHAT_GATEWAY_TRANSCRIPT_SCHEMA,
    DEFAULT_CHAT_GATEWAY_HOST,
    DEFAULT_CHAT_GATEWAY_PORT,
    ChatGatewayConfig,
    create_chat_gateway_server,
)
from chatp2p.chat_session import CHAT_SESSION_REPORT_SCHEMA
from chatp2p.chat_smoke import _ollama_capabilities, _start_fake_ollama
from chatp2p.client import CoordinatorClient
from chatp2p.cli import build_parser
from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.http_api import create_coordinator_http_server
from chatp2p.packets import NodeRegistration
from chatp2p.worker import WorkerNode


def test_chat_gateway_health_and_empty_status_redact_token(tmp_path):
    token = "alpha-token-chat-gateway-secret"
    server, thread, base_url = _start_gateway(
        ChatGatewayConfig(
            out_dir=tmp_path / "chat-session",
            session_id="demo",
            model="tiny-test-model",
            requester_account_id="requester_gateway_account",
            admission_token=token,
            port=0,
        )
    )
    try:
        health = _get_json(f"{base_url}/health")
        status = _get_json(f"{base_url}/api/session/status")
        transcript = _get_json(f"{base_url}/api/session/transcript")
        page = _get_text(f"{base_url}/")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert health["schema"] == CHAT_GATEWAY_REPORT_SCHEMA
    assert health["status"] == "pass"
    assert health["config"]["host"] == DEFAULT_CHAT_GATEWAY_HOST
    assert health["config"]["auth"]["admission_token_present"] is True
    assert health["endpoints"]["session_transcript"] == "/api/session/transcript"
    assert health["endpoints"]["chat_readiness"] == "/api/chat/readiness"
    assert status["status"] == "no_session"
    assert status["summary"]["recommended_next_action"] == "continue_chat_session"
    assert transcript["schema"] == CHAT_GATEWAY_TRANSCRIPT_SCHEMA
    assert transcript["status"] == "no_session"
    assert transcript["turns"] == []
    assert "/api/session/transcript" in page
    assert "/api/chat/readiness" in page
    assert "/api/chat/continue" in page
    assert "Session demo - tiny-test-model" in page
    assert 'id="blockedBanner"' in page
    assert 'id="readinessBadge"' in page
    assert 'id="modelRoute"' in page
    assert 'id="coordinatorState"' in page
    assert 'id="commandHint"' in page
    assert 'id="balance"' in page
    assert 'id="sendButton"' in page
    assert "turn user status-" in page
    assert "turn assistant status-" in page
    assert "Safe action:" in page
    assert "Balance ${balance}" in page
    assert 'sendButton.textContent = value ? "Sending" : "Send"' in page
    assert "innerHTML" not in page
    assert token not in json.dumps({"health": health, "status": status, "transcript": transcript})


def test_chat_gateway_readiness_ready_redacts_node_and_key(tmp_path):
    coordinator_server, coordinator_thread, coordinator_url, worker_identity = _start_coordinator_with_worker(
        requester_balance=2,
        models=["tiny-test-model"],
    )
    try:
        server, thread, base_url = _start_gateway(
            ChatGatewayConfig(
                out_dir=tmp_path / "chat-session",
                session_id="demo",
                coordinator_url=coordinator_url,
                model="tiny-test-model",
                requester_account_id="requester_gateway_account",
                port=0,
            )
        )
        try:
            readiness = _get_json(f"{base_url}/api/chat/readiness")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    finally:
        coordinator_server.shutdown()
        coordinator_server.server_close()
        coordinator_thread.join(timeout=2)

    serialized = json.dumps(readiness)
    assert readiness["schema"] == CHAT_GATEWAY_READINESS_SCHEMA
    assert readiness["status"] == "pass"
    assert readiness["summary"]["can_send"] is True
    assert readiness["summary"]["requester_balance"] == 2
    assert readiness["summary"]["live_eligible_node_count"] == 1
    assert readiness["summary"]["recommended_next_action"] == "continue_chat_session"
    assert readiness["summary"]["suggested_command"] is None
    assert readiness["action_hint"]["id"] == "continue_chat_session"
    assert readiness["action_hint"]["partner_required"] is False
    assert readiness["action_hint"]["commands"] == []
    assert worker_identity.node_id not in serialized
    assert worker_identity.public_key not in serialized


def test_chat_gateway_readiness_blocks_when_coordinator_unreachable(tmp_path):
    token = "alpha-token-readiness-secret"
    server, thread, base_url = _start_gateway(
        ChatGatewayConfig(
            out_dir=tmp_path / "chat-session",
            session_id="demo",
            coordinator_url="http://127.0.0.1:9",
            model="tiny-test-model",
            requester_account_id="requester_gateway_account",
            admission_token=token,
            client_timeout_seconds=0.1,
            port=0,
        )
    )
    try:
        readiness = _get_json(f"{base_url}/api/chat/readiness")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    serialized = json.dumps(readiness)
    assert readiness["status"] == "blocked"
    assert readiness["summary"]["can_send"] is False
    assert readiness["summary"]["coordinator_reachable"] is False
    assert readiness["summary"]["recommended_next_action"] == "start_or_check_local_coordinator"
    assert readiness["action_hint"]["commands"][0]["id"] == "check_coordinator"
    assert "python -m chatp2p.cli node status" in readiness["action_hint"]["primary_command"]
    assert "--coordinator http://127.0.0.1:9" in readiness["action_hint"]["primary_command"]
    assert token not in serialized


def test_chat_gateway_readiness_blocks_when_requester_has_no_credits(tmp_path):
    coordinator_server, coordinator_thread, coordinator_url, _worker_identity = _start_coordinator_with_worker(
        requester_balance=0,
        models=["tiny-test-model"],
    )
    try:
        server, thread, base_url = _start_gateway(
            ChatGatewayConfig(
                out_dir=tmp_path / "chat-session",
                session_id="demo",
                coordinator_url=coordinator_url,
                model="tiny-test-model",
                requester_account_id="requester_gateway_account",
                port=0,
            )
        )
        try:
            readiness = _get_json(f"{base_url}/api/chat/readiness")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    finally:
        coordinator_server.shutdown()
        coordinator_server.server_close()
        coordinator_thread.join(timeout=2)

    assert readiness["status"] == "blocked"
    assert readiness["summary"]["can_send"] is False
    assert readiness["summary"]["requester_balance"] == 0
    assert readiness["summary"]["credits_sufficient"] is False
    assert readiness["summary"]["recommended_next_action"] == "grant_requester_credits"
    assert [command["id"] for command in readiness["action_hint"]["commands"]] == [
        "inspect_requester_credits",
        "preview_credit_grant",
    ]
    assert "operator credits" in readiness["action_hint"]["commands"][0]["command"]
    assert "operator grant-requester-credits" in readiness["action_hint"]["commands"][1]["command"]
    assert "--operator-config '<private-operator-config.json>'" in readiness["action_hint"]["commands"][1]["command"]
    assert "--dry-run" in readiness["action_hint"]["commands"][1]["command"]


def test_chat_gateway_readiness_blocks_when_model_has_no_live_worker(tmp_path):
    coordinator_server, coordinator_thread, coordinator_url, _worker_identity = _start_coordinator_with_worker(
        requester_balance=2,
        models=["other-model"],
    )
    try:
        server, thread, base_url = _start_gateway(
            ChatGatewayConfig(
                out_dir=tmp_path / "chat-session",
                session_id="demo",
                coordinator_url=coordinator_url,
                model="tiny-test-model",
                requester_account_id="requester_gateway_account",
                port=0,
            )
        )
        try:
            readiness = _get_json(f"{base_url}/api/chat/readiness")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    finally:
        coordinator_server.shutdown()
        coordinator_server.server_close()
        coordinator_thread.join(timeout=2)

    assert readiness["status"] == "blocked"
    assert readiness["summary"]["can_send"] is False
    assert readiness["summary"]["live_eligible_node_count"] == 0
    assert readiness["model_routing"]["live_node_count"] == 1
    assert readiness["summary"]["recommended_next_action"] == "wait_for_model_capable_worker_or_change_model"
    assert readiness["action_hint"]["commands"][0]["id"] == "check_node_status"
    assert "node status" in readiness["action_hint"]["primary_command"]


def test_chat_gateway_readiness_blocks_unresolved_session_before_other_actions(tmp_path):
    session_path = _write_failed_session_fixture(tmp_path / "chat-session")
    coordinator_server, coordinator_thread, coordinator_url, _worker_identity = _start_coordinator_with_worker(
        requester_balance=2,
        models=["tiny-test-model"],
    )
    try:
        server, thread, base_url = _start_gateway(
            ChatGatewayConfig(
                out_dir=session_path.parent,
                session_id="demo",
                coordinator_url=coordinator_url,
                model="tiny-test-model",
                requester_account_id="requester_gateway_account",
                port=0,
            )
        )
        try:
            readiness = _get_json(f"{base_url}/api/chat/readiness")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    finally:
        coordinator_server.shutdown()
        coordinator_server.server_close()
        coordinator_thread.join(timeout=2)

    assert readiness["status"] == "blocked"
    assert readiness["summary"]["can_send"] is False
    assert readiness["summary"]["session_blocked"] is True
    assert readiness["summary"]["recommended_next_action"] == "run_session_resume_dry_run"
    assert readiness["action_hint"]["commands"][0]["id"] == "preview_resume"
    assert "chat session-resume" in readiness["action_hint"]["primary_command"]
    assert "--dry-run" in readiness["action_hint"]["primary_command"]


def test_chat_gateway_continue_creates_funded_turn(tmp_path):
    fake = _start_fake_ollama(model="tiny-test-model", answer="Gateway answer.")
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    requester_id = "requester_gateway_account"
    coordinator.apply_credit_delta(
        account_id=requester_id,
        account_type="requester",
        delta=2,
        reason="operator_credit_grant",
    )
    coordinator_server = create_coordinator_http_server(coordinator, host="127.0.0.1", port=0)
    coordinator_thread = threading.Thread(target=coordinator_server.serve_forever, daemon=True)
    coordinator_thread.start()

    worker_errors = []
    try:
        coordinator_host, coordinator_port = coordinator_server.server_address
        coordinator_url = f"http://{coordinator_host}:{coordinator_port}"
        client = CoordinatorClient(coordinator_url)
        worker_identity = NodeIdentity.generate(prefix="worker")
        worker = WorkerNode(
            identity=worker_identity,
            capability_profile=_ollama_capabilities(models=["tiny-test-model"], base_url=fake.base_url),
            ollama_base_url=fake.base_url,
        )
        registration = NodeRegistration.create(node=worker_identity, capabilities=worker.capabilities())
        assert client.register(registration)["accepted"] is True

        gateway_server, gateway_thread, gateway_url = _start_gateway(
            ChatGatewayConfig(
                out_dir=tmp_path / "chat-session",
                session_id="demo",
                coordinator_url=coordinator_url,
                model="tiny-test-model",
                requester_account_id=requester_id,
                timeout_seconds=10,
                poll_interval=0.1,
                port=0,
            )
        )
        worker_thread = threading.Thread(
            target=_run_worker_until_jobs,
            args=(client, worker, worker_errors, 1),
            daemon=True,
        )
        worker_thread.start()
        try:
            report = _post_json(f"{gateway_url}/api/chat/continue", {"prompt": "Hello gateway"})
            status = _get_json(f"{gateway_url}/api/session/status")
            transcript = _get_json(f"{gateway_url}/api/session/transcript")
        finally:
            gateway_server.shutdown()
            gateway_server.server_close()
            gateway_thread.join(timeout=2)
        worker_thread.join(timeout=2)
    finally:
        fake.stop()
        coordinator_server.shutdown()
        coordinator_server.server_close()
        coordinator_thread.join(timeout=2)

    assert worker_errors == []
    assert report["status"] == "pass"
    assert report["summary"]["latest_turn"]["answer"] == "Gateway answer."
    assert status["status"] == "pass"
    assert status["summary"]["turn_count"] == 1
    assert transcript["schema"] == CHAT_GATEWAY_TRANSCRIPT_SCHEMA
    assert transcript["status"] == "pass"
    assert transcript["summary"]["turn_count"] == 1
    assert transcript["turns"][0]["prompt"] == "Hello gateway"
    assert transcript["turns"][0]["answer"] == "Gateway answer."
    assert transcript["turns"][0]["job_status"] == "verified"
    assert transcript["turns"][0]["worker_node_id_redacted"].endswith("...")
    assert worker_identity.node_id not in json.dumps(transcript)
    assert "alpha-token" not in json.dumps({"report": report, "status": status, "transcript": transcript})


def test_chat_gateway_continue_blocks_unresolved_turn_without_spend(tmp_path):
    session_path = _write_failed_session_fixture(tmp_path / "chat-session")
    before = json.loads(session_path.read_text(encoding="utf-8"))
    server, thread, base_url = _start_gateway(
        ChatGatewayConfig(
            out_dir=session_path.parent,
            session_id="demo",
            coordinator_url="http://127.0.0.1:9",
            model="tiny-test-model",
            requester_account_id="requester_gateway_account",
            client_timeout_seconds=0.1,
            port=0,
        )
    )
    try:
        report = _post_json(f"{base_url}/api/chat/continue", {"prompt": "Do not spend"})
        transcript = _get_json(f"{base_url}/api/session/transcript")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    after = json.loads(session_path.read_text(encoding="utf-8"))

    assert report["status"] == "blocked"
    assert report["summary"]["turn_created"] is False
    assert report["summary"]["blocked_reason"] == "unresolved_session_turns"
    assert transcript["status"] == "fail"
    assert transcript["summary"]["turn_count"] == 2
    assert transcript["turns"][1]["status"] == "fail"
    assert transcript["turns"][1]["answer"] is None
    assert after == before


def test_chat_gateway_sync_and_resume_endpoints_use_safe_flows(monkeypatch, tmp_path):
    calls = {}

    def fake_sync(config):
        calls["sync"] = config
        return {
            "schema": "chatp2p.chat-session-sync-report.v1",
            "ok": True,
            "status": "pass",
            "summary": {"recommended_next_action": "continue_chat_session"},
        }

    def fake_resume(config):
        calls["resume"] = config
        return {
            "schema": "chatp2p.chat-session-resume-report.v1",
            "ok": True,
            "status": "pass",
            "summary": {"dry_run": config.dry_run, "recommended_next_action": "rerun_without_dry_run"},
        }

    monkeypatch.setattr("chatp2p.chat_gateway.run_chat_session_sync", fake_sync)
    monkeypatch.setattr("chatp2p.chat_gateway.run_chat_session_resume", fake_resume)

    server, thread, base_url = _start_gateway(
        ChatGatewayConfig(
            out_dir=tmp_path / "chat-session",
            session_id="demo",
            model="tiny-test-model",
            requester_account_id="requester_gateway_account",
            port=0,
        )
    )
    try:
        sync = _post_json(f"{base_url}/api/session/sync", {})
        resume = _post_json(f"{base_url}/api/session/resume-dry-run", {})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert sync["status"] == "pass"
    assert resume["status"] == "pass"
    assert calls["sync"].session_id == "demo"
    assert calls["resume"].dry_run is True


def test_chat_gateway_parser_accepts_required_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "chat",
            "gateway",
            "--out",
            "D:\\ChatP2PData\\chat-session",
            "--session-id",
            "demo",
            "--model",
            "tiny-test-model",
            "--requester-account-id",
            "requester_demo",
        ]
    )

    assert args.func.__name__ == "run_chat_gateway_command"
    assert args.chat_command == "gateway"
    assert args.host == DEFAULT_CHAT_GATEWAY_HOST
    assert args.port == DEFAULT_CHAT_GATEWAY_PORT


def _start_gateway(config):
    server = create_chat_gateway_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _start_coordinator_with_worker(*, requester_balance, models):
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    if requester_balance:
        coordinator.apply_credit_delta(
            account_id="requester_gateway_account",
            account_type="requester",
            delta=requester_balance,
            reason="operator_credit_grant",
        )
    server = create_coordinator_http_server(coordinator, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    coordinator_url = f"http://{host}:{port}"
    client = CoordinatorClient(coordinator_url)
    worker_identity = NodeIdentity.generate(prefix="worker")
    worker = WorkerNode(
        identity=worker_identity,
        capability_profile=_ollama_capabilities(models=models, base_url="http://127.0.0.1:11434"),
        ollama_base_url="http://127.0.0.1:11434",
    )
    registration = NodeRegistration.create(node=worker_identity, capabilities=worker.capabilities())
    assert client.register(registration)["accepted"] is True
    return server, thread, coordinator_url, worker_identity


def _get_json(url):
    return _request_json(url)


def _get_text(url):
    with urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8")


def _post_json(url, payload):
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    return _request_json(request)


def _request_json(request):
    last_error = None
    for _ in range(3):
        try:
            with urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except ConnectionAbortedError as exc:
            last_error = exc
            time.sleep(0.05)
    raise last_error


def _run_worker_until_jobs(client, worker, errors, count):
    deadline = time.time() + 8
    completed = 0
    while time.time() <= deadline and completed < count:
        try:
            job = client.next_job(worker.identity)
            if job is None:
                time.sleep(0.05)
                continue
            result = worker.run_job(job)
            response = client.submit_result(result)
            if not response.get("accepted"):
                errors.append(f"result rejected: {response}")
                return
            completed += 1
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            return
    if completed != count:
        errors.append(f"worker completed {completed}/{count} jobs")


def _write_failed_session_fixture(path):
    path.mkdir(parents=True, exist_ok=True)
    session = {
        "schema": CHAT_SESSION_REPORT_SCHEMA,
        "ok": False,
        "status": "fail",
        "session_id": "demo",
        "title": "demo",
        "created_at": "2026-06-05T00:00:00+00:00",
        "updated_at": "2026-06-05T00:01:00+00:00",
        "config": {
            "out_dir": str(path),
            "session_id": "demo",
            "coordinator": None,
            "invite_path": None,
            "auth": {"admission_token_present": False},
            "model": "tiny-test-model",
            "system": "Be concise.",
            "requester_account_id": "requester_gateway_account",
            "job_cost": 1,
            "reward": 1,
            "temperature": 0.2,
            "max_tokens": 256,
            "ttl_seconds": 300,
            "timeout_seconds": 10.0,
            "poll_interval": 0.1,
            "no_wait": False,
            "max_context_turns": 8,
            "remote_side_effect": "create_funded_chat_job",
        },
        "turns": [
            {
                "turn_id": "turn-0001",
                "turn_index": 1,
                "created_at": "2026-06-05T00:00:00+00:00",
                "ok": True,
                "status": "pass",
                "prompt": "First question?",
                "answer": "Existing answer.",
                "model": "tiny-test-model",
                "job_id": "job_first",
                "job_status": "verified",
                "worker_node_id": "worker_fixture",
                "requester_balance_after": 1,
                "worker_balance_after": 1,
                "recommended_next_action": "continue_chat_session",
                "context_message_count": 0,
                "artifacts": {},
                "errors": [],
            },
            {
                "turn_id": "turn-0002",
                "turn_index": 2,
                "created_at": "2026-06-05T00:01:00+00:00",
                "ok": False,
                "status": "fail",
                "prompt": "Failed question?",
                "answer": None,
                "model": "tiny-test-model",
                "job_id": None,
                "job_status": None,
                "worker_node_id": None,
                "requester_balance_after": 1,
                "worker_balance_after": None,
                "recommended_next_action": "check_live_ollama_workers",
                "context_message_count": 2,
                "artifacts": {},
                "errors": ["TimeoutError: chat job did not verify before timeout"],
            },
        ],
        "summary": {
            "status": "fail",
            "turn_count": 2,
            "completed_turns": 1,
            "submitted_turns": 0,
            "failed_turns": 1,
            "recommended_next_action": "check_live_ollama_workers",
        },
        "errors": ["TimeoutError: chat job did not verify before timeout"],
        "artifacts": {
            "json": str(path / "chat-session.json"),
            "markdown": str(path / "chat-session.md"),
        },
    }
    session_path = path / "chat-session.json"
    session_path.write_text(json.dumps(session, indent=2, sort_keys=True), encoding="utf-8")
    return session_path
