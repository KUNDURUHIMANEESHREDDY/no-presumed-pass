from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from no_presumed_pass import EvidenceOutcome, EvidenceRecord, EvidenceStore, VerdictStatus


def record(
    evidence_id: str,
    criterion: str = "tests",
    outcome: EvidenceOutcome = EvidenceOutcome.PASS,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        criterion=criterion,
        outcome=outcome,
        source="unit-test",
        observed_at=datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc),
    )


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "evidence.sqlite3"
        self.store = EvidenceStore(self.database)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_records_survive_reopen_and_drive_verdict(self) -> None:
        self.store.append_many(
            (record("build", "build"), record("tests", "tests"))
        )
        self.store.close()

        with EvidenceStore(self.database) as reopened:
            result = reopened.derive(("build", "tests"))

        self.assertIs(result.status, VerdictStatus.PASS)
        self.assertEqual(result.evidence_ids, ("build", "tests"))

    def test_backup_is_consistent_and_can_be_reopened(self) -> None:
        self.store.append_many((record("build", "build"), record("tests", "tests")))
        destination = self.database.with_name("evidence-backup.sqlite3")

        self.assertEqual(self.store.backup_to(destination), destination)

        with EvidenceStore(destination) as backup:
            result = backup.derive(("build", "tests"))
            self.assertIs(result.status, VerdictStatus.PASS)
            self.assertEqual(result.evidence_ids, ("build", "tests"))

        with sqlite3.connect(destination) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_backup_never_overwrites_an_existing_destination(self) -> None:
        destination = self.database.with_name("preserve-existing.db")
        destination.write_bytes(b"existing user data")

        with self.assertRaises(FileExistsError):
            self.store.backup_to(destination)

        self.assertEqual(destination.read_bytes(), b"existing user data")

    def test_existing_evidence_id_cannot_be_overwritten(self) -> None:
        original = record("same-id", outcome=EvidenceOutcome.PASS)
        self.store.append(original)

        with self.assertRaisesRegex(ValueError, "evidence_id already exists"):
            self.store.append(record("same-id", outcome=EvidenceOutcome.FAIL))

        records = self.store.list_evidence()
        self.assertEqual(len(records), 1)
        self.assertIs(records[0].outcome, EvidenceOutcome.PASS)

    def test_batch_insert_rolls_back_when_any_id_already_exists(self) -> None:
        self.store.append(record("existing"))

        with self.assertRaisesRegex(ValueError, "evidence_id already exists"):
            self.store.append_many((record("new"), record("existing", outcome=EvidenceOutcome.FAIL)))

        self.assertEqual(tuple(item.evidence_id for item in self.store.list_evidence()), ("existing",))

    def test_duplicate_ids_inside_batch_are_rejected_before_write(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate evidence_id in batch"):
            self.store.append_many((record("dup"), record("dup", outcome=EvidenceOutcome.FAIL)))
        self.assertEqual(self.store.list_evidence(), ())

    def test_database_rejects_update_and_delete(self) -> None:
        self.store.append(record("immutable"))

        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store._connection.execute(
                "UPDATE evidence SET outcome = 'FAIL' WHERE evidence_id = 'immutable'"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store._connection.execute(
                "DELETE FROM evidence WHERE evidence_id = 'immutable'"
            )

        self.assertIs(self.store.list_evidence()[0].outcome, EvidenceOutcome.PASS)

    def test_failure_in_stored_evidence_cannot_be_hidden_by_pass(self) -> None:
        self.store.append_many(
            (
                record("pass", "security", EvidenceOutcome.PASS),
                record("fail", "security", EvidenceOutcome.FAIL),
            )
        )
        self.assertIs(self.store.derive(("security",)).status, VerdictStatus.FAIL)

    def test_filter_rejects_ambiguous_criterion(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty string"):
            self.store.list_evidence("  ")

    def test_newer_schema_is_not_silently_downgraded(self) -> None:
        self.store.close()
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA user_version = 999")

        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            EvidenceStore(self.database)


if __name__ == "__main__":
    unittest.main()
