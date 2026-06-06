import json
from urllib.request import Request, urlopen

from chatp2p.chat_demo import CHAT_DEMO_RUNTIME_SCHEMA, ChatDemoConfig, create_chat_demo_runtime
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


def test_chat_demo_parser_accepts_minimal_command():
    parser = build_parser()

    args = parser.parse_args(["chat", "demo", "--port", "8787"])

    assert args.func.__name__ == "run_chat_demo_command"
    assert args.chat_command == "demo"
    assert args.model == "tiny-test-model"
    assert args.requester_account_id == "requester_demo"
    assert args.starting_credits == 10


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
