"""Node identity and Ed25519 signing primitives."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


@dataclass(frozen=True)
class NodeIdentity:
    """A node's long-lived signing identity."""

    node_id: str
    public_key: str
    private_key: str | None = None

    @classmethod
    def generate(cls, prefix: str = "node") -> "NodeIdentity":
        private = Ed25519PrivateKey.generate()
        public = private.public_key()
        public_bytes = public.public_bytes(Encoding.Raw, PublicFormat.Raw)
        private_bytes = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        fingerprint = hashlib.sha256(public_bytes).hexdigest()[:16]
        return cls(
            node_id=f"{prefix}_{fingerprint}",
            public_key=_b64encode(public_bytes),
            private_key=_b64encode(private_bytes),
        )

    @classmethod
    def load(cls, path: Path) -> "NodeIdentity":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            node_id=data["node_id"],
            public_key=data["public_key"],
            private_key=data.get("private_key"),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "node_id": self.node_id,
            "public_key": self.public_key,
            "private_key": self.private_key,
        }
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def public(self) -> "NodeIdentity":
        return NodeIdentity(node_id=self.node_id, public_key=self.public_key)

    def sign(self, payload: bytes) -> str:
        if self.private_key is None:
            raise ValueError("Private key is required for signing")
        private = Ed25519PrivateKey.from_private_bytes(_b64decode(self.private_key))
        return _b64encode(private.sign(payload))

    def verify(self, payload: bytes, signature: str) -> bool:
        public = Ed25519PublicKey.from_public_bytes(_b64decode(self.public_key))
        try:
            public.verify(_b64decode(signature), payload)
            return True
        except InvalidSignature:
            return False
