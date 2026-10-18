# LAAIHACKS — DB, orchestrator and authentication handoff

## Live database

Supabase project `hjdogsjwgnbdlxcspkaf`. Migrations `002_shared_workflow` and
`003_user_preferences` were applied through its browser SQL editor on 2026-10-17.
Both succeeded; the read-only `services/orchestrator/check_supabase.py` verifies Auth and
all expected tables. Do not rerun the non-idempotent migrations on this project.

The backend persists owned sessions/pages/actions, approval events, idempotent responses,
execution results, preferences and up to 10,000 characters of private notes. Page changes
invalidate old actions. Approval and event creation commit together. Terminal results mark
events consumed; polling remains at-least-once. Notes are never sent to agent/model prompts.

## Dashboard and Google login

Dashboard: `http://localhost:5173/dashboard.html`; orchestrator: port 8001; agents: port 8000.
Google OAuth is enabled in Supabase and uses PKCE. Google Cloud project `laaihacks-focal`
contains the dedicated client. Both localhost and loopback dashboard redirects are allowlisted.
Google is in **Testing** mode with `borislo830@gmail.com` allowed. Add teammate emails in
Google Auth Platform → Audience → Test users; broader access requires completing Branding
and publishing the Google app. Credentials stay out of the repository.

## Validation and next handoff

Isolated feature validation: 26 API tests, 17 database tests, and browser auth/preferences
checks passed. The real Supabase SDK Google fixture test also passed PKCE callback exchange,
restored preferences, reload, sign-out and email fallback. Live Supabase table/provider checks
pass and authorization redirects to Google.

Next: run the shared session API against G2 with role-scoped tokens for the same user.
Ordinary users have `web`; trusted server-managed `app_metadata.laai_roles` grants `g2`,
`agent`, or `executor`. Never accept these roles from user-editable metadata.
See [workflow and persistence details](services/orchestrator/WORKFLOW.md) and
[Google OAuth configuration](services/orchestrator/GOOGLE_OAUTH.md).

Feature commits: `c7ea823` authentication, `6cb3a0d` persistent shared workflow,
`e0b3c68` preferences/notes; Google OAuth is committed separately.

Live end-to-end verification: Google account selection and identity-only consent returned to
`http://localhost:5173/dashboard.html`, signed in as the configured test account, with
“Preferences loaded.” and no authentication error.
