"""Single-owner durable fencing and action admission. Never replay pending effects."""

import fcntl
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .protocol import Call, Lease, WorkerError


class Journal:
    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(state_dir, 0o700)
        self.lock_file = (state_dir / "worker.lock").open("a+")
        os.chmod(state_dir / "worker.lock", 0o600)
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            raise RuntimeError("Another worker owns this state directory") from None
        self.db = sqlite3.connect(state_dir / "worker.sqlite3")
        os.chmod(state_dir / "worker.sqlite3", 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS lease (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS actions (
                lease_id TEXT NOT NULL, epoch INTEGER NOT NULL, action_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, status INTEGER, receipt TEXT,
                PRIMARY KEY (lease_id, epoch, action_id));
            CREATE TABLE IF NOT EXISTS metrics (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS fork_checkpoints (
                checkpoint_id TEXT PRIMARY KEY, phase TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fork_rebindings (
                fork_id TEXT PRIMARY KEY, checkpoint_id TEXT NOT NULL,
                computer_id TEXT NOT NULL, incarnation_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, phase TEXT NOT NULL);
        """)
        self.db.commit()

    def lease(self) -> Lease | None:
        row = self.db.execute("SELECT body FROM lease WHERE singleton=1").fetchone()
        return Lease.model_validate_json(row[0]) if row else None

    def set_lease(self, lease: Lease):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO lease VALUES (1, ?)", (lease.model_dump_json(),)
            )

    @staticmethod
    def fingerprint(call: Call) -> str:
        body = json.dumps([call.name, call.arguments], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()

    def receipt(self, call: Call) -> tuple[int, dict] | None:
        row = self.db.execute(
            "SELECT fingerprint, status, receipt FROM actions WHERE lease_id=? AND epoch=? AND action_id=?",
            (call.lease_id, call.epoch, call.action_id),
        ).fetchone()
        if not row:
            return None
        if row[0] != self.fingerprint(call):
            raise WorkerError(
                "action_id_conflict", "Action ID was already used for different arguments"
            )
        if row[1] is None:
            raise WorkerError(
                "action_effect_unknown",
                "Action was admitted without a saved receipt; do not replay",
                effect="unknown",
            )
        return row[1], json.loads(row[2])

    def admit(self, call: Call):
        with self.db:
            self.db.execute(
                "INSERT INTO actions VALUES (?, ?, ?, ?, NULL, NULL)",
                (call.lease_id, call.epoch, call.action_id, self.fingerprint(call)),
            )

    def finish(self, call: Call, status: int, receipt: dict):
        with self.db:
            self.db.execute(
                "UPDATE actions SET status=?, receipt=? WHERE lease_id=? AND epoch=? AND action_id=?",
                (status, json.dumps(receipt), call.lease_id, call.epoch, call.action_id),
            )

    def reject(self, call: Call, status: int, receipt: dict, delta: dict[str, int]) -> dict:
        """Save a completed rejection and its counters without admitting an effect."""
        with self.db:
            for name, amount in delta.items():
                self.db.execute(
                    "INSERT INTO metrics VALUES (?, ?) ON CONFLICT(name) "
                    "DO UPDATE SET value=value+excluded.value",
                    (name, amount),
                )
            body = {
                **receipt,
                "metrics": {**receipt["metrics"], "delta": delta, "totals": self.metrics()},
            }
            self.db.execute(
                "INSERT INTO actions VALUES (?, ?, ?, ?, ?, ?)",
                (call.lease_id, call.epoch, call.action_id, self.fingerprint(call),
                 status, json.dumps(body)),
            )
        return body

    def increment(self, name: str, amount: int = 1):
        with self.db:
            self.db.execute(
                "INSERT INTO metrics VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value",
                (name, amount),
            )

    def metrics(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT name, value FROM metrics"))

    def prepared_checkpoint(self) -> str | None:
        row = self.db.execute(
            "SELECT checkpoint_id FROM fork_checkpoints WHERE phase IN ('prepared', 'rebinding')"
        ).fetchone()
        return row[0] if row else None

    def prepare_fork(self, checkpoint_id: str):
        row = self.db.execute(
            "SELECT phase FROM fork_checkpoints WHERE checkpoint_id=?", (checkpoint_id,)
        ).fetchone()
        if row:
            if row[0] == "prepared":
                return
            raise WorkerError("fork_checkpoint_closed", "Checkpoint can no longer be prepared")
        if self.prepared_checkpoint() is not None:
            raise WorkerError("fork_prepared", "Another checkpoint is already prepared")
        if self.db.execute("SELECT 1 FROM actions WHERE status IS NULL LIMIT 1").fetchone():
            raise WorkerError(
                "fork_action_unresolved", "An admitted action has no completed receipt"
            )
        with self.db:
            self.db.execute("INSERT INTO fork_checkpoints VALUES (?, 'prepared')", (checkpoint_id,))

    def resume_fork(self, checkpoint_id: str):
        row = self.db.execute(
            "SELECT phase FROM fork_checkpoints WHERE checkpoint_id=?", (checkpoint_id,)
        ).fetchone()
        if row and row[0] == "resumed":
            return
        if row is None or row[0] != "prepared":
            raise WorkerError("fork_checkpoint_closed", "Checkpoint cannot resume parent authority")
        with self.db:
            self.db.execute(
                "UPDATE fork_checkpoints SET phase='resumed' WHERE checkpoint_id=?", (checkpoint_id,)
            )

    def rebind_record(self, fork_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT checkpoint_id, computer_id, incarnation_id, fingerprint, phase "
            "FROM fork_rebindings WHERE fork_id=?", (fork_id,),
        ).fetchone()
        return dict(zip(
            ("checkpoint_id", "computer_id", "incarnation_id", "fingerprint", "phase"), row,
            strict=True,
        )) if row else None

    def begin_rebind(self, request, fingerprint: str):
        if self.prepared_checkpoint() != request.checkpoint_id:
            raise WorkerError("fork_not_prepared", "Rebind requires the exact prepared checkpoint")
        prior = self.rebind_record(request.fork_id)
        if prior:
            if prior["fingerprint"] != fingerprint:
                raise WorkerError("fork_rebind_conflict", "Fork ID was already bound differently")
            return
        row = self.db.execute(
            "SELECT phase FROM fork_checkpoints WHERE checkpoint_id=?", (request.checkpoint_id,)
        ).fetchone()
        if row[0] != "prepared":
            raise WorkerError("fork_rebind_conflict", "Checkpoint is already being rebound")
        # This durable gate precedes token publication and any in-memory reset.
        # A crash or cancelled rebind can only be completed by an exact retry.
        with self.db:
            self.db.execute(
                "INSERT INTO fork_rebindings VALUES (?, ?, ?, ?, ?, 'pending')",
                (request.fork_id, request.checkpoint_id, request.computer_id,
                 request.incarnation_id, fingerprint),
            )
            self.db.execute(
                "UPDATE fork_checkpoints SET phase='rebinding' WHERE checkpoint_id=?",
                (request.checkpoint_id,),
            )
            self.db.execute("DELETE FROM lease")
            self.db.execute("DELETE FROM actions")
            self.db.execute("DELETE FROM metrics")

    def finish_rebind(self, request):
        with self.db:
            # Keep old IDs for conflict detection, but only the current binding
            # may be replayed successfully after another full-state fork.
            self.db.execute("UPDATE fork_rebindings SET phase='superseded' WHERE phase='complete'")
            self.db.execute(
                "UPDATE fork_rebindings SET phase='complete' WHERE fork_id=?", (request.fork_id,)
            )
            self.db.execute(
                "UPDATE fork_checkpoints SET phase='rebound' WHERE checkpoint_id=?",
                (request.checkpoint_id,),
            )

    def close(self):
        self.db.close()
        fcntl.flock(self.lock_file, fcntl.LOCK_UN)
        self.lock_file.close()
