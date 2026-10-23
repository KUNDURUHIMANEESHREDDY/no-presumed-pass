"""Deterministic verdict derivation with no presumed-pass behavior."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable


class EvidenceOutcome(str, Enum):
    """An observed outcome for one criterion; absence is represented by no record."""

    PASS = "PASS"
    FAIL = "FAIL"


class VerdictStatus(str, Enum):
    """Three-valued verdict derived from required criteria and stored evidence."""

    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """Immutable, attributable evidence for one criterion."""

    evidence_id: str
    criterion: str
    outcome: EvidenceOutcome
    source: str
    observed_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("evidence_id", "criterion", "source"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            if value != value.strip():
                raise ValueError(f"{field_name} must not contain surrounding whitespace")

        if not isinstance(self.outcome, EvidenceOutcome):
            raise ValueError("outcome must be an EvidenceOutcome")
        if not isinstance(self.observed_at, datetime):
            raise ValueError("observed_at must be a datetime")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Verdict:
    """Explainable result; callers cannot supply a status to the derivation function."""

    status: VerdictStatus
    required_criteria: tuple[str, ...]
    missing_criteria: tuple[str, ...]
    failed_criteria: tuple[str, ...]
    unmatched_criteria: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    reason: str


def derive_verdict(
    required_criteria: Iterable[str],
    evidence: Iterable[EvidenceRecord],
) -> Verdict:
    """Derive PASS/FAIL/INDETERMINATE from evidence, never from caller-supplied status.

    A failure for any required criterion wins over a pass for that criterion.
    Missing required evidence prevents PASS. Evidence for unconfigured criteria is
    surfaced as unmatched and cannot satisfy a required criterion. An empty policy
    is INDETERMINATE rather than a vacuous PASS.
    """

    criteria = tuple(required_criteria)
    if any(not isinstance(item, str) or not item.strip() for item in criteria):
        raise ValueError("required criteria must be non-empty strings")
    if any(item != item.strip() for item in criteria):
        raise ValueError("required criteria must not contain surrounding whitespace")
    if len(set(criteria)) != len(criteria):
        raise ValueError("required criteria must be unique")

    records = tuple(evidence)
    seen_ids: set[str] = set()
    for record in records:
        if not isinstance(record, EvidenceRecord):
            raise ValueError("evidence must contain only EvidenceRecord instances")
        if record.evidence_id in seen_ids:
            raise ValueError(f"duplicate evidence_id: {record.evidence_id}")
        seen_ids.add(record.evidence_id)

    if not criteria:
        return Verdict(
            status=VerdictStatus.INDETERMINATE,
            required_criteria=(),
            missing_criteria=(),
            failed_criteria=(),
            unmatched_criteria=tuple(sorted({record.criterion for record in records})),
            evidence_ids=(),
            reason="no_required_criteria_configured",
        )

    required = set(criteria)
    relevant = tuple(record for record in records if record.criterion in required)
    observed = {record.criterion for record in relevant}
    missing = tuple(item for item in criteria if item not in observed)
    failed = tuple(
        item
        for item in criteria
        if any(
            record.criterion == item and record.outcome is EvidenceOutcome.FAIL
            for record in relevant
        )
    )
    unmatched = tuple(sorted({record.criterion for record in records if record.criterion not in required}))
    evidence_ids = tuple(sorted(record.evidence_id for record in relevant))

    if failed:
        status = VerdictStatus.FAIL
        reason = "one_or_more_required_criteria_failed"
    elif missing:
        status = VerdictStatus.INDETERMINATE
        reason = "required_evidence_missing"
    else:
        status = VerdictStatus.PASS
        reason = "all_required_criteria_have_passing_evidence"

    return Verdict(
        status=status,
        required_criteria=criteria,
        missing_criteria=missing,
        failed_criteria=failed,
        unmatched_criteria=unmatched,
        evidence_ids=evidence_ids,
        reason=reason,
    )
