import json
from urllib.request import Request, urlopen

import pytest

from chatp2p.chat_demo import CHAT_DEMO_RUNTIME_SCHEMA, ChatDemoConfig, create_chat_demo_runtime
from chatp2p.chat_smoke import _start_fake_ollama
from chatp2p.cli import build_parser


def test_chat_demo_runtime_starts_ready_and_completes_gateway_turn(tmp_path):
    runtime = create_chat_demo_runtime(
        ChatDemoConfig(
            out_dir=tmp_path / "chat-demo",
            port=0,
            starting_credits=3,
            timeout_seconds=10,
            poll_interval=0.1,
            client_timeout_seconds=2,
            fake_answer="Demo gateway answer.",
        )
    )
    runtime.start_gateway_thread()
    try:
        report = runtime.report()
        readiness = _get_json(f"{runtime.gateway_url}/api/chat/readiness")
        turn = _post_json(f"{runtime.gateway_url}/api/chat/continue", {"prompt": "Run the local demo"})
        transcript = _get_json(f"{runtime.gateway_url}/api/session/transcript")
        readiness_after = _get_json(f"{runtime.gateway_url}/api/chat/readiness")
    finally:
        runtime.close()

    serialized_report = json.dumps(report)
    assert report["schema"] == CHAT_DEMO_RUNTIME_SCHEMA
    assert report["status"] == "pass"
    assert runtime.worker.identity.node_id not in serialized_report
    assert readiness["status"] == "pass"
    assert readiness["summary"]["can_send"] is True
    assert readiness["summary"]["requester_balance"] == 3
    assert readiness["summary"]["live_eligible_node_count"] == 1
    assert turn["status"] == "pass"
    assert turn["summary"]["latest_turn"]["answer"] == "Demo gateway answer."
    assert transcript["summary"]["turn_count"] == 1
    assert transcript["turns"][0]["answer"] == "Demo gateway answer."
    assert readiness_after["summary"]["requester_balance"] == 2


def test_chat_demo_ollama_mode_uses_preflighted_runtime(tmp_path):
    fake = _start_fake_ollama(model="tiny-test-model", answer="Ollama mode answer.")
    runtime = None
    try:
        runtime = create_chat_demo_runtime(
            ChatDemoConfig(
                out_dir=tmp_path / "chat-demo",
                mode="ollama",
                ollama_base_url=fake.base_url,
                port=0,
                starting_credits=2,
                timeout_seconds=10,
                poll_interval=0.1,
                client_timeout_seconds=2,
            )
        )
        runtime.start_gateway_thread()
        readiness = _get_json(f"{runtime.gateway_url}/api/chat/readiness")
        turn = _post_json(f"{runtime.gateway_url}/api/chat/continue", {"prompt": "Use real mode"})
    finally:
        if runtime is not None:
            runtime.close()
        fake.stop()

    assert readiness["status"] == "pass"
    assert readiness["summary"]["live_eligible_node_count"] == 1
    assert turn["status"] == "pass"
    assert turn["summary"]["latest_turn"]["answer"] == "Ollama mode answer."
    assert fake.requests


def test_chat_demo_ollama_mode_fails_before_gateway_when_model_missing(tmp_path):
    fake = _start_fake_ollama(model="other-model", answer="Wrong model.")
    try:
        with pytest.raises(ValueError, match="not advertised"):
            create_chat_demo_runtime(
                ChatDemoConfig(
                    out_dir=tmp_path / "chat-demo",
                    mode="ollama",
                    model="tiny-test-model",
                    ollama_base_url=fake.base_url,
                    port=0,
                )
            )
    finally:
        fake.stop()


def test_chat_demo_parser_accepts_minimal_command():
    parser = build_parser()

    args = parser.parse_args(["chat", "demo", "--port", "8787"])

    assert args.func.__name__ == "run_chat_demo_command"
    assert args.chat_command == "demo"
    assert args.mode == "fake"
    assert args.model == "tiny-test-model"
    assert args.requester_account_id == "requester_demo"
    assert args.starting_credits == 10


def test_chat_demo_parser_accepts_ollama_mode():
    parser = build_parser()

    args = parser.parse_args(
        [
            "chat",
            "demo",
            "--mode",
            "ollama",
            "--model",
            "llama3.2:3b",
            "--ollama-base-url",
            "http://127.0.0.1:11434",
        ]
    )

    assert args.mode == "ollama"
    assert args.model == "llama3.2:3b"
    assert args.ollama_base_url == "http://127.0.0.1:11434"


def _get_json(url):
    with urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url, payload):
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))
