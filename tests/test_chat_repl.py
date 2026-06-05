import json
import threading
import time

from chatp2p.chat_repl import CHAT_REPL_REPORT_SCHEMA, ChatReplConfig, run_chat_repl
from chatp2p.chat_session import CHAT_SESSION_REPORT_SCHEMA
from chatp2p.chat_smoke import _ollama_capabilities, _start_fake_ollama
from chatp2p.client import CoordinatorClient
from chatp2p.cli import build_parser
from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.http_api import create_coordinator_http_server
from chatp2p.packets import NodeRegistration
from chatp2p.worker import WorkerNode


def test_chat_repl_submits_message_and_status_command(tmp_path):
    fake = _start_fake_ollama(model="tiny-test-model", answer="REPL answer.")
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    requester_id = "requester_repl_account"
    coordinator.apply_credit_delta(
        account_id=requester_id,
        account_type="requester",
        delta=2,
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
            args=(client, worker, worker_errors, 1),
            daemon=True,
        )
        worker_thread.start()

        output = []
        inputs = iter(["Hello from REPL", "/status", "/quit"])
        report = run_chat_repl(
            ChatReplConfig(
                out_dir=tmp_path / "chat-session",
                session_id="demo",
                coordinator_url=base_url,
                model="tiny-test-model",
                requester_account_id=requester_id,
                timeout_seconds=10,
                poll_interval=0.1,
            ),
            input_func=lambda _prompt: next(inputs),
            output_func=output.append,
        )
        worker_thread.join(timeout=2)
    finally:
        fake.stop()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    assert worker_errors == []
    assert report["schema"] == CHAT_REPL_REPORT_SCHEMA
    assert report["status"] == "pass"
    assert report["summary"]["messages"] == 1
    assert report["summary"]["commands"] == 1
    assert report["summary"]["exit_reason"] == "user_exit"
    assert any(line == "assistant> REPL answer." for line in output)
    assert (tmp_path / "chat-session" / "chat-repl.json").exists()
    assert (tmp_path / "chat-session" / "chat-repl.md").exists()
    assert "alpha-token" not in json.dumps(report)


def test_chat_repl_blocks_unresolved_turn_without_spend(tmp_path):
    session_path = _write_failed_session_fixture(tmp_path / "chat-session")
    before = json.loads(session_path.read_text(encoding="utf-8"))
    output = []
    inputs = iter(["Spend again", "/quit"])

    report = run_chat_repl(
        ChatReplConfig(
            out_dir=session_path.parent,
            session_id="demo",
            coordinator_url="http://127.0.0.1:9",
            model="tiny-test-model",
            requester_account_id="requester_repl_account",
            client_timeout_seconds=0.1,
        ),
        input_func=lambda _prompt: next(inputs),
        output_func=output.append,
    )
    after = json.loads(session_path.read_text(encoding="utf-8"))

    assert report["status"] == "warn"
    assert report["summary"]["blocked_messages"] == 1
    assert report["events"][0]["status"] == "blocked"
    assert report["events"][0]["summary"]["blocked_reason"] == "unresolved_session_turns"
    assert after == before
    assert any(line.startswith("status> blocked") for line in output)


def test_chat_repl_parser_accepts_safe_loop_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "chat",
            "repl",
            "--out",
            "D:\\ChatP2PData\\chat-session",
            "--session-id",
            "demo",
            "--model",
            "tiny-test-model",
            "--requester-account-id",
            "requester_demo",
            "--max-context-turns",
            "4",
            "--json",
        ]
    )

    assert args.func.__name__ == "run_chat_repl_command"
    assert args.chat_command == "repl"
    assert args.max_context_turns == 4
    assert args.json is True


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
            "requester_account_id": "requester_repl_account",
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
