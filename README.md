# LAAIhacks

Smart glasses that project websites and run Fetch.ai agents to act on what you're looking at.

## Structure

```
apps/
  glasses/        Glasses UI/UX, head tracking, website projection client
  dashboard/      PaperOS web UI to customise glasses and manage permissions
services/
  orchestrator/   Backend orchestrator: task prediction, agent dispatch, persistence
  agents/         Fetch.ai uAgents (price compare, competitor redirect, reviews)
packages/
  shared/         Shared types and schemas
demo/             Demo flows, scripts, video assets
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
