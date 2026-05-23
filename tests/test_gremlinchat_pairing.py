import json

import pytest

from chatp2p.crypto import NodeIdentity
from chatp2p.gremlinchat.pairing import create_invite_code, parse_invite_code
from chatp2p.gremlinchat.security import (
    ReplayGuard,
    X25519Identity,
    b64decode,
    b64encode,
    derive_room_key,
    open_message,
    safety_phrase,
    seal_message,
)


def test_invite_codes_validate_checksum_and_expiry():
    creator = NodeIdentity.generate(prefix="gremlin")
    code = create_invite_code(creator=creator, relay_url="http://127.0.0.1:8778", ttl_seconds=60)
    invite = parse_invite_code(code)

    assert invite.relay_url == "http://127.0.0.1:8778"
    assert invite.creator_node_id == creator.node_id
    assert invite.creator_public_key == creator.public_key
    assert invite.room_id.startswith("room_")

    payload = json.loads(b64decode(code.removeprefix("GC1:")).decode("utf-8"))
    payload["relay_url"] = "http://127.0.0.1:9999"
    tampered = "GC1:" + b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    with pytest.raises(ValueError, match="checksum"):
        parse_invite_code(tampered)

    expired = create_invite_code(creator=creator, relay_url="http://127.0.0.1:8778", ttl_seconds=-1)
    with pytest.raises(ValueError, match="expired"):
        parse_invite_code(expired)


def test_room_key_encrypted_envelope_and_replay_guard():
    alice = NodeIdentity.generate(prefix="gremlin")
    bob = NodeIdentity.generate(prefix="gremlin")
    invite = parse_invite_code(
        create_invite_code(creator=alice, relay_url="http://127.0.0.1:8778", ttl_seconds=60)
    )
    alice_x = X25519Identity.generate()
    bob_x = X25519Identity.generate()
    participants = [alice.public_key, bob.public_key]

    alice_key = derive_room_key(
        local_private_key=alice_x.private_key,
        peer_public_key=bob_x.public_key,
        pair_secret=invite.pair_secret,
        participant_public_keys=participants,
    )
    bob_key = derive_room_key(
        local_private_key=bob_x.private_key,
        peer_public_key=alice_x.public_key,
        pair_secret=invite.pair_secret,
        participant_public_keys=participants,
    )

    assert alice_key == bob_key
    assert safety_phrase(invite.pair_secret, participants) == safety_phrase(invite.pair_secret, list(reversed(participants)))

    message = {
        "type": "task.request.v1",
        "runbook": "repo.status",
        "payload": {"repo_path": "C:/private/repo"},
    }
    envelope = seal_message(room_id=invite.room_id, sender=alice, room_key=alice_key, message=message)
    relay_visible = json.dumps(envelope.to_dict())
    assert "repo.status" not in relay_visible
    assert "C:/private/repo" not in relay_visible

    guard = ReplayGuard()
    assert open_message(envelope=envelope, room_key=bob_key, replay_guard=guard) == message
    with pytest.raises(ValueError, match="replayed"):
        open_message(envelope=envelope, room_key=bob_key, replay_guard=guard)

    tampered = envelope.to_dict()
    tampered["ciphertext"] = tampered["ciphertext"][:-2] + "xx"
    with pytest.raises(ValueError, match="signature"):
        open_message(envelope=type(envelope).from_dict(tampered), room_key=bob_key, replay_guard=ReplayGuard())

    wrong_key = derive_room_key(
        local_private_key=X25519Identity.generate().private_key,
        peer_public_key=bob_x.public_key,
        pair_secret=invite.pair_secret,
        participant_public_keys=participants,
    )
    with pytest.raises(ValueError, match="decryption"):
        open_message(envelope=envelope, room_key=wrong_key, replay_guard=ReplayGuard())
