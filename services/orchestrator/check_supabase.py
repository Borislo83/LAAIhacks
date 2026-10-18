"""Read-only deployment check. Never prints credentials or database contents."""

import asyncio

import httpx
from pydantic import ValidationError

from laai_orchestrator.config import Config


async def check() -> int:
    try:
        config = Config()
    except ValidationError:
        print("Set SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, and SUPABASE_SECRET_KEY in .env.")
        return 1
    base = str(config.supabase_url).rstrip("/")
    secret = config.supabase_secret_key.get_secret_value()
    headers = {"apikey": secret}
    if secret.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {secret}"
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.get(
                base + "/auth/v1/settings",
                headers={
                    "apikey": config.supabase_publishable_key.get_secret_value(),
                },
            )
            response.raise_for_status()
            for table, column in [
                ("laai_settings", "user_id"),
                ("laai_contexts", "user_id"),
                ("laai_tasks", "user_id"),
                ("laai_sessions", "user_id"),
                ("laai_pages", "session_id"),
                ("laai_actions", "session_id"),
                ("laai_approval_events", "session_id"),
                ("laai_idempotency_keys", "session_id"),
                ("laai_user_preferences", "user_id"),
            ]:
                response = await http.get(
                    base + "/rest/v1/" + table,
                    headers=headers,
                    params={"select": column, "limit": 0},
                )
                if response.is_error:
                    print(f"Database check failed for {table} (HTTP {response.status_code}).")
                    print(
                        "Check the server secret, apply the migration, and reload the API schema."
                    )
                    return 1
            # Migration 004 adds this pure helper; calling it reads and writes nothing.
            response = await http.post(
                base + "/rest/v1/rpc/laai_uuid_or_null", headers=headers, json={"p": "x"}
            )
            if response.is_error:
                print("Workflow v1.1 (migration 004) is not applied yet.")
                return 1
    except httpx.HTTPError:
        print("Supabase connection check failed. Check project URL, keys, and network access.")
        return 1
    print(
        "Supabase Auth and all orchestrator/workflow/preferences tables are reachable. No data was changed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(check()))
