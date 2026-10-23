"""Append-only SQLite persistence for attributable verification evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Iterable

from .verdict import EvidenceOutcome, EvidenceRecord, Verdict, derive_verdict

_SCHEMA_VERSION = 1


class EvidenceStore:
    """Persist evidence atomically and derive verdicts only from stored records.

    The store deliberately exposes append and read operations only. SQLite
    constraints and triggers enforce unique IDs and prevent record mutation or
    deletion through this connection. This is a local persistence primitive,
    not a multi-tenant service or a substitute for OS-level database access
    controls, backups, or an authenticated API.
    """

    def __init__(self, database: str | Path) -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(
            str(database),
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self._lock:
            current_version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > _SCHEMA_VERSION:
                self._connection.close()
                raise RuntimeError(
                    f"database schema version {current_version} is newer than supported "
                    f"version {_SCHEMA_VERSION}"
                )

            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS evidence (
                        evidence_id TEXT PRIMARY KEY NOT NULL
                            CHECK (length(trim(evidence_id)) > 0),
                        criterion TEXT NOT NULL
                            CHECK (length(trim(criterion)) > 0),
                        outcome TEXT NOT NULL CHECK (outcome IN ('PASS', 'FAIL')),
                        source TEXT NOT NULL CHECK (length(trim(source)) > 0),
                        observed_at TEXT NOT NULL,
                        recorded_at TEXT NOT NULL
                            DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    )
                    """
                )
                self._connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS evidence_no_update
                    BEFORE UPDATE ON evidence
                    BEGIN
                        SELECT RAISE(ABORT, 'evidence is append-only');
                    END
                    """
                )
                self._connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS evidence_no_delete
                    BEFORE DELETE ON evidence
                    BEGIN
                        SELECT RAISE(ABORT, 'evidence is append-only');
                    END
                    """
                )
                self._connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _validate_records(records: tuple[EvidenceRecord, ...]) -> None:
        seen: set[str] = set()
        for record in records:
            if not isinstance(record, EvidenceRecord):
                raise ValueError("records must contain only EvidenceRecord instances")
            if record.evidence_id in seen:
                raise ValueError(f"duplicate evidence_id in batch: {record.evidence_id}")
            seen.add(record.evidence_id)

    def append(self, record: EvidenceRecord) -> EvidenceRecord:
        """Append one record; never replace an existing evidence ID."""
        self.append_many((record,))
        return record

    def append_many(self, records: Iterable[EvidenceRecord]) -> tuple[EvidenceRecord, ...]:
        """Append a batch atomically; any invalid row rolls back the whole batch."""
        batch = tuple(records)
        self._validate_records(batch)
        if not batch:
            return ()

        rows = [
            (
                record.evidence_id,
                record.criterion,
                record.outcome.value,
                record.source,
                record.observed_at.astimezone(timezone.utc).isoformat(),
            )
            for record in batch
        ]

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.executemany(
                    """
                    INSERT INTO evidence
                        (evidence_id, criterion, outcome, source, observed_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                self._connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._connection.execute("ROLLBACK")
                if "evidence.evidence_id" in str(exc):
                    raise ValueError("evidence_id already exists") from exc
                raise
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return batch

    def list_evidence(self, criterion: str | None = None) -> tuple[EvidenceRecord, ...]:
        """Read immutable evidence in deterministic observation-time order."""
        if criterion is not None:
            if not isinstance(criterion, str) or not criterion.strip():
                raise ValueError("criterion must be a non-empty string when supplied")
            if criterion != criterion.strip():
                raise ValueError("criterion must not contain surrounding whitespace")

        with self._lock:
            if criterion is None:
                rows = self._connection.execute(
                    """
                    SELECT evidence_id, criterion, outcome, source, observed_at
                    FROM evidence ORDER BY observed_at, evidence_id
                    """
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT evidence_id, criterion, outcome, source, observed_at
                    FROM evidence WHERE criterion = ?
                    ORDER BY observed_at, evidence_id
                    """,
                    (criterion,),
                ).fetchall()

        return tuple(
            EvidenceRecord(
                evidence_id=row["evidence_id"],
                criterion=row["criterion"],
                outcome=EvidenceOutcome(row["outcome"]),
                source=row["source"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
            )
            for row in rows
        )

    def derive(self, required_criteria: Iterable[str]) -> Verdict:
        """Derive a verdict from the records currently persisted in this store."""
        return derive_verdict(required_criteria, self.list_evidence())

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> EvidenceStore:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
