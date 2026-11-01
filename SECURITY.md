# WomanUP security: operations, monitoring, incident response

This file is for the people who run the platform. The code's own security
behaviour is documented beside the code; `DEPLOYMENT.md` lists what the API
insists on before it starts. What follows is what has to happen *around* the
code: what to watch, what to do when something goes wrong, and what the
platform keeps about the women who use it.

## 1. What the platform holds, and for how long

| Data | Where | Why | Kept |
| --- | --- | --- | --- |
| Address, phone, password hash (argon2), Firebase uid | `users` | sign-in | until erasure: identifiers nulled, row kept anonymous |
| Name, date of birth, district, education, profession, skills, interests, bio, avatar URL | `profiles` | personalisation | until erasure: name, birth date, bio, avatar nulled |
| Marital status, children, disability, register membership | `profiles` (sensitive block) | eligibility | never leaves the platform; only her own `/me/profile` returns it |
| Diagnostic answers and scores, plans, enrolments, certificates, portfolio | own tables | the product | kept; identifiers live only on `users`/`profiles` |
| Assistant prompt and answer excerpts | `ai_interactions` | tracing a recommendation | text blanked after `AI_TRACE_RETENTION_DAYS` (180) and at erasure; trace metadata kept |
| One-time codes (HMAC only), sign-in throttle buckets (HMAC only), revoked token ids | `otp_challenges`, `auth_throttle`, `revoked_tokens` | sign-in safety | swept hourly by the worker (`services.retention`) |
| Consent and audit rows (with IP and user agent) | `consent_logs`, `audit_logs` | accountability | append-only, kept |
| Analytics: hashed visitor cookie, session id, device family, route template, event name | `page_views`, `analytics_events` | product figures | no IP, no user agent string, no query strings, no text |

Logs are JSON with addresses and phone numbers scrubbed (`core.logging`).
Nothing logs a password, a code, a token or a request body.

Erasure (`DELETE /users/me`): identifiers, name, birth date, bio, avatar and
Firebase uid are cleared, the account is marked deleted, her assistant text
and pending codes are removed, and every token she holds stops working at the
next request. Aggregate figures keep counting her anonymously. Backups taken
before the erasure still contain her data until they age out (seven nightly
dumps, see §3) — a restore from such a dump must be followed by re-running
erasures made since, which the audit log (`user.anonymise`) lists.

## 2. Security events worth an alert

The API writes security events to the `security` logger as JSON lines with an
`event` field. Forward that logger to the alerting of your choice and start
with these rules:

| Event | Meaning | Alert when |
| --- | --- | --- |
| `auth.login_failed` with `endpoint=staff_login` | wrong password on a desk account | more than 5 in 10 minutes from any address, or any on an administrator's login |
| `auth.throttled` | an address hit a sign-in or registration ceiling | more than 3 per hour |
| `auth.revoked_token_used` | a signed-out or spent token presented again | any, repeated for one `user_id` |
| `auth.refresh_replayed` | a spent refresh token exchanged again | any for a desk account |
| `auth.blocked_account_used` | a suspended or deleted account's token used | any |
| `auth.stale_role_claim` | a token claiming a role the database no longer grants | any (expected briefly after a revocation; repeated means a kept token) |
| `admin.role_changed` | a role granted or revoked | every one, to the administrators' channel |
| `ai.limit_reached` | the hourly model allowance exhausted | more than 20 distinct keys per hour (an automated spend) |
| `api.rate_limited` | the per-address minute ceiling | sustained from one address: block it at the edge |
| `integration.auth_failed` | a partner callback with a wrong id or signature | any |

Beside the events, three signals come from the platform's own records: the
`audit_logs` table (every role change, export, staff read of personal data
and consent change), `/health/ready` failing (database unreachable) and the
worker heartbeat file going stale (compose health check).

## 3. Backups and recovery

Backups are the infrastructure repository's job (`scripts/backup.sh`): a
verified `pg_dump` every night and before every migration, kept seven days
and never fewer than three, mode 0600 under `/opt/womanup/backups` on the
server. Restore is `scripts/restore.sh … RESTORE`, which takes one more backup
first and refuses an unreadable dump.

