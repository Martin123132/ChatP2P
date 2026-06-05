import json
import threading
import time

from chatp2p.chat_session import (
    CHAT_SESSION_REPORT_SCHEMA,
    CHAT_SESSION_RESUME_REPORT_SCHEMA,
    CHAT_SESSION_STATUS_REPORT_SCHEMA,
    CHAT_SESSION_SYNC_REPORT_SCHEMA,
    ChatSessionConfig,
    ChatSessionResumeConfig,
    ChatSessionStatusConfig,
    ChatSessionSyncConfig,
    run_chat_session,
    run_chat_session_resume,
    run_chat_session_status,
    run_chat_session_sync,
)
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


def test_chat_session_status_flags_failed_turns(tmp_path):
    session_path = _write_session_fixture(tmp_path / "chat-session")

    report = run_chat_session_status(
        ChatSessionStatusConfig(
            out_dir=session_path.parent,
            session_id="demo",
        )
    )

    assert report["schema"] == CHAT_SESSION_STATUS_REPORT_SCHEMA
    assert report["status"] == "warn"
    assert report["summary"]["turn_count"] == 2
    assert report["summary"]["failed_turns"] == 1
    assert report["summary"]["recommended_next_action"] == "resume_failed_turn"
    assert report["turns"][1]["artifact_status"] == "missing"
    assert (session_path.parent / "chat-session-status.json").exists()
    assert "alpha-token" not in json.dumps(report)


def test_chat_session_resume_dry_run_does_not_mutate_session(tmp_path):
    session_path = _write_session_fixture(tmp_path / "chat-session")
    before = json.loads(session_path.read_text(encoding="utf-8"))

    report = run_chat_session_resume(
        ChatSessionResumeConfig(
            out_dir=session_path.parent,
            session_id="demo",
            dry_run=True,
        )
    )
    after = json.loads(session_path.read_text(encoding="utf-8"))

    assert report["schema"] == CHAT_SESSION_RESUME_REPORT_SCHEMA
    assert report["status"] == "dry_run"
    assert report["target_turn"]["turn_id"] == "turn-0002"
    assert report["retry"]["created"] is False
    assert report["summary"]["recommended_next_action"] == "rerun_session_resume_without_dry_run"
    assert after == before


def test_chat_session_sync_updates_failed_turn_from_verified_snapshot(tmp_path):
    fake = _start_fake_ollama(model="tiny-test-model", answer="Recovered answer.")
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    requester_id = "requester_session_sync"
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
        job = client.create_chat_job(
            model="tiny-test-model",
            messages=[{"role": "user", "content": "Recover this failed turn"}],
            requester_account_id=requester_id,
            job_cost=1,
            reward=1,
        )
        session_path = _write_session_fixture(
            tmp_path / "chat-session",
            requester_id=requester_id,
            failed_job_id=job.job_id,
        )

        worker_thread = threading.Thread(
            target=_run_worker_until_jobs,
            args=(client, worker, worker_errors, 1),
            daemon=True,
        )
        worker_thread.start()
        worker_thread.join(timeout=2)

        report = run_chat_session_sync(
            ChatSessionSyncConfig(
                out_dir=session_path.parent,
                session_id="demo",
                coordinator_url=base_url,
            )
        )
    finally:
        fake.stop()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    session = json.loads(session_path.read_text(encoding="utf-8"))
    assert worker_errors == []
    assert report["schema"] == CHAT_SESSION_SYNC_REPORT_SCHEMA
    assert report["status"] == "pass"
    assert report["summary"]["updated_turns"] == 1
    target_update = next(update for update in report["updates"] if update["turn_id"] == "turn-0002")
    assert target_update["previous_status"] == "fail"
    assert target_update["synced_status"] == "pass"
    assert session["turns"][1]["status"] == "pass"
    assert session["turns"][1]["answer"] == "Recovered answer."
    assert session["summary"]["recommended_next_action"] == "continue_chat_session"
    assert len(fake.requests) == 1


