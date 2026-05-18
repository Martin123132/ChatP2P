"""SQLite-backed coordinator state."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .crypto import NodeIdentity
from .packets import JobPacket, JobResult, NodeRegistration


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
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    packet_json TEXT NOT NULL,
                    leased_to TEXT,
                    created_at REAL NOT NULL
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
                """
            )

    def load_nodes(self) -> tuple[dict[str, NodeIdentity], dict[str, dict[str, Any]]]:
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
        return nodes, capabilities

    def save_node(self, registration: NodeRegistration) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO nodes (node_id, public_key, capabilities_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    public_key = excluded.public_key,
                    capabilities_json = excluded.capabilities_json,
                    created_at = excluded.created_at
                """,
                (
                    registration.node_id,
                    registration.node_public_key,
                    json.dumps(registration.capabilities, sort_keys=True),
                    registration.created_at,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO credits (node_id, credits) VALUES (?, 0)",
                (registration.node_id,),
            )

    def load_jobs(self) -> tuple[dict[str, JobPacket], dict[str, str]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM jobs").fetchall()
        jobs = {
            row["job_id"]: JobPacket.from_dict(json.loads(row["packet_json"]))
            for row in rows
        }
        leases = {
            row["job_id"]: row["leased_to"]
            for row in rows
            if row["leased_to"] is not None
        }
        return jobs, leases

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

    def save_lease(self, job_id: str, node_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET leased_to = ? WHERE job_id = ?",
                (node_id, job_id),
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

    def set_credit(self, node_id: str, credits: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO credits (node_id, credits) VALUES (?, ?)
                ON CONFLICT(node_id) DO UPDATE SET credits = excluded.credits
                """,
                (node_id, credits),
            )
