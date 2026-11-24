# Security policy

## Reporting a vulnerability

Please report security problems privately through
[GitHub private vulnerability reporting](https://github.com/syedhassan13/leadpilot-dealcloser/security/advisories/new).
Don't open a public issue for a vulnerability.

We aim to reply within 3 days. Please include what you found, how to reproduce it, and what data could be affected.
Only test against your own accounts and data. No load tests, spam or real-customer data.

## Who acts if something goes wrong

The project owner (GitHub `syedhassan13`) handles reports. If customer data is exposed we will:
1. Stop the leak: rotate the affected key or password, or take the API offline on Render.
2. Find out which customers were affected, using the security event log in the `dc_audit_log` table.
3. Tell the affected dealers within 72 hours. They tell their customers.

## If a key leaks

| Key | Where to revoke and replace | Where to update |
|---|---|---|
| `ANTHROPIC_API_KEY` | Ask the instructor for a new group key (the old one is disabled) | Render → leadpilot-api → Environment, then redeploy |
| Supabase database password | Supabase → Project Settings → Database → Reset password | `DATABASE_URL` on Render, then redeploy |
| `ADMIN_TOKENS` / `SESSION_SECRET` / `CRON_SECRET` | Generate new random values | Render → Environment (and the `CRON_SECRET` GitHub secret), then redeploy. Changing `SESSION_SECRET` logs everyone out. |
| Render / Vercel / Supabase access tokens | Each dashboard's account → tokens page | Local `.env` only |

After rotating, check the Anthropic usage and Supabase logs for activity you don't recognise.
