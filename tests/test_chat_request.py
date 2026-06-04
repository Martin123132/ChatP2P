import json
import threading
import time

from chatp2p.alpha import AlphaInvite, write_alpha_invite
from chatp2p.chat_request import CHAT_ASK_REPORT_SCHEMA, ChatAskConfig, run_chat_ask
from chatp2p.chat_smoke import _ollama_capabilities, _start_fake_ollama
from chatp2p.client import CoordinatorClient
from chatp2p.cli import build_parser
from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.http_api import create_coordinator_http_server
from chatp2p.operator_config import DEFAULT_ALLOWED_JOB_TYPES, OperatorConfig
from chatp2p.packets import NodeRegistration
from chatp2p.worker import WorkerNode


def test_chat_ask_waits_for_funded_worker_answer(tmp_path):
    fake = _start_fake_ollama(
        model="tiny-test-model",
        answer="A contributor ran this answer through the mesh.",
    )
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    requester_id = "requester_chat_ask_account"
    coordinator.apply_credit_delta(
        account_id=requester_id,
        account_type="requester",
        delta=3,
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
            target=_run_worker_until_one_job,
            args=(client, worker, worker_errors),
            daemon=True,
        )
        worker_thread.start()
        report = run_chat_ask(
            ChatAskConfig(
                out_dir=tmp_path / "chat-ask",
                coordinator_url=base_url,
                model="tiny-test-model",
                prompt="Say hello.",
                requester_account_id=requester_id,
                job_cost=2,
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
    assert report["schema"] == CHAT_ASK_REPORT_SCHEMA
    assert report["status"] == "pass"
    assert report["summary"]["answer"] == "A contributor ran this answer through the mesh."
    assert report["summary"]["requester_balance_after"] == 1
    assert report["summary"]["worker_balance_after"] == 1
    assert report["summary"]["recommended_next_action"] == "continue_chat_session"
    assert report["job"]["job_type"] == "inference.chat.v1"
    assert report["result"]["output"]["interface"] == "chat"
    assert (tmp_path / "chat-ask" / "chat-ask.json").exists()
    assert (tmp_path / "chat-ask" / "chat-ask.md").exists()


def test_chat_ask_invite_auth_does_not_leak_token(tmp_path):
    token = "alpha-token-for-chat-ask-123456"
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    coordinator.apply_credit_delta(
        account_id="requester_invite_account",
        account_type="requester",
        delta=2,
        reason="operator_credit_grant",
    )
    server = create_coordinator_http_server(
        coordinator,
        host="127.0.0.1",
        port=0,
        operator_config=OperatorConfig(public_alpha=True, admission_token=token),
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        host, port = server.server_address
        invite_path = tmp_path / "alpha-invite.json"
        write_alpha_invite(
            invite_path,
            AlphaInvite.create(coordinator=f"http://{host}:{port}", admission_token=token),
        )
        report = run_chat_ask(
            ChatAskConfig(
                out_dir=tmp_path / "chat-ask",
                invite_path=invite_path,
                model="tiny-test-model",
                prompt="Submit only.",
                requester_account_id="requester_invite_account",
                job_cost=1,
                no_wait=True,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    assert report["status"] == "submitted"
    assert report["config"]["auth"]["token_present"] is True
    assert token not in json.dumps(report)


def test_default_public_alpha_allows_chat_jobs():
    assert "inference.chat.v1" in DEFAULT_ALLOWED_JOB_TYPES
    assert "inference.chat.v1" in OperatorConfig.default().public_summary()["allowed_job_types"]


def test_chat_ask_cli_parses():
    parser = build_parser()

    args = parser.parse_args(
        [
            "chat",
            "ask",
            "--invite",
            "D:\\ChatP2PData\\alpha-invite.json",
            "--out",
            "D:\\ChatP2PData\\chat-ask",
            "--model",
            "tiny-test-model",
            "--prompt",
            "Explain ChatP2P",
            "--requester-account-id",
            "requester_demo",
            "--job-cost",
            "2",
            "--json",
        ]
    )

    assert args.func.__name__ == "run_chat_ask_command"
    assert args.chat_command == "ask"
    assert args.invite == "D:\\ChatP2PData\\alpha-invite.json"
    assert args.requester_account_id == "requester_demo"
    assert args.job_cost == 2


def _run_worker_until_one_job(client, worker, errors):
    deadline = time.time() + 5
    while time.time() <= deadline:
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
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            return
    errors.append("worker did not receive a job")
