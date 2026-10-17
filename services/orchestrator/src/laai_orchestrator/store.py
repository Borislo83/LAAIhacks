from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException

from .config import Config
from .models import PageContext, Task, UserSettings


class Store:
    """Supabase REST adapter. Every operation is scoped to a verified Auth user.

    The backend secret bypasses RLS; never accept user_id from an API request body.
    Settings initialization and context/task creation are transactional database RPCs.
    """

    def __init__(self, config: Config, http: httpx.AsyncClient):
        self.http = http
        self.base = str(config.supabase_url).rstrip("/") + "/rest/v1/"
        key = config.supabase_secret_key.get_secret_value()
        self.headers = {"apikey": key, "Prefer": "return=representation"}
        if key.startswith("eyJ"):  # Legacy service_role JWTs are also supported.
            self.headers["Authorization"] = f"Bearer {key}"

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self.http.request(
                method, self.base + path, headers=self.headers | kwargs.pop("headers", {}), **kwargs
            )
            if response.is_error:
                try:
                    code = response.json().get("code")
                except ValueError:
                    code = None
                errors = {
                    "PT403": (403, "Agent disabled or confirmation required"),
                    "PT404": (404, "Task not found"),
                    "PT409": (409, "Task already dispatched"),
                }
                if code in errors:
                    status, message = errors[code]
                    raise HTTPException(status, message)
                response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Never return raw upstream errors, request headers, or tokens.
            raise HTTPException(503, "Supabase persistence unavailable") from exc

    async def settings(self, user_id: UUID) -> UserSettings:
        row = await self.request("POST", "rpc/laai_get_settings", json={"p_user_id": str(user_id)})
        return UserSettings.model_validate({k: row[k] for k in UserSettings.model_fields})

    async def save_settings(self, user_id: UUID, settings: UserSettings) -> UserSettings:
        await self.request(
            "POST",
            "laai_settings",
            params={"on_conflict": "user_id"},
            # Merge only after an explicit settings replacement, never on a read.
            json={"user_id": str(user_id), **settings.model_dump(mode="json")},
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
        )
        return settings

    async def save_context(
        self, user_id: UUID, page: PageContext, source: str, tasks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            "rpc/laai_create_context",
            json={
                "p_user_id": str(user_id),
                "p_page": page.model_dump(mode="json"),
                "p_source": source,
                "p_tasks": tasks,
            },
        )

    async def tasks(self, user_id: UUID, limit: int, offset: int) -> list[Task]:
        rows = await self.request(
            "GET",
            "laai_tasks",
            params={
                "user_id": f"eq.{user_id}",
                "order": "created_at.desc,id.desc",
                "limit": limit,
                "offset": offset,
            },
        )
        return [Task.model_validate(row) for row in rows]

    async def task(self, user_id: UUID, task_id: UUID) -> Task:
        rows = await self.request(
            "GET",
            "laai_tasks",
            params={
                "user_id": f"eq.{user_id}",
                "id": f"eq.{task_id}",
                "limit": 1,
            },
        )
        if not rows:
            raise HTTPException(404, "Task not found")
        return Task.model_validate(rows[0])

    async def claim(self, user_id: UUID, task_id: UUID, confirmed: bool) -> Task:
        row = await self.request(
            "POST",
            "rpc/laai_claim_task",
            json={
                "p_user_id": str(user_id),
                "p_task_id": str(task_id),
                "p_confirmed": confirmed,
            },
        )
        return Task.model_validate(row)

    async def finish(
        self, user_id: UUID, task_id: UUID, result: dict[str, Any] | None, error: str | None
    ) -> Task:
        row = await self.request(
            "POST",
            "rpc/laai_finish_task",
            json={
                "p_user_id": str(user_id),
                "p_task_id": str(task_id),
                "p_result": result,
                "p_error": error,
            },
        )
        return Task.model_validate(row)
