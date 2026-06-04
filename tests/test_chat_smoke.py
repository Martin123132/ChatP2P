import json

from chatp2p.chat_smoke import (
    FUNDED_CHAT_SMOKE_REPORT_SCHEMA,
    FundedChatSmokeConfig,
    run_funded_chat_smoke,
)
from chatp2p.cli import build_parser


def test_funded_chat_smoke_runs_credit_backed_product_loop(tmp_path):
    report = run_funded_chat_smoke(
        FundedChatSmokeConfig(
            out_dir=tmp_path / "chat-smoke",
            prompt="Say hello to the mesh.",
            fake_answer="Hello from contributed compute.",
            requester_account_id="requester_demo_account",
            starting_credits=3,
            job_cost=2,
            reward=1,
        )
    )

    assert report["schema"] == FUNDED_CHAT_SMOKE_REPORT_SCHEMA
    assert report["status"] == "pass"
    assert report["job"]["status"] == "verified"
    assert report["result"]["output"]["answer"] == "Hello from contributed compute."
    assert report["summary"]["requester_balance_after"] == 1
    assert report["summary"]["worker_balance_after"] == 1
    assert report["summary"]["recommended_next_action"] == "continue_to_chat_ui_mvp"
    assert report["fake_ollama"]["request_count"] == 1
    assert report["fake_ollama"]["requests"][0]["prompt_preview"] == (
        "SYSTEM: Be concise. USER: Say hello to the mesh. ASSISTANT:"
    )

    ledger = report["ledger"]
    assert [entry["reason"] for entry in ledger["recent_entries"]] == [
        "operator_credit_grant",
        "job_cost_reserved",
        "worker_result_reward",
    ]
    assert ledger["summary"]["negative_credits"] == 2
    assert ledger["summary"]["positive_credits"] == 4

    json_path = tmp_path / "chat-smoke" / "funded-chat-smoke.json"
    markdown_path = tmp_path / "chat-smoke" / "funded-chat-smoke.md"
    assert json_path.exists()
    assert markdown_path.exists()
    written = json.loads(json_path.read_text(encoding="utf-8"))
    assert written["schema"] == FUNDED_CHAT_SMOKE_REPORT_SCHEMA


def test_funded_chat_smoke_fails_before_lease_when_requester_lacks_credits(tmp_path):
    report = run_funded_chat_smoke(
        FundedChatSmokeConfig(
            out_dir=tmp_path / "chat-smoke",
            requester_account_id="requester_low_credit",
            starting_credits=1,
            job_cost=2,
        )
    )

    assert report["status"] == "fail"
    assert report["job"] is None
    assert report["result"] is None
    assert report["summary"]["recommended_next_action"] == "increase_requester_credits"
    assert report["ledger"]["summary"]["balances"]["requester_low_credit"] == 1
    assert [entry["reason"] for entry in report["ledger"]["recent_entries"]] == [
        "operator_credit_grant",
    ]
    assert "negative" in report["errors"][0].lower()


def test_chat_smoke_report_does_not_contain_secret_material(tmp_path):
    report = run_funded_chat_smoke(
        FundedChatSmokeConfig(
            out_dir=tmp_path / "chat-smoke",
            prompt="Privacy check.",
        )
    )

    serialized = json.dumps(report).lower()
    assert "admission_token" not in serialized
    assert "private_key" not in serialized
    assert "tailscale auth" not in serialized


def test_chat_smoke_cli_parses():
    parser = build_parser()

    args = parser.parse_args(
        [
            "chat",
            "smoke",
            "--out",
            "D:\\ChatP2PData\\chat-smoke",
            "--model",
            "tiny-test-model",
            "--prompt",
            "Explain ChatP2P",
            "--requester-account-id",
            "requester_demo",
            "--starting-credits",
            "5",
            "--job-cost",
            "2",
            "--reward",
            "1",
            "--json",
        ]
    )

    assert args.func.__name__ == "run_chat_smoke_command"
    assert args.chat_command == "smoke"
    assert args.model == "tiny-test-model"
    assert args.starting_credits == 5
    assert args.job_cost == 2
