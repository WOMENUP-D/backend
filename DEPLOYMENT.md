# Backend CI and production image

Production is managed by [WOMENUP-D/infra](https://github.com/WOMENUP-D/infra).
All production secrets and application variables live in its GitHub environment
**prod**. This repository has no SSH credentials and does not contact the VPS.

Configure INFRA_PUSH_TOKEN here with contents read/write access to infra.
GITHUB_TOKEN publishes ghcr.io/womenup-d/backend. PRs run checks without publishing;
main releases run Semgrep SAST, Gitleaks, dependency scans, PostgreSQL tests,
migration checks, image build/scanning, and a final infra image-file commit.

The locked Python 3.12 image contains API, worker and migrations. Production
installs only runtime dependencies. For development and tests:

```bash
uv sync --frozen --extra dev
uv run --frozen pytest
docker build -t womanup-backend:local .
```

Set TEST_DATABASE_URL to PostgreSQL with pgvector for database tests.
CI fails database initialization errors instead of skipping. CI uses a separate
fresh database for the complete Alembic chain.

The permanent version is backend-<full-source-sha>; infra records the image digest
and readable version. Existing versions are reused. Only successfully scanned
images are promoted. Re-run a failed deploy job to retry an infra Git push; stale
source builds are skipped. Candidate tags from failed scans are never deployed.

Production mode, database credentials, JWT/Firebase/provider secrets, worker
heartbeat and migration sequencing are supplied by infra. Do not run demo seeds
in production. See infra's README for prod configuration, bootstrap and recovery.