def test_chat_session_sync_dry_run_does_not_mutate_session(tmp_path):
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    requester_id = "requester_session_sync_dry_run"
    coordinator.apply_credit_delta(
        account_id=requester_id,
        account_type="requester",
        delta=2,
        reason="operator_credit_grant",
    )
    server = create_coordinator_http_server(coordinator, host="127.0.0.1", port=0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        client = CoordinatorClient(base_url)
        job = client.create_chat_job(
            model="tiny-test-model",
            messages=[{"role": "user", "content": "Queued turn"}],
            requester_account_id=requester_id,
            job_cost=1,
            reward=1,
        )
        session_path = _write_session_fixture(
            tmp_path / "chat-session",
            requester_id=requester_id,
            failed_job_id=job.job_id,
        )
        before = json.loads(session_path.read_text(encoding="utf-8"))

        report = run_chat_session_sync(
            ChatSessionSyncConfig(
                out_dir=session_path.parent,
                session_id="demo",
                coordinator_url=base_url,
                dry_run=True,
            )
        )
        after = json.loads(session_path.read_text(encoding="utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    assert report["status"] == "dry_run"
    assert report["summary"]["updated_turns"] == 1
    target_update = next(update for update in report["updates"] if update["turn_id"] == "turn-0002")
    assert target_update["synced_status"] == "submitted"
    assert report["summary"]["recommended_next_action"] == "rerun_session_sync_without_dry_run"
    assert after == before


def test_chat_session_resume_appends_retry_turn(tmp_path):
    fake = _start_fake_ollama(model="tiny-test-model", answer="Retried answer.")
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    requester_id = "requester_session_resume"
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
        session_path = _write_session_fixture(tmp_path / "chat-session", requester_id=requester_id)
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
        report = run_chat_session_resume(
            ChatSessionResumeConfig(
                out_dir=session_path.parent,
                session_id="demo",
                coordinator_url=base_url,
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

    session = json.loads(session_path.read_text(encoding="utf-8"))
    assert worker_errors == []
    assert report["status"] == "pass"
    assert report["target_turn"]["turn_id"] == "turn-0002"
    assert report["retry"]["created"] is True
    assert report["retry"]["turn_id"] == "turn-0003"
    assert report["summary"]["recommended_next_action"] == "continue_chat_session"
    assert len(session["turns"]) == 3
    assert session["turns"][2]["retry_of_turn_id"] == "turn-0002"
    assert session["turns"][2]["retry_attempt"] == 1
    assert session["turns"][2]["answer"] == "Retried answer."
    assert "USER: First question?" in fake.requests[0]["prompt"]
    assert "ASSISTANT: Existing answer." in fake.requests[0]["prompt"]
    assert "USER: Failed question?" in fake.requests[0]["prompt"]


def test_chat_session_status_and_resume_cli_parse():
    parser = build_parser()

    status_args = parser.parse_args(
        [
            "chat",
            "session-status",
            "--out",
            "D:\\ChatP2PData\\chat-session",
            "--session-id",
            "demo",
            "--json",
        ]
    )
    sync_args = parser.parse_args(
        [
            "chat",
            "session-sync",
            "--out",
            "D:\\ChatP2PData\\chat-session",
            "--session-id",
            "demo",
            "--dry-run",
            "--json",
        ]
    )
    resume_args = parser.parse_args(
        [
            "chat",
            "session-resume",
            "--out",
            "D:\\ChatP2PData\\chat-session",
            "--session-id",
            "demo",
            "--include-submitted",
            "--dry-run",
            "--json",
        ]
    )

    assert status_args.func.__name__ == "run_chat_session_status_command"
    assert status_args.chat_command == "session-status"
    assert sync_args.func.__name__ == "run_chat_session_sync_command"
    assert sync_args.chat_command == "session-sync"
    assert sync_args.dry_run is True
    assert resume_args.func.__name__ == "run_chat_session_resume_command"
    assert resume_args.include_submitted is True
    assert resume_args.dry_run is True


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


def _write_session_fixture(path, requester_id="requester_session_account", failed_job_id=None):
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
            "requester_account_id": requester_id,
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
                "job_id": failed_job_id,
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
        "artifacts": {},
    }
    session_path = path / "chat-session.json"
    session_path.write_text(json.dumps(session, indent=2, sort_keys=True), encoding="utf-8")
    return session_path
