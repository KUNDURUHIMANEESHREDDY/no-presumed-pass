from datetime import datetime, timezone
import unittest

from no_presumed_pass import (
    EvidenceOutcome,
    EvidenceRecord,
    VerdictStatus,
    derive_verdict,
)


def evidence(evidence_id: str, criterion: str, outcome: EvidenceOutcome) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        criterion=criterion,
        outcome=outcome,
        source="unit-test",
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class DeriveVerdictTests(unittest.TestCase):
    def test_all_required_criteria_with_pass_evidence_pass(self) -> None:
        result = derive_verdict(
            ["build", "tests"],
            [
                evidence("e-1", "build", EvidenceOutcome.PASS),
                evidence("e-2", "tests", EvidenceOutcome.PASS),
            ],
        )
        self.assertIs(result.status, VerdictStatus.PASS)
        self.assertEqual(result.evidence_ids, ("e-1", "e-2"))
        self.assertEqual(result.missing_criteria, ())

    def test_missing_evidence_is_indeterminate_not_pass(self) -> None:
        result = derive_verdict(
            ["build", "security"],
            [evidence("e-1", "build", EvidenceOutcome.PASS)],
        )
        self.assertIs(result.status, VerdictStatus.INDETERMINATE)
        self.assertEqual(result.missing_criteria, ("security",))

    def test_any_required_failure_fails(self) -> None:
        result = derive_verdict(
            ["build", "tests"],
            [
                evidence("e-1", "build", EvidenceOutcome.PASS),
                evidence("e-2", "tests", EvidenceOutcome.FAIL),
            ],
        )
        self.assertIs(result.status, VerdictStatus.FAIL)
        self.assertEqual(result.failed_criteria, ("tests",))

    def test_failure_wins_when_evidence_conflicts(self) -> None:
        result = derive_verdict(
            ["security"],
            [
                evidence("e-pass", "security", EvidenceOutcome.PASS),
                evidence("e-fail", "security", EvidenceOutcome.FAIL),
            ],
        )
        self.assertIs(result.status, VerdictStatus.FAIL)
        self.assertEqual(result.evidence_ids, ("e-fail", "e-pass"))

    def test_empty_policy_is_indeterminate_not_vacuous_pass(self) -> None:
        result = derive_verdict([], [])
        self.assertIs(result.status, VerdictStatus.INDETERMINATE)
        self.assertEqual(result.reason, "no_required_criteria_configured")

    def test_unconfigured_evidence_does_not_satisfy_required_criterion(self) -> None:
        result = derive_verdict(
            ["security"],
            [evidence("e-1", "build", EvidenceOutcome.PASS)],
        )
        self.assertIs(result.status, VerdictStatus.INDETERMINATE)
        self.assertEqual(result.missing_criteria, ("security",))
        self.assertEqual(result.unmatched_criteria, ("build",))

    def test_duplicate_evidence_ids_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate evidence_id"):
            derive_verdict(
                ["build"],
                [
                    evidence("same", "build", EvidenceOutcome.PASS),
                    evidence("same", "build", EvidenceOutcome.FAIL),
                ],
            )

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            EvidenceRecord(
                evidence_id="e-1",
                criterion="build",
                outcome=EvidenceOutcome.PASS,
                source="unit-test",
                observed_at=datetime(2026, 1, 1),
            )

    def test_blank_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "source"):
            EvidenceRecord(
                evidence_id="e-1",
                criterion="build",
                outcome=EvidenceOutcome.PASS,
                source=" ",
                observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

    def test_required_criteria_must_be_unique(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique"):
            derive_verdict(["build", "build"], [])

    def test_string_is_not_accepted_as_criterion_collection(self) -> None:
        with self.assertRaisesRegex(ValueError, "not a string"):
            derive_verdict("build", [])


if __name__ == "__main__":
    unittest.main()
