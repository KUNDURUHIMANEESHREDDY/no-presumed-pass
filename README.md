# No Presumed Pass

An evidence-driven verification platform. A verdict is derived from persisted, attributable evidence; it is never supplied by a caller or inferred from the absence of failures.

## Core contract

The platform uses three outcomes:

- **PASS** — every required criterion has explicit passing evidence and no required criterion has failing evidence.
- **FAIL** — at least one required criterion has explicit failing evidence.
- **INDETERMINATE** — required evidence is missing, the policy is empty, or the available evidence cannot establish a verdict.

Missing evidence must never be treated as success. Conflicting evidence is preserved and must not be silently rewritten; a recorded failure takes precedence over a pass for the same required criterion until a separately specified resolution policy exists.

## Initial implementation scope

The first implementation is deliberately a small, deterministic core:

1. Represent evidence as immutable records with a stable identifier, criterion, outcome, source, and observation timestamp.
2. Derive a verdict from an explicit list of required criteria and evidence records.
3. Return missing and failed criteria plus the IDs of evidence used, so a result is explainable.
4. Reject malformed evidence and duplicate evidence IDs.
5. Verify the contract with unit tests and run them in GitHub Actions.

The current slice includes a local SQLite evidence store with atomic batch writes, schema-version checks, integrity-checked backups, and validated restore-to-new-path support. It does **not** yet provide an HTTP API, authentication, multi-tenant authorization, production backup retention/off-site recovery, deployment, or operational recovery. SQLite triggers guard against ordinary record updates/deletes, but are not tamper-proof against an operator who can alter the database schema. These are explicit follow-up work, not implied capabilities.

## Local evidence persistence

`EvidenceStore` stores immutable evidence records in a SQLite database and derives verdicts from the records currently persisted there. It refuses duplicate evidence IDs, rolls back a batch if any row fails, and normalizes observation timestamps to UTC. The store is a local library primitive; do not expose its database file to untrusted writers or treat it as a multi-tenant production service. `backup_to()` writes a consistent integrity-checked copy without overwriting an existing destination. `EvidenceStore.restore_to(source, destination)` validates the source schema and append-only triggers, then publishes a verified copy to a new path; it refuses to replace existing files. These are local primitives, not a production retention policy or disaster-recovery service.

```python
from datetime import datetime, timezone
from no_presumed_pass import EvidenceOutcome, EvidenceRecord, EvidenceStore

with EvidenceStore("evidence.sqlite3") as store:
    store.append(EvidenceRecord(
        evidence_id="ci-run-123",
        criterion="unit-tests",
        outcome=EvidenceOutcome.PASS,
        source="github-actions",
        observed_at=datetime.now(timezone.utc),
    ))
    verdict = store.derive(("unit-tests",))
    print(verdict.status.value)
```

## Development

Requires Python 3.11 or newer.

```bash
python -m unittest discover -s tests -v
```

The core is dependency-free. CI is expected to run the same command on every pull request and push to the default branch.

## Readiness status

- [x] Local SQLite evidence store with atomic batch writes, unique IDs, schema versioning, update/delete guards, and integrity-checked local backup/restore primitives; regression tests passed in current-head CI
- [x] Deterministic three-valued verdict engine and edge-case tests passed in current-head CI
- [ ] API contract, authentication, and authorization boundaries
- [ ] Production migration policy, off-site backup retention, restore drills, and operational recovery
- [ ] Security/dependency scanning and secret handling
- [ ] Reproducible CI, release gate, health checks, deployment and rollback documentation
- [ ] End-to-end tests proving missing evidence can never produce PASS
