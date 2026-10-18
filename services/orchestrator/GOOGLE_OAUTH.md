# Google OAuth handoff

The dashboard uses Supabase Google OAuth with PKCE. “Continue with Google” returns to
`location.origin + location.pathname`; the SDK exchanges the code and restores the session.
The orchestrator verifies the Supabase access token before accessing user data. Email sign-in
remains available under “Use email instead”. Client credentials live only in Supabase/Google.

## Live configuration (2026-10-17)

- Supabase project: `hjdogsjwgnbdlxcspkaf` (LAAIHACKS). Google provider enabled.
- Google Cloud project: `laaihacks-focal` (LAAIHACKS Focal).
- OAuth app: Focal LAAIHACKS; web client: Focal Supabase Google login.
- Callback: `https://hjdogsjwgnbdlxcspkaf.supabase.co/auth/v1/callback`.
- Saved dashboard redirects: `http://localhost:5173/dashboard.html` and
  `http://127.0.0.1:5173/dashboard.html`.
- Nonce checking remains enabled; email is required. Identity scopes only.
- Google audience is External, **Testing**. `borislo830@gmail.com` is an allowed test user.
  Add teammates under Google Auth Platform → Audience → Test users. Complete the Branding
  requirements and publish the Google app when broader access is needed.
- Add the exact hosted dashboard URL to Supabase before deploying; preserve PKCE callback origin.

## Verification

The real Supabase SDK browser test verified Google authorization URL generation, PKCE
challenge/verifier matching, callback code exchange, authenticated preference loading,
reload, sign-out and email fallback using fixture responses. Live Supabase settings report
Google enabled, and its authorization endpoint returns a 302 to `accounts.google.com`.

[Supabase Google OAuth documentation](https://supabase.com/docs/guides/auth/social-login/auth-google).

Live end-to-end verification: Google account selection and identity-only consent returned to
`http://localhost:5173/dashboard.html`, signed in as the configured test account, with
“Preferences loaded.” and no authentication error.
