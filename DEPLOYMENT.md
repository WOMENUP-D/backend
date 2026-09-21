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

## The first administrator

A fresh installation has a working `/admin` and nobody who can open it: the
only thing that ever created an administrator was the demonstration seed, and
the seed refuses to run against production. Open the first desk account on the
server itself, once:

```bash
sudo bash -c 'cd /opt/womanup/current && source scripts/common.sh /opt/womanup &&
  c run --rm --no-deps migrate python -m app.create_staff \
    --email admin@example.uz --name "Ism Familiya"'
```

It asks for the password twice on a hidden prompt, so the password never
reaches the shell history, `ps` or the deployment log. A non-interactive
console can pass it in `WOMANUP_STAFF_PASSWORD` instead.

`--role` grants `regional_coordinator`, `moderator` or `trainer` instead of
`admin`, and a coordinator takes `--region`. The address may already belong to
someone who registered as a learner — `--update` then promotes that account and
sets a new password rather than opening a second WomanUP ID. Every grant is
written to the audit log.
