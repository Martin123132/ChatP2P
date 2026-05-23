import sys
import threading

from chatp2p.crypto import NodeIdentity
from chatp2p.gremlinchat.approvals import decide_approval, load_approvals
from chatp2p.gremlinchat.config import RunbookPolicy, load_or_create_identity, load_or_create_x25519_identity, save_policy, save_room
from chatp2p.gremlinchat.daemon import _snapshot
from chatp2p.gremlinchat.pairing import create_invite_code, parse_invite_code
from chatp2p.gremlinchat.relay import RelayClient, create_relay_http_server
from chatp2p.gremlinchat.reports import write_task_report
from chatp2p.gremlinchat.security import derive_room_key, seal_message
from chatp2p.gremlinchat.tasks import create_pair_hello, create_task_request
from chatp2p.gremlinchat_cli import _process_room_once


def _start_relay():
    server = create_relay_http_server(host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, RelayClient(f"http://{host}:{port}")


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_report_writer_creates_redacted_json_and_markdown(tmp_path):
    paths = write_task_report(
        tmp_path,
        {
            "task_id": "task_test",
            "runbook": "presence.ping",
            "result": {
                "accepted": True,
                "status": "completed",
                "summary": "done",
                "output": {"token": "sk-secretsecretsecret"},
            },
        },
    )

    json_text = (tmp_path / "reports" / "task_test.json").read_text(encoding="utf-8")
    md_text = (tmp_path / "reports" / "task_test.md").read_text(encoding="utf-8")
    assert paths["json"].endswith("task_test.json")
    assert "[redacted]" in json_text
    assert "GremlinChat Task Report" in md_text


def test_dashboard_snapshot_does_not_expose_room_secrets(tmp_path):
    save_room(
        {
            "room_id": "room_test",
            "relay_url": "http://relay",
            "relay_token": "relay-secret",
            "pair_secret": "pair-secret",
        },
        tmp_path,
    )

    snapshot = _snapshot(tmp_path)
    rendered = str(snapshot)
    assert "relay-secret" not in rendered
    assert "pair-secret" not in rendered
    assert snapshot["rooms"][0]["room_id"] == "room_test"


def test_room_process_queues_owner_approval_then_executes_after_approval(tmp_path):
    server, thread, relay_client = _start_relay()
    try:
        alice_home = tmp_path / "alice"
        bob_home = tmp_path / "bob"
        alice = load_or_create_identity(alice_home)
        bob = load_or_create_identity(bob_home)
        alice_x = load_or_create_x25519_identity(alice_home)
        bob_x = load_or_create_x25519_identity(bob_home)
        relay_room = relay_client.create_room(ttl_seconds=60)
        invite = parse_invite_code(
            create_invite_code(
                creator=alice,
                relay_url=relay_client.base_url,
                room_id=relay_room["room_id"],
                relay_token=relay_room["relay_token"],
                creator_x25519_public_key=alice_x.public_key,
                ttl_seconds=60,
            )
        )

        alice_room = {
            "room_id": invite.room_id,
            "relay_url": invite.relay_url,
            "relay_token": invite.relay_token,
            "pair_secret": invite.pair_secret,
            "local_x25519_public_key": alice_x.public_key,
            "peer_node_id": bob.node_id,
            "peer_public_key": bob.public_key,
            "peer_x25519_public_key": bob_x.public_key,
            "processed_message_ids": [],
        }
        bob_room = {
            "room_id": invite.room_id,
            "relay_url": invite.relay_url,
            "relay_token": invite.relay_token,
            "pair_secret": invite.pair_secret,
            "local_x25519_public_key": bob_x.public_key,
            "peer_node_id": alice.node_id,
            "peer_public_key": alice.public_key,
            "peer_x25519_public_key": alice_x.public_key,
            "processed_message_ids": [],
        }
        save_room(alice_room, alice_home)
        save_room(bob_room, bob_home)
        relay_client.post_envelope(
            room_id=invite.room_id,
            relay_token=invite.relay_token,
            envelope=create_pair_hello(room_id=invite.room_id, sender=bob, x25519_public_key=bob_x.public_key),
        )

        save_policy(
            RunbookPolicy(managed_workers={"safe": [sys.executable, "-c", "print('safe restart')"]}),
            bob_home,
        )
        room_key = derive_room_key(
            local_private_key=alice_x.private_key,
            peer_public_key=bob_x.public_key,
            pair_secret=invite.pair_secret,
            participant_public_keys=[alice.public_key, bob.public_key],
        )
        request = create_task_request(runbook="worker.restart_named", payload={"worker_name": "safe"})
        request_envelope = seal_message(room_id=invite.room_id, sender=alice, room_key=room_key, message=request)
        relay_client.post_envelope(
            room_id=invite.room_id,
            relay_token=invite.relay_token,
            envelope=request_envelope.to_dict(),
        )

        pending = _process_room_once(bob_home, invite.room_id)
        approvals = load_approvals(bob_home)
        assert pending["processed"][0]["status"] == "pending_approval"
        assert approvals[0]["status"] == "pending"
        assert not (bob_home / "reports" / f"{request['task_id']}.json").exists()

        decide_approval(bob_home, approvals[0]["approval_id"], approved=True)
        completed = _process_room_once(bob_home, invite.room_id)
        approvals = load_approvals(bob_home)
        assert completed["processed"][0]["runbook"] == "worker.restart_named"
        assert approvals[0]["status"] == "consumed"
        assert (bob_home / "reports" / f"{request['task_id']}.json").exists()
    finally:
        _stop(server, thread)