What this does not yet do, and must before launch: keep a copy **off the
server**. A single host holds the database and its only backups; a lost disk
or a compromised root loses both. Ship each dump encrypted (`age` or GPG to an
operator's public key) to object storage in another provider or region, with
the same retention. Test a restore into a scratch database at least once
before launch and after every major schema change, and write the date and the
outcome into the infra repository.

## 4. Incident response

Roles: one incident lead (decides and communicates), one engineer on the
server, one engineer on the application. Keep a private channel for the
incident and a timeline from the first minute.

**Step 0 — contain fast, without destroying evidence.** Snapshot the server
(provider snapshot) and copy the current logs before changing anything.

### A. A woman's account is compromised
1. Suspend the account (`status=suspended` by an administrator); every token she holds fails on the next request.
2. Review `audit_logs` and the `security` events for her `user_id`.
3. Reset: she signs in again by password/Google; if her mailbox is compromised, verify by another channel first.
4. Tell her what was seen and when (plain language, in her language).

### B. An administrator or coordinator account is compromised
1. Revoke the role (`DELETE /admin/users/{id}/roles/{role}`) **and** suspend the account — the role check is live, so access ends at once.
2. Rotate `JWT_SECRET_KEY` in the prod environment and redeploy: every session on the platform ends. Announce the sign-out to users.
3. Read every `admin.*`, `role.*`, `users.export`, `user.view`, `user.profile_view` audit row for the period; reverse unauthorised role grants; list exported data.
4. Check the desk's mailbox and device; re-issue the password with `create_staff --update`.

### C. The database or the server is compromised
1. Isolate: at the edge (Cloudflare) put the site in "under attack" / maintenance; on the host, block all inbound except the operator's address.
2. Rotate everything the host could see: `POSTGRES_PASSWORD`, `JWT_SECRET_KEY`, `ANTHROPIC_API_KEY`, SMTP, partner secrets, GHCR token, the deploy SSH key. Revoke the Firebase service account key and issue a new one.
3. Rebuild the host from `bootstrap.sh` on a fresh VM rather than cleaning the old one; restore the last trusted dump; re-run erasures made since (audit `user.anonymise`).
4. Assume personal data was read. Notify the data protection authority and the affected women as the law requires; keep the timeline.

### D. A credential leaked (key in a log, a paste, a commit)
1. Rotate it immediately — the Anthropic, Firebase, SMTP and partner consoles each issue a new value; update the prod environment and redeploy.
2. For a leaked `JWT_SECRET_KEY`: rotate; all sessions end. For a leaked database password: rotate **and** check `pg_stat_activity` and the provider's firewall logs for foreign connections.
3. Search the git history of all three repositories for the value; if committed, rewrite history and force the re-clone, then rotate again.
4. Check the provider's usage/billing for the leaked key's period.

### E. The assistant disclosed something it should not have
1. Capture the `trace_id` from the reply; read the `ai_interactions` row and the retrieved sources.
2. If the source is a knowledge document: unapprove it (`DELETE /ai/knowledge/{id}` or re-edit) — retrieval only reads approved documents.
3. If the model repeated private data from a persona: check what `build_persona` sent; the fix is in code and ships as a release.
4. If the disclosure concerns a specific woman, treat it as case A for her.

### F. Denial of service
1. At the edge: enable Cloudflare's "under attack" mode and rate-limiting rules for `/api/v1/auth/*`, `/api/v1/assistant/*`, `/api/v1/vacancies*`.
2. On the host: the API's own ceilings (`RATE_LIMIT_PER_MINUTE`, `EXPENSIVE_RATE_LIMIT_PER_MINUTE`) can be lowered without a release; the worker and the database are on an internal network and are not reachable.
3. Confirm the origin accepts traffic only from Cloudflare's address ranges (ufw rules); if the origin address is known to the attacker, move it.

### G. Malware or a tampered image
1. Images are deployed by digest from GHCR after a Trivy scan; compare the running digest with `apps/*/image.env` in infra.
2. If they differ, redeploy the recorded digest and rotate the GHCR and infra tokens.
3. If the host itself is suspect, go to C.

**After every incident:** a written review within a week — what happened, how it was noticed, what took longest, which control was missing — and at least one change in code, configuration or this document.

## 4b. Sessions, as they are now

The access token (30 min) lives in the page's memory only; the refresh token
(30 days) is an HttpOnly, SameSite cookie scoped to `/api/v1/auth` that no
script can read. Every refresh spends the token and issues a new cookie; a
token presented twice ends every session of the account (the account's
session generation is bumped), as does `POST /auth/logout-all`, which a woman
can use when she left herself signed in on a shared device. Signing out
revokes both tokens and clears the cookie. A password door holds an account
after too many wrong guesses from anywhere, with the same answer as a wrong
password. The site's Content Security Policy allows scripts only with the
response's own nonce, so an injected script does not run.

## 5. Pre-launch checklist

Infrastructure (needs access to Cloudflare, the VPS and the registrar):
- [ ] Origin firewall accepts 80/443 only from Cloudflare's published address ranges; SSH from operator addresses only, keys only, non-default port; `fail2ban` or equivalent on SSH.
- [ ] Cloudflare: WAF managed rules on, rate-limiting rules for the auth, assistant and vacancies paths, bot fight mode, "Full (strict)" TLS to the origin, minimum TLS 1.2.
- [ ] HSTS at the edge with `includeSubDomains` (the Caddyfile sets a bare `max-age`); consider the preload list once stable.
- [ ] DNS: SPF (`v=spf1 … -all` for the SMTP provider), DKIM for the sending domain, DMARC (`p=quarantine` then `p=reject`, with a reporting address), CAA for the certificate authorities in use (Cloudflare's: `pki.goog`, `letsencrypt.org`), and an MX or a null MX so the domain cannot be used for mail it does not send.
- [ ] Registrar and Cloudflare accounts on hardware-key or app-based two-factor authentication; registry lock if offered.
- [ ] Off-server encrypted backups and one rehearsed restore (§3).
- [ ] Log forwarding for the `security` logger and the alert rules in §2.

Platform (settings in the prod environment):
- [ ] `JWT_SECRET_KEY` ≥ 48 random characters; `POSTGRES_PASSWORD` random; `ANTHROPIC_API_KEY` set; SMTP configured so codes and notices can be sent.
- [ ] `AI_USER_HOURLY_LIMIT`, `AI_GUEST_IP_HOURLY_LIMIT`, `RATE_LIMIT_PER_MINUTE` reviewed against expected traffic.
- [ ] Every partner integration that goes live has a `*_CLIENT_SECRET` so its callbacks are signed.
- [ ] The demo account and the first administrator carry fresh passwords (the demo password once lived in the frontend's source history).
- [ ] GHCR package write access limited to the CI identities; infra `prod` environment restricted to `main`; the three `INFRA_PUSH_TOKEN`s are fine-grained and expire.

People:
- [ ] Two named administrators at most, each with their own account; no shared desk logins.
- [ ] This document read by whoever is on call for launch week.
