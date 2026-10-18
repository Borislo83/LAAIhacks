# Reader and LensOS merge

The user approved using glasses-web-reader for all reading behavior (Claude web
reinterpretation, streaming decks, image processing, MangaDex, navigation, recent
pages and bridge persistence), LensOS for action review/confirmation, and the
existing sibling LAAIhacks orchestrator for Supabase persistence and predictions.

The merged application lives in glasses-web-reader. The standalone LensOS source
and sibling orchestrator/database remain intact. This is a source archive, not a
Git checkout; no branch or commit operation is available here.

## Integration decisions

- Add a restricted same-origin gateway to the configured orchestrator, default
  http://127.0.0.1:8001. Forward caller bearer credentials, never service keys.
- Use the actual orchestrator session-create body `{}` (LensOS's create payload
  is incompatible). Keep its revisioned display/approval contract.
- Connect with a web access token, plus an optional G2 token for the same user.
  Credentials stay in memory. Save only the session ID through SDK storage and
  revalidate access on reconnect. A token with both roles can serve both uses.
- Submit extracted page summaries to owned sessions. Submit bounded page context
  to /v1/contexts for persisted predictions, respecting saved proactive_enabled.
- Keep predicted tasks separate from external session actions. Tasks use explicit
  /dispatch confirmation and polling after uncertain results; external actions
  use revision checks and retry the identical decision request ID.
- Reuse LensOS's paginated action review on glasses. Phone controls operate the
  same review flow, including cancel/back. Reader remains usable disconnected.
- Offer saved density, shopping priority and proactive preferences. Read the full
  record before updating; retain private notes without sending them to models.
- Restore task history from the backend and session display on reconnect. Never
  overwrite a newer page with a late response or show old-page proposals as current.

## Verification

Test gateway allowlisting/auth forwarding, real request payload compatibility,
summary/context bounds, cancellation/navigation races, task uncertainty and LensOS
decision retries. Run TypeScript checking, production build, local integration
tests and browser UI checks with controlled API fixtures. Do not execute live
agents or modify the production database to test the merge. Live credentials,
hardware and an actual agent worker may remain external acceptance requirements.
