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

This bootstrap does **not** yet provide an HTTP API, durable database, authentication, multi-tenant authorization, deployment, or production operations. Those are explicit follow-up work, not implied capabilities.

## Development

Requires Python 3.11 or newer.

```bash
python -m unittest discover -s tests -v
```

The core is dependency-free. CI is expected to run the same command on every pull request and push to the default branch.

## Readiness status

- [ ] Local SQLite evidence store implemented with atomic batch writes, unique IDs, schema versioning, and update/delete guards; tests added, current-head CI pending
- [ ] Deterministic three-valued verdict engine and edge-case tests (prior core-only CI passed; current-head verification pending)
- [ ] API contract, authentication, and authorization boundaries
- [ ] Database schema, migrations, transaction and restore tests
- [ ] Security/dependency scanning and secret handling
- [ ] Reproducible CI, release gate, health checks, deployment and rollback documentation
- [ ] End-to-end tests proving missing evidence can never produce PASS
