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

## Security configuration the API insists on

Outside `ENVIRONMENT=local` the process refuses to start unless
`JWT_SECRET_KEY` is set to at least 32 random characters and `DEBUG` is off;
the error names the setting and never its value.

Sessions: the refresh token is an HttpOnly cookie (`AUTH_COOKIE_NAME`, default
`wu_refresh`) scoped to `/api/v1/auth`, Secure outside local development,
SameSite Lax — a script in the page can never read it, and the page keeps the
thirty-minute access token in memory only. The site and the API must share an
origin for this (they do, behind Caddy; in development Next proxies `/api`).
Every refresh token is spent on use (`revoked_tokens`, revision 0036); a replay
ends every session of the account by bumping `users.session_generation`
(revision 0037), as does `POST /auth/logout-all`. Staff authority is re-read
from `user_roles` on every request: revoking a role or suspending an account
takes effect at once, not when the token expires. A password door holds an
account after `LOGIN_FAILURES_PER_HOUR` wrong guesses (`STAFF_LOGIN_FAILURES_PER_HOUR`
for desks) whatever addresses they come from. Set `ALLOWED_HOSTS` to the
site's host names so a forged `Host` header is refused before routing.

Partner callbacks (`/integrations/{system}/…`) carry `X-Client-Id` and, when
`*_CLIENT_SECRET` is configured for the system, `X-Signature: sha256=<hex
HMAC-SHA256 of the raw body under that secret>`. A partner may only decide
applications to its own listings and report outcomes for women it knows
through an application or a consent.

Model-backed answers are capped per account (`AI_USER_HOURLY_LIMIT`) and per
anonymous address (`AI_GUEST_IP_HOURLY_LIMIT`); every address is held to
`RATE_LIMIT_PER_MINUTE` requests a minute and `EXPENSIVE_RATE_LIMIT_PER_MINUTE`
on the job-board and model paths (per worker — the edge's own rate limiting
stands in front); request bodies above `MAX_REQUEST_BODY_BYTES` are refused.
The worker blanks assistant excerpts after `AI_TRACE_RETENTION_DAYS` and drops
spent codes after `OTP_RETENTION_HOURS`. Security events go to the `security`
logger; the alert rules, the incident plan and the pre-launch checklist are in
`SECURITY.md`. Every response carries `nosniff`,
`X-Frame-Options: DENY`, a `no-referrer` policy and `Cache-Control: no-store`
on API paths; HSTS is added in production. The OpenAPI schema and the docs
are served only outside production.

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

## Analytics

Revision `0029` adds product analytics: a browser session, referrer host and
device family on `page_views`, an `analytics_events` stream and a
`visitor_presence` table. The release's migration step applies it like any
other; nothing is backfilled, because nothing can be. Every figure that depends
on the new columns or on events says on the dashboard since which day it has
data, and is reported as "not recorded" — never as 0 — before that day.
Registrations, completions, diagnostics and the like are counted from their own
tables and reach back as far as the records do.

What is stored: a hashed first-party visitor cookie, a random session id, the
referrer's host, three words from the User-Agent (device, browser, OS family),
a route with its ids folded out, an event name from a fixed list and a few
short scalar properties. Not stored: IP addresses, user-agent strings, query
strings, search text, answers, scores, messages. Staff accounts' own browsing
is flagged and left out of every figure by default.

The management panel is `/admin` (sign-in at `/admin/login`); its Analytics
screens are for administrators and moderators. A tester keeps her own browsing
out of the figures with `localStorage.setItem("womanup.analytics", "off")` in
the browser console.

No new environment variables. The in-process throttles on `/analytics/*` and
the one-minute answer cache are per worker, like the sign-in throttles; move
them to Redis when the API runs more than one worker.
