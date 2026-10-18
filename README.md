# LAAIhacks

Smart glasses that project websites and run Fetch.ai agents to act on what you're looking at.

## Structure

```
glasses-web-reader/  G2 glasses app (Even Hub SDK) + condense server; voice, action popups
apps/
  glasses/        G2 session API contract (INTEGRATION.md)
  dashboard/      Focal Studio web dashboard (prototype/)
  public-info/    Public homepage and privacy pages for Google OAuth branding
services/
  orchestrator/   FastAPI backend: chat, sessions and approvals, Supabase persistence
  agents/         Fetch.ai uAgents: price, reviews, news, YouTube, Spotify, iMessage, coordinator
packages/
  shared/         OpenAPI schema shared by clients
supabase/         Migrations and database tests
demo/             Demo script and sample pages
```

## Deploy

- `apps/dashboard`: frontend host (e.g. Vercel)
- `services/orchestrator`: backend host (e.g. Railway, Render, Fly.io)
- `services/agents`: Fetch.ai Agentverse

## Backend

The [orchestrator](services/orchestrator/README.md) runs on port 8001, predicts tasks, checks
permissions, calls the agents on port 8000, and saves settings and task history in **Supabase**.
See the [implementation plan](services/orchestrator/PLAN.md) and
[database migration](supabase/migrations/202610170001_orchestrator.sql) for setup.
