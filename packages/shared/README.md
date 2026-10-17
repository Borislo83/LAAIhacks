# Shared

Message types and schemas shared by every part: tasks, permissions, settings, glasses display and input events.

[`openapi.json`](openapi.json) is the generated orchestrator API contract, including page inputs,
tasks, settings, authentication, and responses. Clients can generate language-specific types from
this file. Agent wire models remain in `services/agents/src/laai_agents/models.py`.

Regenerate after changing backend routes or models:

```bash
cd services/orchestrator
uv run python export_schema.py
```

Device display/input events beyond REST will be added with the glasses client.
