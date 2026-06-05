import json
import threading
import time

from chatp2p.chat_session import CHAT_SESSION_REPORT_SCHEMA, ChatSessionConfig, run_chat_session
from chatp2p.chat_smoke import _ollama_capabilities, _start_fake_ollama
from chatp2p.client import CoordinatorClient
from chatp2p.cli import build_parser
from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.http_api import create_coordinator_http_server
from chatp2p.packets import NodeRegistration
from chatp2p.worker import WorkerNode


def test_chat_session_appends_turns_and_reuses_verified_context(tmp_path):
    fake = _start_fake_ollama(model="tiny-test-model", answer="Session answer.")
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    requester_id = "requester_session_account"
    coordinator.apply_credit_delta(
        account_id=requester_id,
        account_type="requester",
        delta=4,
        reason="operator_credit_grant",
    )
    server = create_coordinator_http_server(coordinator, host="127.0.0.1", port=0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    worker_errors = []
    try:
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        client = CoordinatorClient(base_url)
        worker_identity = NodeIdentity.generate(prefix="worker")
        worker = WorkerNode(
            identity=worker_identity,
            capability_profile=_ollama_capabilities(models=["tiny-test-model"], base_url=fake.base_url),
            ollama_base_url=fake.base_url,
        )
        registration = NodeRegistration.create(node=worker_identity, capabilities=worker.capabilities())
        assert client.register(registration)["accepted"] is True

        worker_thread = threading.Thread(
            target=_run_worker_until_jobs,
            args=(client, worker, worker_errors, 2),
            daemon=True,
        )
        worker_thread.start()
        first = run_chat_session(
            ChatSessionConfig(
                out_dir=tmp_path / "chat-session",
                coordinator_url=base_url,
                session_id="demo",
                model="tiny-test-model",
                prompt="First question?",
                requester_account_id=requester_id,
                job_cost=1,
                reward=1,
                timeout_seconds=10,
                poll_interval=0.1,
            )
        )
        second = run_chat_session(
            ChatSessionConfig(
                out_dir=tmp_path / "chat-session",
                coordinator_url=base_url,
                session_id="demo",
                model="tiny-test-model",
                prompt="Second question?",
                requester_account_id=requester_id,
                job_cost=1,
                reward=1,
                timeout_seconds=10,
                poll_interval=0.1,
            )
        )
        worker_thread.join(timeout=2)
    finally:
        fake.stop()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    assert worker_errors == []
    assert first["schema"] == CHAT_SESSION_REPORT_SCHEMA
    assert first["status"] == "pass"
    assert first["summary"]["turn_count"] == 1
    assert second["status"] == "pass"
    assert second["summary"]["turn_count"] == 2
    assert second["summary"]["completed_turns"] == 2
    assert second["summary"]["latest_turn"]["answer"] == "Session answer."
    assert second["summary"]["recommended_next_action"] == "continue_chat_session"
    assert second["turns"][1]["context_message_count"] == 2
    assert second["turns"][1]["requester_balance_after"] == 2
    assert (tmp_path / "chat-session" / "chat-session.json").exists()
    assert (tmp_path / "chat-session" / "chat-session.md").exists()
    assert (tmp_path / "chat-session" / "turn-0002" / "chat-ask.json").exists()
    assert len(fake.requests) == 2
    assert "USER: First question?" in fake.requests[1]["prompt"]
    assert "ASSISTANT: Session answer." in fake.requests[1]["prompt"]
    assert "alpha-token" not in json.dumps(second)


def test_chat_session_cli_parses():
    parser = build_parser()

    args = parser.parse_args(
        [
            "chat",
            "session",
            "--invite",
            "D:\\ChatP2PData\\alpha-invite.json",
            "--out",
            "D:\\ChatP2PData\\chat-session",
            "--session-id",
            "demo",
            "--model",
            "tiny-test-model",
            "--prompt",
            "Explain ChatP2P",
            "--requester-account-id",
            "requester_demo",
            "--job-cost",
            "2",
            "--max-context-turns",
            "4",
            "--json",
        ]
    )

    assert args.func.__name__ == "run_chat_session_command"
    assert args.chat_command == "session"
    assert args.session_id == "demo"
    assert args.requester_account_id == "requester_demo"
    assert args.job_cost == 2
    assert args.max_context_turns == 4


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
