import json
import threading
from urllib.error import HTTPError

from chatp2p.alpha import AlphaInvite, write_alpha_invite
from chatp2p.cli import build_parser
from chatp2p.client import CoordinatorClient
from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.http_api import create_coordinator_http_server
from chatp2p.operator_config import OperatorConfig, write_operator_config
from chatp2p.operator_credits import (
    OPERATOR_CREDITS_REPORT_SCHEMA,
    OPERATOR_GRANT_REQUESTER_CREDITS_REPORT_SCHEMA,
    OperatorCreditsConfig,
    OperatorGrantRequesterCreditsConfig,
    run_operator_credits,
    run_operator_grant_requester_credits,
)


def test_operator_grant_requires_separate_credit_grant_token():
    admission_token = "alpha-token-123456"
    grant_token = "credit-grant-token-1234567890"
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    server = create_coordinator_http_server(
        coordinator,
        host="127.0.0.1",
        port=0,
        operator_config=OperatorConfig(
            public_alpha=True,
            admission_token=admission_token,
            credit_grant_token=grant_token,
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        try:
            CoordinatorClient(base_url, admission_token=admission_token).grant_requester_credits(
                credit_grant_token=admission_token,
                account_id="requester_demo",
                credits=5,
            )
        except HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("Expected admission token to be refused for credit grants")

        response = CoordinatorClient(base_url).grant_requester_credits(
            credit_grant_token=grant_token,
            account_id="requester_demo",
            credits=5,
        )
        health = CoordinatorClient(base_url).health()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response["ok"] is True
    assert response["balance"] == 5
    assert coordinator.credits["requester_demo"] == 5
    assert health["operator"]["credit_grant_token_required"] is True
    assert grant_token not in json.dumps(response)
    assert grant_token not in json.dumps(health)


def test_operator_grant_and_credits_reports_redact_tokens(tmp_path):
    admission_token = "alpha-token-" + "for-credit-report-123456"
    grant_token = "credit-grant-token-for-report-123456"
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    server = create_coordinator_http_server(
        coordinator,
        host="127.0.0.1",
        port=0,
        operator_config=OperatorConfig(
            public_alpha=True,
            admission_token=admission_token,
            credit_grant_token=grant_token,
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        invite_path = tmp_path / "alpha-invite.json"
        config_path = tmp_path / "operator-config.json"
        write_alpha_invite(
            invite_path,
            AlphaInvite.create(coordinator=f"http://{host}:{port}", admission_token=admission_token),
        )
        write_operator_config(
            config_path,
            OperatorConfig(
                public_alpha=True,
                admission_token=admission_token,
                credit_grant_token=grant_token,
            ),
        )

        grant_report = run_operator_grant_requester_credits(
            OperatorGrantRequesterCreditsConfig(
                out_dir=tmp_path / "grant",
                invite_path=invite_path,
                operator_config_path=config_path,
                requester_account_id="requester_demo",
                credits=7,
            )
        )
        credits_report = run_operator_credits(
            OperatorCreditsConfig(
                out_dir=tmp_path / "credits",
                invite_path=invite_path,
                requester_account_id="requester_demo",
                min_requester_balance=1,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert grant_report["schema"] == OPERATOR_GRANT_REQUESTER_CREDITS_REPORT_SCHEMA
    assert grant_report["status"] == "pass"
    assert grant_report["summary"]["balance_after"] == 7
    assert grant_report["summary"]["recommended_next_action"] == "run_chat_ask"
    assert credits_report["schema"] == OPERATOR_CREDITS_REPORT_SCHEMA
    assert credits_report["status"] == "pass"
    assert credits_report["summary"]["selected_requester"]["balance"] == 7
    assert credits_report["summary"]["recommended_next_action"] == "continue_chat_ask"
    assert (tmp_path / "grant" / "grant-requester-credits.json").exists()
    assert (tmp_path / "credits" / "operator-credits.md").exists()
    combined = json.dumps({"grant": grant_report, "credits": credits_report})
    assert admission_token not in combined
    assert grant_token not in combined


def test_operator_grant_dry_run_does_not_require_coordinator(tmp_path):
    grant_token = "credit-grant-token-dry-run-123456"
    report = run_operator_grant_requester_credits(
        OperatorGrantRequesterCreditsConfig(
            out_dir=tmp_path / "grant",
            coordinator_url="http://127.0.0.1:1",
            credit_grant_token=grant_token,
            requester_account_id="requester_demo",
            credits=3,
            dry_run=True,
        )
    )

    assert report["status"] == "dry_run"
    assert report["summary"]["recommended_next_action"] == "rerun_grant_without_dry_run"
    assert report["grant"] is None


def test_operator_credit_cli_parses():
    parser = build_parser()

    credits = parser.parse_args(
        [
            "operator",
            "credits",
            "--invite",
            "D:\\ChatP2PData\\alpha-invite.json",
            "--requester-account-id",
            "requester_demo",
            "--json",
        ]
    )
    grant = parser.parse_args(
        [
            "operator",
            "grant-requester-credits",
            "--invite",
            "D:\\ChatP2PData\\alpha-invite.json",
            "--operator-config",
            "D:\\ChatP2PData\\operator-config.json",
            "--requester-account-id",
            "requester_demo",
            "--credits",
            "10",
            "--dry-run",
        ]
    )

    assert credits.func.__name__ == "operator_credits_command"
    assert credits.requester_account_id == "requester_demo"
    assert grant.func.__name__ == "operator_grant_requester_credits_command"
    assert grant.credits == 10
    assert grant.dry_run is True
