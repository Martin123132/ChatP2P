from chatp2p.crypto import NodeIdentity
from chatp2p.gremlinchat.config import RunbookPolicy
from chatp2p.gremlinchat.pairing import create_invite_code, parse_invite_code
from chatp2p.gremlinchat.relay import GremlinRelay
from chatp2p.gremlinchat.runbooks import execute_runbook
from chatp2p.gremlinchat.security import (
    ReplayGuard,
    X25519Identity,
    derive_room_key,
    open_message,
    seal_message,
)
from chatp2p.gremlinchat.tasks import create_pair_hello, create_task_request, create_task_result, verify_pair_hello


def test_encrypted_task_request_and_result_round_trip(tmp_path):
    relay = GremlinRelay()
    relay_room = relay.create_room(ttl_seconds=60)
    alice = NodeIdentity.generate(prefix="gremlin")
    bob = NodeIdentity.generate(prefix="gremlin")
    alice_x = X25519Identity.generate()
    bob_x = X25519Identity.generate()
    invite = parse_invite_code(
        create_invite_code(
            creator=alice,
            relay_url="http://relay.test",
            room_id=relay_room.room_id,
            relay_token=relay_room.token,
            creator_x25519_public_key=alice_x.public_key,
            ttl_seconds=60,
        )
    )

    hello = create_pair_hello(room_id=invite.room_id, sender=bob, x25519_public_key=bob_x.public_key)
    assert verify_pair_hello(hello) is True
    relay.append_envelope(invite.room_id, invite.relay_token, hello)

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

    request = create_task_request(runbook="presence.ping", payload={})
    request_envelope = seal_message(room_id=invite.room_id, sender=alice, room_key=alice_key, message=request)
    relay.append_envelope(invite.room_id, invite.relay_token, request_envelope.to_dict())

    opened_request = open_message(
        envelope=type(request_envelope).from_dict(relay.messages_after(invite.room_id, invite.relay_token)[
            "messages"
        ][1]["envelope"]),
        room_key=bob_key,
        replay_guard=ReplayGuard(),
    )
    result = execute_runbook("presence.ping", opened_request["payload"], policy=RunbookPolicy(), home=tmp_path)
    result_message = create_task_result(request=opened_request, result=result.to_dict())
    result_envelope = seal_message(room_id=invite.room_id, sender=bob, room_key=bob_key, message=result_message)
    relay.append_envelope(invite.room_id, invite.relay_token, result_envelope.to_dict())

    opened_result = open_message(envelope=result_envelope, room_key=alice_key, replay_guard=ReplayGuard())
    assert opened_result["type"] == "task.result.v1"
    assert opened_result["task_id"] == request["task_id"]
    assert opened_result["result"]["accepted"] is True
    assert opened_result["result"]["runbook"] == "presence.ping"

