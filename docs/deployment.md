# Deploying Course Consultant

Course Consultant runs as one Python function on Vercel, with Neon Postgres
for storage and Neon Auth for email sign-in. There is no build step, cron job,
Blob store or background worker.

## 1. Vercel project
1. Import this repository at its root (framework: Flask). `vercel.json` and
   `pyproject.toml` already point at `server.py`.
2. Python 3.12. Any Vercel plan works (the app needs no cron).

## 2. Neon
1. Create a Neon project. Copy the **pooled** connection string
   (`DATABASE_URL`). Tables are created automatically on first use.
2. Enable **Neon Auth** on the same branch and copy its **Auth Base URL**
   (`NEON_AUTH_BASE_URL`).
3. Set up your own email sender (next section) before inviting people.

## 3. Your own email sender (SMTP) — do this before inviting instructors
Neon Auth sends the six-digit sign-in codes. By default it uses Neon's shared
sender, which is rate-limited and sends from a Neon address, so codes can be
slow, land in spam, or stop arriving when several people sign in at once. Use
your own SMTP service instead.

**What you need from an email provider** (for example your institution's mail
relay, Amazon SES, Postmark, SendGrid, Mailgun or Resend): SMTP host, port
(usually 587 with STARTTLS, or 465 with TLS), username, password (often an API
key), and a sender address on a domain you control, such as
`consultant@yourcollege.edu`. Verify that domain with the provider (SPF and
DKIM DNS records) so messages are not marked as spam.

**In the Neon Console:**
1. Open your project, then **Auth** (on some accounts: **Settings → Auth**).
2. Open the **Configuration** tab and find **Email server**.
3. Switch from **Shared** to **Custom SMTP**.
4. Enter the host, port, username, password, sender email and sender name
   (for example "Course Consultant"). Save.
5. Click **Send test email** and confirm it arrives (check spam too).
6. Sign in to Course Consultant with a new address to confirm the code arrives
   from your sender.

Notes:
- The SMTP password is stored by Neon, not by Course Consultant; it never goes
  in Vercel's environment variables or this repository.
- A custom sender changes the **From** address; Neon's message wording stays
  the same. Custom templates require Neon Auth webhooks, which this app does
  not use.
- Neon can also be configured through its API (`PATCH …/auth/email_provider`)
  or CLI (`neon neon-auth config email-provider update`). That call replaces
  the whole email configuration, so send every field.

## 4. Environment variables (Vercel → Settings → Environment Variables)
| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Neon pooled connection URL |
| `NEON_AUTH_BASE_URL` | Neon Auth Base URL |
| `HAX_APP_ORIGIN` | Exact production origin, `https://…`, no trailing slash |
| `HAX_SESSION_SECRET` | Random, 32+ characters; signs browser identities and links |
| `HAX_BETA_ACCESS_CODE` | Invitation code new users enter once (32+ characters, different from the session secret) |
| `HAX_MENTOR_ENCRYPTION_KEY` | Fernet key encrypting saved AI keys; keep it, or saved keys become unreadable |
| `HAX_AUTH_MODE` | Optional: `neon` (default, email accounts) or `browser-beta` |
| `HAX_ALLOWED_AI_HOSTS` | Optional: reviewed institutional AI gateway hosts |
| `HAX_CANVAS_INSTITUTIONS` + one secret per institution | Optional: institutional Canvas connection (see `python/canvas_oauth.py`) |
| `HAX_CDN_URL` | Optional: where HAX's web components load from (default `https://cdn.hax.cloud/cdn/`) |

Generate a starting set without printing secrets:

```bash
pip install -r requirements.txt
python3 scripts/configure_hosted.py          # writes .env.local (owner-only); never commit it
python3 scripts/check_hosted.py              # offline shape check
python3 scripts/check_hosted.py --live       # also connects to the database
```

Variables from older versions (`HAX_UPLOAD_SIGNING_SECRET`,
`HAX_INTERNAL_SECRET`, `CRON_SECRET`, `BLOB_READ_WRITE_TOKEN`) are no longer
used; you can delete them, and the Blob store, from the Vercel project.

## 5. Callbacks to register
- OpenRouter (PKCE, nothing to register): `https://<origin>/mentor/openrouter/callback`
- Canvas developer key redirect URI: `https://<origin>/mentor/canvas/callback`

## 6. Smoke test after each deployment
1. `/healthz` returns `{"configured": true}`.
2. `/mentor`: enter the invitation code, sign in with an emailed code (from
   your own sender), create a course workspace.
3. Connect an AI model, import a small course, run one consultation turn.
4. Open the course site preview and a share link in a private window.
5. Sign in from a second browser with the same account: the work is there.
   A different account must not see it.
