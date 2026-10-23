# No Presumed Pass

An evidence-driven verification platform. A verdict is derived from persisted, attributable evidence; it is never supplied by a caller or inferred from the absence of failures.

## Core contract

The platform uses three outcomes:

- **PASS** — every required criterion has explicit passing evidence and no required criterion has failing evidence.
- **FAIL** — at least one required criterion has explicit failing evidence.
- **INDETERMINATE** — required evidence is missing, the policy is empty, or the available evidence cannot establish a verdict.

Missing evidence must never be treated as success. Conflicting evidence is preserved and must not be silently rewritten; a recorded failure takes precedence over a pass for the same required criterion until a separately specified resolution policy exists.

## Evidence persistence

`EvidenceStore` stores immutable evidence records in SQLite with atomic batch writes, unique evidence IDs, schema-version checks, and update/delete guards. `backup_to()` creates a consistent integrity-checked copy without overwriting an existing destination. `EvidenceStore.restore_to(source, destination)` validates the source schema and append-only triggers, verifies the restored copy, and refuses to replace existing files.

These are local persistence and recovery primitives, not a production retention policy. SQLite triggers do not protect against an operator who can alter the database schema. Production deployment still needs off-site backups, retention, access controls, migration policy, and tested recovery objectives.

## Local HTTP API

The repository includes a small standard-library HTTP adapter for local integrations. It binds only to `127.0.0.1`; it deliberately does not offer a public-network bind mode. Evidence reads and writes require a bearer token of at least 32 characters. `/healthz` reports process liveness; `/readyz` performs a bounded read against SQLite and returns 503 with a generic error if storage is unavailable.

Start it with a local database and a strong token:

```bash
export NO_PRESUMED_PASS_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m no_presumed_pass.http_api --database evidence.sqlite3 --port 8765
```

PowerShell equivalent:

```powershell
$env:NO_PRESUMED_PASS_API_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m no_presumed_pass.http_api --database evidence.sqlite3 --port 8765
```

The token is held in the process environment and must not be committed, printed into CI logs, or embedded in frontend assets. This adapter uses HTTP on loopback and a single configured token: **do not expose it beyond the local machine or treat it as a multi-user production service**. A production deployment needs TLS at a trusted reverse proxy, user/tenant authorization, rotation and revocation, rate limits, audit retention, and a hardened service supervisor.

### Routes

- `GET /healthz` — unauthenticated liveness check; returns only `{"status":"ok"}`.
- `GET /readyz` — unauthenticated readiness check; verifies that the evidence store can serve a bounded read and returns a generic 503 without database details when it cannot.
- `POST /v1/evidence` — authenticated JSON evidence insert; rejects duplicate IDs and malformed or timezone-naive records.
- `GET /v1/evidence` — authenticated evidence listing with bounded pagination. Optional `criterion`, `limit` (default 50, maximum 100), and `offset` (maximum 1,000,000) query parameters. The response includes `pagination.has_more` and `pagination.next_offset`; results use deterministic observation-time/evidence-ID ordering.
- `GET /v1/verdict?criterion=build&criterion=tests` — authenticated verdict derived from persisted evidence. Repeat `criterion` for each required criterion.

Writes are capped at 64 KiB, unsupported routes return explicit errors, duplicate JSON keys are rejected, responses are not cacheable, and logs omit request bodies and authorization headers.

Example insert body:

```json
{
  "evidence_id": "ci-run-123",
  "criterion": "unit-tests",
  "outcome": "PASS",
  "source": "github-actions",
  "observed_at": "2026-10-24T00:00:00Z"
}
```

## Development

Requires Python 3.11 or newer. The core and HTTP adapter use only the Python standard library.

```bash
python -m unittest discover -s tests -v
```

CI runs the same test command on pull requests and pushes to the default branch. HTTP tests start a loopback server on an ephemeral port and cover authentication, evidence-to-verdict behavior, missing evidence, duplicate IDs/JSON keys, invalid timestamps, payload size, media type, unsupported routes, deterministic pagination, and rejection of malformed or out-of-range pagination parameters.

## Readiness status

- [x] Local SQLite evidence store with atomic batch writes, schema checks, append-only guards, and integrity-checked local backup/restore; current-head CI passed
- [x] Deterministic three-valued verdict engine; current-head CI passed
- [ ] Database-backed `/readyz` regression test; awaiting current-head CI verification
- [x] Loopback-only authenticated HTTP adapter with bounded JSON input, bounded evidence pagination, and explicit error contracts; CI run #40014396673 and Security run #40014396690 passed after pagination was added. Review CodeQL alerts separately before release.
- [ ] Production API authentication, authorization/tenant isolation, token rotation, and rate limiting
- [ ] Production migration policy, off-site backup retention, restore drills, and operational recovery
- [x] Gitleaks secret scanning and Python CodeQL analysis are configured and have completed successfully on a recent PR head; review CodeQL alerts in repository Security before release. Third-party dependency audit is not claimed because no dependency manifest exists.
- [ ] Deployment supervision, health monitoring, release gate, and rollback documentation
- [ ] End-to-end deployment tests proving missing evidence can never produce PASS
