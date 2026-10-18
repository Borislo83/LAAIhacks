# Reader and LensOS Merge Implementation Plan

**Goal:** Keep the reader functionality and integrate durable orchestrator sessions,
AI recommendations and LensOS review/approval controls.

**Architecture:** Existing reader plus isolated gateway, typed backend client,
session coordinator and LensOS review cards. Backend data stays in Supabase.

**Tech Stack:** Existing TypeScript, Hono, Vite, Even Hub SDK, Node test runner.

**Spec:** ../specs/2026-10-17-lensos-merge-design.md

## Constraints and review focus

Reader behavior remains authoritative. No secret persistence or service-role
substitution; credentials cannot follow redirects. Backend failure cannot erase a
readable page. Test stale navigation, reconnect ownership failures, expired tokens,
unknown dispatch results and external proposal revisions during confirmation.

## Tasks

- [x] 1. Gateway and client: server/orchestrator.ts, src/lensos/client.ts,
  src/lensos/types.ts, src/lensos/page.ts. Add tests for allowed routes, caller
  auth, forbidden redirects, schema-compatible session creation and bounded page
  conversion. Run failing tests, implement, rerun `npm test`.
- [x] 2. Session coordinator and review: src/lensos/session.ts and LensOS-derived
  action-cards.ts. Test serialized ingestion, stale-page exclusion, reconnect,
  preferences retention, uncertain dispatch polling and stable approval retries.
  Run failing tests, implement, rerun `npm test`.
- [x] 3. Reader integration: src/main.ts and src/companion.ts plus a dedicated
  backend panel. Preserve reader gestures outside action review. Add navigation,
  connection, preferences, current recommendations and persisted history UI.
  Check `npm run typecheck`, `npm run build` and UI interaction tests.
- [x] 4. Run instructions and review: .env.example, README.md, app.json and merge
  notes. Run the complete suite/build and a separate reviewer pass. Resolve
  important findings and record evidence and limitations.

## Evidence

Gateway/client/session/review/panel implemented. Initial production build and
13-test suite passed. Independent reviewer found four defects; fixes and
regressions are present. Current typecheck passes. Isolated browser check passed
reader, cancel, confirmation, execution result/history and reconnect (revision
remained 2, contexts remained 1, dispatches remained 1 across reconnect).

Final automated rerun passed: all 16 tests and the production build succeeded.
Elevated execution was required because tsx failed Windows user-info lookup in
the sandbox and Vite/esbuild could not traverse the Windows parent path there.
The final race regressions were reproduced failing before the fixes and passed
in this rerun.
See ../../MERGE.md for setup and explicit limits.
