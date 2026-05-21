"""SQLite-backed coordinator state."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .crypto import NodeIdentity
from .packets import JobLeaseAcknowledgement, JobLeaseGrant, JobPacket, JobResult, NodeRegistration


class SQLiteCoordinatorStore:
    """Small durable store for coordinator state.

    This is intentionally boring: one SQLite file, JSON payload columns for signed
    packets, and simple indexes. The signatures remain the source of truth.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    node_id TEXT PRIMARY KEY,
                    public_key TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    packet_json TEXT NOT NULL,
                    leased_to TEXT,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS leases (
                    job_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    leased_at REAL,
                    expires_at REAL,
                    expired_at REAL,
                    lease_id TEXT,
                    request_id TEXT,
                    grant_json TEXT,
                    grant_hash TEXT,
                    acknowledged_at REAL,
                    acknowledgement_json TEXT,
                    PRIMARY KEY (job_id, node_id)
                );

                CREATE TABLE IF NOT EXISTS results (
                    job_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (job_id, node_id)
                );

                CREATE TABLE IF NOT EXISTS credits (
                    node_id TEXT PRIMARY KEY,
                    credits INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS seen_packets (
                    packet_id TEXT PRIMARY KEY,
                    packet_type TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    seen_at REAL NOT NULL
                );
                """
            )
            self._ensure_column(connection, "nodes", "last_seen_at", "REAL")
            self._ensure_column(connection, "leases", "leased_at", "REAL")
            self._ensure_column(connection, "leases", "expires_at", "REAL")
            self._ensure_column(connection, "leases", "expired_at", "REAL")
            self._ensure_column(connection, "leases", "lease_id", "TEXT")
            self._ensure_column(connection, "leases", "request_id", "TEXT")
            self._ensure_column(connection, "leases", "grant_json", "TEXT")
            self._ensure_column(connection, "leases", "grant_hash", "TEXT")
            self._ensure_column(connection, "leases", "acknowledged_at", "REAL")
            self._ensure_column(connection, "leases", "acknowledgement_json", "TEXT")

    def _ensure_column(self, connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing_columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def load_nodes(self) -> tuple[dict[str, NodeIdentity], dict[str, dict[str, Any]], dict[str, float]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM nodes").fetchall()
        nodes = {
            row["node_id"]: NodeIdentity(node_id=row["node_id"], public_key=row["public_key"])
            for row in rows
        }
        capabilities = {
            row["node_id"]: json.loads(row["capabilities_json"])
            for row in rows
        }
        last_seen = {
            row["node_id"]: float(row["last_seen_at"] if row["last_seen_at"] is not None else row["created_at"])
            for row in rows
        }
        return nodes, capabilities, last_seen

    def save_node(self, registration: NodeRegistration, last_seen_at: float | None = None) -> None:
        seen_at = last_seen_at if last_seen_at is not None else registration.created_at
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO nodes (node_id, public_key, capabilities_json, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    public_key = excluded.public_key,
                    capabilities_json = excluded.capabilities_json,
                    created_at = excluded.created_at,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    registration.node_id,
                    registration.node_public_key,
                    json.dumps(registration.capabilities, sort_keys=True),
                    registration.created_at,
                    seen_at,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO credits (node_id, credits) VALUES (?, 0)",
                (registration.node_id,),
            )

    def touch_node(self, node_id: str, last_seen_at: float) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE nodes SET last_seen_at = ? WHERE node_id = ?",
                (last_seen_at, node_id),
            )

    def load_jobs(
        self,
        default_lease_timeout_seconds: float,
    ) -> tuple[
        dict[str, JobPacket],
        dict[str, set[str]],
        dict[str, dict[str, float]],
        dict[str, dict[str, float]],
        dict[str, dict[str, float]],
        dict[str, dict[str, JobLeaseGrant]],
        dict[str, dict[str, JobLeaseAcknowledgement]],
        list[dict[str, Any]],
    ]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM jobs").fetchall()
            lease_rows = connection.execute("SELECT * FROM leases").fetchall()
        jobs = {
            row["job_id"]: JobPacket.from_dict(json.loads(row["packet_json"]))
            for row in rows
        }
        leases: dict[str, set[str]] = {}
        lease_started_at: dict[str, dict[str, float]] = {}
        lease_expires_at: dict[str, dict[str, float]] = {}
        lease_acknowledged_at: dict[str, dict[str, float]] = {}
        lease_grants: dict[str, dict[str, JobLeaseGrant]] = {}
        lease_acknowledgements: dict[str, dict[str, JobLeaseAcknowledgement]] = {}
        expired_leases: list[dict[str, Any]] = []
        for row in rows:
            if row["leased_to"] is not None:
                leased_at = float(row["created_at"])
                expires_at = leased_at + default_lease_timeout_seconds
                leases.setdefault(row["job_id"], set()).add(row["leased_to"])
                lease_started_at.setdefault(row["job_id"], {})[row["leased_to"]] = leased_at
                lease_expires_at.setdefault(row["job_id"], {})[row["leased_to"]] = expires_at
        for row in lease_rows:
            leased_at = float(row["leased_at"] if row["leased_at"] is not None else row["created_at"])
            expires_at = float(
                row["expires_at"] if row["expires_at"] is not None else leased_at + default_lease_timeout_seconds
            )
            lease = {
                "job_id": row["job_id"],
                "node_id": row["node_id"],
                "leased_at": leased_at,
                "expires_at": expires_at,
                "expired_at": row["expired_at"],
                "acknowledged_at": (
                    float(row["acknowledged_at"]) if row["acknowledged_at"] is not None else None
                ),
                "lease_id": row["lease_id"],
                "request_id": row["request_id"],
                "grant_hash": row["grant_hash"],
            }
            if row["grant_json"] is not None:
                lease_grants.setdefault(row["job_id"], {})[row["node_id"]] = (
                    JobLeaseGrant.from_dict(json.loads(row["grant_json"]))
                )
            if row["acknowledged_at"] is not None:
                lease_acknowledged_at.setdefault(row["job_id"], {})[row["node_id"]] = float(row["acknowledged_at"])
            if row["acknowledgement_json"] is not None:
                lease_acknowledgements.setdefault(row["job_id"], {})[row["node_id"]] = (
                    JobLeaseAcknowledgement.from_dict(json.loads(row["acknowledgement_json"]))
                )
            if row["expired_at"] is None:
                leases.setdefault(row["job_id"], set()).add(row["node_id"])
                lease_started_at.setdefault(row["job_id"], {})[row["node_id"]] = leased_at
                lease_expires_at.setdefault(row["job_id"], {})[row["node_id"]] = expires_at
            else:
                lease["expired_at"] = float(row["expired_at"])
                expired_leases.append(lease)
        return (
            jobs,
            leases,
            lease_started_at,
            lease_expires_at,
            lease_acknowledged_at,
            lease_grants,
            lease_acknowledgements,
            expired_leases,
        )

    def save_job(self, job: JobPacket) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (job_id, packet_json, leased_to, created_at)
                VALUES (?, ?, NULL, ?)
                ON CONFLICT(job_id) DO UPDATE SET packet_json = excluded.packet_json
                """,
                (job.job_id, json.dumps(job.to_dict(), sort_keys=True), time.time()),
            )

    def save_lease(
        self,
        job_id: str,
        node_id: str,
        *,
        leased_at: float,
        expires_at: float,
        grant: JobLeaseGrant | None = None,
    ) -> None:
        grant_json = json.dumps(grant.to_dict(), sort_keys=True) if grant is not None else None
        grant_hash = grant.grant_hash() if grant is not None else None
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET leased_to = ? WHERE job_id = ?",
                (node_id, job_id),
            )
            connection.execute(
                """
                INSERT INTO leases (
                    job_id,
                    node_id,
                    created_at,
                    leased_at,
                    expires_at,
                    expired_at,
                    lease_id,
                    request_id,
                    grant_json,
                    grant_hash,
                    acknowledged_at,
                    acknowledgement_json
                )
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(job_id, node_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    leased_at = excluded.leased_at,
                    expires_at = excluded.expires_at,
                    expired_at = NULL,
                    lease_id = excluded.lease_id,
                    request_id = excluded.request_id,
                    grant_json = excluded.grant_json,
                    grant_hash = excluded.grant_hash,
                    acknowledged_at = NULL,
                    acknowledgement_json = NULL
                """,
                (
                    job_id,
                    node_id,
                    leased_at,
                    leased_at,
                    expires_at,
                    grant.lease_id if grant is not None else None,
                    grant.request_id if grant is not None else None,
                    grant_json,
                    grant_hash,
                ),
            )

    def save_lease_acknowledgement(self, acknowledgement: JobLeaseAcknowledgement) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE leases
                SET acknowledged_at = ?,
                    acknowledgement_json = ?
                WHERE job_id = ? AND node_id = ? AND expired_at IS NULL
                """,
                (
                    acknowledgement.created_at,
                    json.dumps(acknowledgement.to_dict(), sort_keys=True),
                    acknowledgement.job_id,
                    acknowledgement.node_id,
                ),
            )

    def renew_lease(self, job_id: str, node_id: str, *, expires_at: float) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE leases
                SET expires_at = ?
                WHERE job_id = ? AND node_id = ? AND expired_at IS NULL
                """,
                (expires_at, job_id, node_id),
            )

    def mark_lease_expired(self, job_id: str, node_id: str, *, expired_at: float) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE leases
                SET expired_at = ?
                WHERE job_id = ? AND node_id = ? AND expired_at IS NULL
                """,
                (expired_at, job_id, node_id),
            )
            connection.execute(
                """
                UPDATE jobs
                SET leased_to = NULL
                WHERE job_id = ? AND leased_to = ?
                """,
                (job_id, node_id),
            )

    def load_results(self) -> dict[str, list[JobResult]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM results").fetchall()
        results: dict[str, list[JobResult]] = {}
        for row in rows:
            results.setdefault(row["job_id"], []).append(
                JobResult.from_dict(json.loads(row["result_json"]))
            )
        return results

    def save_result(self, result: JobResult) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO results (job_id, node_id, result_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, node_id) DO UPDATE SET
                    result_json = excluded.result_json,
                    created_at = excluded.created_at
                """,
                (
                    result.job_id,
                    result.node_id,
                    json.dumps(result.to_dict(), sort_keys=True),
                    result.created_at,
                ),
            )

    def load_credits(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM credits").fetchall()
        return {row["node_id"]: int(row["credits"]) for row in rows}

    def load_seen_packet_ids(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT packet_id FROM seen_packets").fetchall()
        return {row["packet_id"] for row in rows}

    def save_seen_packet(
        self,
        *,
        packet_id: str,
        packet_type: str,
        node_id: str,
        created_at: float,
        seen_at: float,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO seen_packets (packet_id, packet_type, node_id, created_at, seen_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (packet_id, packet_type, node_id, created_at, seen_at),
            )

    def set_credit(self, node_id: str, credits: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO credits (node_id, credits) VALUES (?, ?)
                ON CONFLICT(node_id) DO UPDATE SET credits = excluded.credits
                """,
                (node_id, credits),
            )
