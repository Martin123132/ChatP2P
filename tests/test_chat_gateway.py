import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from chatp2p.chat_gateway import (
    CHAT_GATEWAY_MODEL_CATALOG_SCHEMA,
    CHAT_GATEWAY_READINESS_SCHEMA,
    CHAT_GATEWAY_REPORT_SCHEMA,
    CHAT_GATEWAY_SESSION_CONTROL_SCHEMA,
    CHAT_GATEWAY_SESSIONS_SCHEMA,
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
        sessions = _get_json(f"{base_url}/api/sessions")
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
    assert health["endpoints"]["sessions"] == "/api/sessions"
    assert health["endpoints"]["chat_readiness"] == "/api/chat/readiness"
    assert health["endpoints"]["chat_models"] == "/api/chat/models"
    assert health["endpoints"]["session_reset_dry_run"] == "/api/session/reset-dry-run"
    assert health["endpoints"]["session_archive_dry_run"] == "/api/session/archive-dry-run"
    assert sessions["schema"] == CHAT_GATEWAY_SESSIONS_SCHEMA
    assert sessions["summary"]["current_session_id"] == "demo"
    assert sessions["sessions"][0]["session_id"] == "demo"
    assert sessions["sessions"][0]["status"] == "no_session"
    assert status["status"] == "no_session"
    assert status["summary"]["recommended_next_action"] == "continue_chat_session"
    assert transcript["schema"] == CHAT_GATEWAY_TRANSCRIPT_SCHEMA
    assert transcript["status"] == "no_session"
    assert transcript["turns"] == []
    assert "/api/session/transcript" in page
    assert "/api/sessions" in page
    assert "/api/chat/readiness" in page
    assert "/api/chat/models" in page
    assert "/api/chat/continue" in page
    assert "/api/session/reset-dry-run" in page
    assert "/api/session/archive-dry-run" in page
    assert 'Session <span id="sessionLabel">demo</span> - tiny-test-model' in page
    assert 'id="blockedBanner"' in page
    assert 'id="readinessBadge"' in page
    assert 'id="modelRoute"' in page
    assert 'id="modelCatalog"' in page
    assert 'id="modelSelect"' in page
    assert 'id="sessionLabel"' in page
    assert 'id="sessionsList"' in page
    assert 'id="newSessionButton"' in page
    assert 'id="apiErrorBanner"' in page
    assert 'id="coordinatorState"' in page
    assert 'id="commandHint"' in page
    assert 'id="balance"' in page
    assert 'id="sendButton"' in page
    assert 'id="safeActionButton"' in page
    assert 'id="resetButton"' in page
    assert 'id="archiveButton"' in page
    assert "turn user status-" in page
    assert "turn assistant status-" in page
    assert "Safe action:" in page
    assert "Balance ${balance}" in page
    assert 'sendButton.textContent = value ? "Sending" : "Send"' in page
    assert "safeActionEndpoints" in page
    assert "renderApiError" in page
    assert "Run Safe Action" in page
    assert "New Session Dry Run" in page
    assert "Archive Dry Run" in page
    assert "Conversations" in page
    assert "session_id: activeSessionId" in page
    assert "setActiveSession" in page
    assert "model: selectedModel()" in page
    assert "innerHTML" not in page
    assert "\u00b7" not in page
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
    assert readiness["summary"]["error_category"] == "coordinator_unreachable"
    assert readiness["error_category"] == "coordinator_unreachable"
    assert readiness["action_hint"]["error_category"] == "coordinator_unreachable"
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
    assert readiness["summary"]["error_category"] == "insufficient_credits"
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
    assert readiness["summary"]["error_category"] == "no_model_worker"
    assert readiness["model_routing"]["live_node_count"] == 1
    assert readiness["summary"]["recommended_next_action"] == "wait_for_model_capable_worker_or_change_model"
    assert readiness["action_hint"]["commands"][0]["id"] == "check_node_status"
    assert "node status" in readiness["action_hint"]["primary_command"]


def test_chat_gateway_readiness_accepts_model_query_override(tmp_path):
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
            default_readiness = _get_json(f"{base_url}/api/chat/readiness")
            override_readiness = _get_json(f"{base_url}/api/chat/readiness?model=other-model")
            override_catalog = _get_json(f"{base_url}/api/chat/models?model=other-model")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    finally:
        coordinator_server.shutdown()
        coordinator_server.server_close()
        coordinator_thread.join(timeout=2)

    assert default_readiness["status"] == "blocked"
    assert default_readiness["summary"]["model"] == "tiny-test-model"
    assert default_readiness["summary"]["live_eligible_node_count"] == 0
    assert override_readiness["status"] == "pass"
    assert override_readiness["summary"]["model"] == "other-model"
    assert override_readiness["summary"]["live_eligible_node_count"] == 1
    assert override_catalog["summary"]["selected_model"] == "other-model"
    assert override_catalog["summary"]["selected_model_sendable"] is True


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
    assert readiness["summary"]["error_category"] == "unresolved_session"
    assert readiness["summary"]["recommended_next_action"] == "run_session_resume_dry_run"
    assert readiness["action_hint"]["commands"][0]["id"] == "preview_resume"
    assert "chat session-resume" in readiness["action_hint"]["primary_command"]
    assert "--dry-run" in readiness["action_hint"]["primary_command"]


def test_chat_gateway_model_catalog_lists_sendable_models_redacts_nodes(tmp_path):
    coordinator_server, coordinator_thread, coordinator_url, worker_identity = _start_coordinator_with_worker(
        requester_balance=2,
        models=["tiny-test-model", "other-model"],
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
            catalog = _get_json(f"{base_url}/api/chat/models")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    finally:
        coordinator_server.shutdown()
        coordinator_server.server_close()
        coordinator_thread.join(timeout=2)

    serialized = json.dumps(catalog)
    assert catalog["schema"] == CHAT_GATEWAY_MODEL_CATALOG_SCHEMA
    assert catalog["status"] == "pass"
    assert catalog["summary"]["selected_model"] == "tiny-test-model"
    assert catalog["summary"]["selected_model_sendable"] is True
    assert catalog["summary"]["available_model_count"] == 2
    assert catalog["summary"]["sendable_model_count"] == 2
    assert catalog["summary"]["recommended_model"] == "tiny-test-model"
    assert catalog["summary"]["recommended_next_action"] == "continue_chat_session"
    assert {item["model"] for item in catalog["models"]} == {"tiny-test-model", "other-model"}
    assert all(item["sendable"] is True for item in catalog["models"])
    assert worker_identity.node_id not in serialized
    assert worker_identity.public_key not in serialized


def test_chat_gateway_model_catalog_recommends_available_model_when_selected_missing(tmp_path):
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
            catalog = _get_json(f"{base_url}/api/chat/models")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    finally:
        coordinator_server.shutdown()
        coordinator_server.server_close()
        coordinator_thread.join(timeout=2)

    assert catalog["status"] == "pass"
    assert catalog["summary"]["selected_model_sendable"] is False
    assert catalog["summary"]["recommended_model"] == "other-model"
    assert catalog["summary"]["recommended_next_action"] == "change_model_to_recommended_model"


def test_chat_gateway_model_catalog_handles_unreachable_coordinator(tmp_path):
    token = "alpha-token-model-catalog-secret"
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
        catalog = _get_json(f"{base_url}/api/chat/models")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    serialized = json.dumps(catalog)
    assert catalog["schema"] == CHAT_GATEWAY_MODEL_CATALOG_SCHEMA
    assert catalog["ok"] is False
    assert catalog["status"] == "warn"
    assert catalog["summary"]["available_model_count"] == 0
    assert catalog["summary"]["recommended_next_action"] == "start_or_check_local_coordinator"
    assert catalog["models"] == []
    assert token not in serialized


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


def test_chat_gateway_continue_uses_request_model_override(tmp_path):
    fake = _start_fake_ollama(model="other-model", answer="Override answer.")
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
            capability_profile=_ollama_capabilities(models=["other-model"], base_url=fake.base_url),
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
            report = _post_json(
                f"{gateway_url}/api/chat/continue",
                {"prompt": "Hello override", "model": "other-model"},
            )
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
    assert report["summary"]["latest_turn"]["answer"] == "Override answer."
    assert transcript["turns"][0]["model"] == "other-model"
    assert transcript["turns"][0]["answer"] == "Override answer."
    assert worker_identity.node_id not in json.dumps(transcript)


def test_chat_gateway_continue_rejects_invalid_model_without_spend(tmp_path):
    server, thread, base_url = _start_gateway(
        ChatGatewayConfig(
            out_dir=tmp_path / "chat-session",
            session_id="demo",
            coordinator_url="http://127.0.0.1:9",
            model="tiny-test-model",
            requester_account_id="requester_gateway_account",
            client_timeout_seconds=0.1,
            port=0,
        )
    )
    try:
        status_code, payload = _post_json_error(
            f"{base_url}/api/chat/continue",
            {"prompt": "Do not spend", "model": "bad model"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status_code == 400
    assert payload["status"] == "fail"
    assert payload["error_category"] == "invalid_model"
    assert payload["summary"]["error_category"] == "invalid_model"
    assert payload["summary"]["recommended_next_action"] == "choose_valid_model"
    assert payload["action_hint"]["id"] == "choose_valid_model"
    assert payload["action_hint"]["partner_required"] is False
    assert "model" in payload["error"]
    assert not (tmp_path / "chat-session" / "chat-session.json").exists()


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
    assert report["error_category"] == "unresolved_session"
    assert report["summary"]["error_category"] == "unresolved_session"
    assert report["summary"]["turn_created"] is False
    assert report["summary"]["blocked_reason"] == "unresolved_session_turns"
    assert report["action_hint"]["id"] == "run_session_resume_dry_run"
    assert report["action_hint"]["partner_required"] is False
    assert report["action_hint"]["commands"][0]["id"] == "preview_resume"
    assert transcript["status"] == "fail"
    assert transcript["summary"]["turn_count"] == 2
    assert transcript["turns"][1]["status"] == "fail"
    assert transcript["turns"][1]["answer"] is None
    assert after == before


def test_chat_gateway_continue_categorizes_request_timeout(tmp_path):
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
    try:
        coordinator_host, coordinator_port = coordinator_server.server_address
        coordinator_url = f"http://{coordinator_host}:{coordinator_port}"
        server, thread, base_url = _start_gateway(
            ChatGatewayConfig(
                out_dir=tmp_path / "chat-session",
                session_id="demo",
                coordinator_url=coordinator_url,
                model="tiny-test-model",
                requester_account_id=requester_id,
                timeout_seconds=0.1,
                poll_interval=0.05,
                port=0,
            )
        )
        try:
            report = _post_json(f"{base_url}/api/chat/continue", {"prompt": "No worker will answer"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    finally:
        coordinator_server.shutdown()
        coordinator_server.server_close()
        coordinator_thread.join(timeout=2)

    assert report["status"] == "fail"
    assert report["error_category"] == "request_timeout"
    assert report["summary"]["error_category"] == "request_timeout"
    assert report["summary"]["recommended_next_action"] == "run_session_sync"
    assert report["action_hint"]["id"] == "run_session_sync"
    assert report["action_hint"]["commands"][0]["id"] == "sync_session"
    assert "TimeoutError" in json.dumps(report["session"])


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


def test_chat_gateway_reset_and_archive_dry_run_are_report_only(tmp_path):
    session_path = _write_failed_session_fixture(tmp_path / "chat-session")
    token = "alpha-token-session-control-secret"
    before = json.loads(session_path.read_text(encoding="utf-8"))
    server, thread, base_url = _start_gateway(
        ChatGatewayConfig(
            out_dir=session_path.parent,
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
        reset = _post_json(f"{base_url}/api/session/reset-dry-run", {})
        archive = _post_json(f"{base_url}/api/session/archive-dry-run", {})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    after = json.loads(session_path.read_text(encoding="utf-8"))

    for report, action_id in [(reset, "reset_session_dry_run"), (archive, "archive_session_dry_run")]:
        serialized = json.dumps(report)
        assert report["schema"] == CHAT_GATEWAY_SESSION_CONTROL_SCHEMA
        assert report["status"] == "pass"
        assert report["dry_run"] is True
        assert report["action"]["id"] == action_id
        assert report["action"]["local_only"] is True
        assert report["action"]["partner_required"] is False
        assert report["action"]["credit_spend"] is False
        assert report["action"]["will_modify"] is False
        assert report["summary"]["session_exists"] is True
        assert report["summary"]["file_count"] >= 1
        assert report["plan"]["files_to_archive"][0]["name"] == "chat-session.json"
        assert token not in serialized

    assert after == before


def test_chat_gateway_session_control_dry_run_handles_missing_session(tmp_path):
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
        reset = _post_json(f"{base_url}/api/session/reset-dry-run", {})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert reset["schema"] == CHAT_GATEWAY_SESSION_CONTROL_SCHEMA
    assert reset["status"] == "no_session"
    assert reset["summary"]["session_exists"] is False
    assert reset["summary"]["recommended_next_action"] == "continue_chat_session"
    assert reset["warnings"] == ["no_session_to_archive_or_reset"]
    assert not (tmp_path / "chat-session" / "chat-session.json").exists()


def test_chat_gateway_sessions_list_safe_local_summaries(tmp_path):
    root = tmp_path / "sessions"
    _write_chat_session_fixture(
        root / "demo",
        session_id="demo",
        prompt="Do not list this prompt",
        answer="Do not list this answer",
        worker_node_id="worker_fixture_sensitive_one",
        updated_at="2026-06-06T10:00:00+00:00",
    )
    _write_chat_session_fixture(
        root / "second",
        session_id="second",
        prompt="Hidden second prompt",
        answer="Hidden second answer",
        worker_node_id="worker_fixture_sensitive_two",
        updated_at="2026-06-06T11:00:00+00:00",
    )
    token = "alpha-token-session-list-secret"
    server, thread, base_url = _start_gateway(
        ChatGatewayConfig(
            out_dir=root / "demo",
            session_id="demo",
            sessions_root=root,
            model="tiny-test-model",
            requester_account_id="requester_gateway_account",
            admission_token=token,
            port=0,
        )
    )
    try:
        sessions = _get_json(f"{base_url}/api/sessions")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    serialized = json.dumps(sessions)
    assert sessions["schema"] == CHAT_GATEWAY_SESSIONS_SCHEMA
    assert sessions["status"] == "pass"
    assert sessions["summary"]["session_count"] == 2
    assert sessions["summary"]["current_session_id"] == "demo"
    assert [session["session_id"] for session in sessions["sessions"]] == ["demo", "second"]
    assert sessions["sessions"][0]["current"] is True
    assert sessions["sessions"][0]["turn_count"] == 1
    assert sessions["sessions"][0]["latest_model"] == "tiny-test-model"
    assert "Do not list this prompt" not in serialized
    assert "Do not list this answer" not in serialized
    assert "worker_fixture_sensitive_one" not in serialized
    assert token not in serialized


def test_chat_gateway_selected_session_query_reads_safe_sibling_session(tmp_path):
    root = tmp_path / "sessions"
    _write_chat_session_fixture(
        root / "demo",
        session_id="demo",
        prompt="Default prompt",
        answer="Default answer",
        worker_node_id="worker_default",
        updated_at="2026-06-06T10:00:00+00:00",
    )
    _write_chat_session_fixture(
        root / "second",
        session_id="second",
        prompt="Second prompt",
        answer="Second answer",
        worker_node_id="worker_second",
        updated_at="2026-06-06T11:00:00+00:00",
    )
    server, thread, base_url = _start_gateway(
        ChatGatewayConfig(
            out_dir=root / "demo",
            session_id="demo",
            sessions_root=root,
            model="tiny-test-model",
            requester_account_id="requester_gateway_account",
            port=0,
        )
    )
    try:
        default_transcript = _get_json(f"{base_url}/api/session/transcript")
        selected_transcript = _get_json(f"{base_url}/api/session/transcript?session_id=second")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert default_transcript["session_id"] == "demo"
    assert default_transcript["turns"][0]["prompt"] == "Default prompt"
    assert selected_transcript["session_id"] == "second"
    assert selected_transcript["turns"][0]["prompt"] == "Second prompt"
    assert selected_transcript["turns"][0]["answer"] == "Second answer"


def test_chat_gateway_rejects_invalid_session_override(tmp_path):
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
        get_code, get_error = _get_json_error(f"{base_url}/api/session/transcript?session_id=..%2Fsecret")
        post_code, post_error = _post_json_error(
            f"{base_url}/api/chat/continue",
            {"prompt": "Hello", "session_id": "bad/path"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert get_code == 400
    assert get_error["error_category"] == "invalid_session"
    assert get_error["summary"]["recommended_next_action"] == "choose_valid_session"
    assert post_code == 400
    assert post_error["error_category"] == "invalid_session"
    assert not (tmp_path / "secret").exists()


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
            "--sessions-root",
            "D:\\ChatP2PData\\chat-sessions",
            "--model",
            "tiny-test-model",
            "--requester-account-id",
            "requester_demo",
        ]
    )

    assert args.func.__name__ == "run_chat_gateway_command"
    assert args.chat_command == "gateway"
    assert args.sessions_root == "D:\\ChatP2PData\\chat-sessions"
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


def _get_json_error(url):
    try:
        with urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_json(url, payload):
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    return _request_json(request)


def _post_json_error(url, payload):
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        return 200, _request_json(request)
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


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


def _write_chat_session_fixture(
    path,
    *,
    session_id,
    prompt,
    answer,
    worker_node_id,
    updated_at,
):
    path.mkdir(parents=True, exist_ok=True)
    session = {
        "schema": CHAT_SESSION_REPORT_SCHEMA,
        "ok": True,
        "status": "pass",
        "session_id": session_id,
        "title": session_id,
        "created_at": "2026-06-06T09:00:00+00:00",
        "updated_at": updated_at,
        "config": {
            "out_dir": str(path),
            "session_id": session_id,
            "model": "tiny-test-model",
            "requester_account_id": "requester_gateway_account",
        },
        "turns": [
            {
                "turn_id": "turn-0001",
                "turn_index": 1,
                "created_at": "2026-06-06T09:01:00+00:00",
                "ok": True,
                "status": "pass",
                "prompt": prompt,
                "answer": answer,
                "model": "tiny-test-model",
                "job_id": "job_fixture",
                "job_status": "verified",
                "worker_node_id": worker_node_id,
                "requester_balance_after": 1,
                "worker_balance_after": 1,
                "recommended_next_action": "continue_chat_session",
                "context_message_count": 0,
                "artifacts": {},
                "errors": [],
            }
        ],
        "summary": {
            "status": "pass",
            "turn_count": 1,
            "completed_turns": 1,
            "submitted_turns": 0,
            "failed_turns": 0,
            "recommended_next_action": "continue_chat_session",
        },
        "errors": [],
        "artifacts": {"json": str(path / "chat-session.json")},
    }
    session_path = path / "chat-session.json"
    session_path.write_text(json.dumps(session, indent=2, sort_keys=True), encoding="utf-8")
    return session_path


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
