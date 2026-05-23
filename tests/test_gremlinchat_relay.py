import threading

from chatp2p.crypto import NodeIdentity
from chatp2p.gremlinchat.relay import RelayClient, create_relay_http_server


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


def _envelope(sender):
    return {
        "protocol": "gremlinchat.envelope.v1",
        "room_id": "filled-by-test",
        "message_id": f"msg_{sender.node_id}",
        "created_at": 1.0,
        "sender_node_id": sender.node_id,
        "sender_public_key": sender.public_key,
        "nonce": "opaque",
        "ciphertext": "opaque-ciphertext",
        "signature": "opaque",
    }


def test_relay_locks_room_after_two_participants_and_stores_only_envelopes():
    server, thread, client = _start_relay()
    try:
        room = client.create_room(ttl_seconds=60)
        alice = NodeIdentity.generate(prefix="gremlin")
        bob = NodeIdentity.generate(prefix="gremlin")
        eve = NodeIdentity.generate(prefix="gremlin")

        first = _envelope(alice)
        first["room_id"] = room["room_id"]
        assert client.post_envelope(
            room_id=room["room_id"],
            relay_token=room["relay_token"],
            envelope=first,
        )["accepted"] is True

        second = _envelope(bob)
        second["room_id"] = room["room_id"]
        assert client.post_envelope(
            room_id=room["room_id"],
            relay_token=room["relay_token"],
            envelope=second,
        )["locked"] is True

        third = _envelope(eve)
        third["room_id"] = room["room_id"]
        rejected = client.post_envelope(
            room_id=room["room_id"],
            relay_token=room["relay_token"],
            envelope=third,
        )
        assert rejected["http_status"] == 403
        assert "locked" in rejected["error"]

        messages = client.messages_after(room_id=room["room_id"], relay_token=room["relay_token"])
        assert messages["participants"] == sorted([alice.node_id, bob.node_id])
        assert len(messages["messages"]) == 2
        assert messages["messages"][0]["envelope"]["ciphertext"] == "opaque-ciphertext"
    finally:
        _stop(server, thread)


def test_relay_rejects_expired_or_wrong_token_rooms():
    server, thread, client = _start_relay()
    try:
        room = client.create_room(ttl_seconds=-1)
        sender = NodeIdentity.generate(prefix="gremlin")
        envelope = _envelope(sender)
        envelope["room_id"] = room["room_id"]

        expired = client.post_envelope(
            room_id=room["room_id"],
            relay_token=room["relay_token"],
            envelope=envelope,
        )
        assert expired["http_status"] == 403
        assert "expired" in expired["error"]

        room = client.create_room(ttl_seconds=60)
        wrong = client.messages_after(room_id=room["room_id"], relay_token="wrong")
        assert wrong["http_status"] == 403
    finally:
        _stop(server, thread)

