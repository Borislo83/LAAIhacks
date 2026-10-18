from laai_agents.memory import MemoryRequest, MemoryResponse, build_memory_agent


class Storage:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value


class Ctx:
    def __init__(self):
        self.storage = Storage()


async def test_rest_route_saves_then_recalls(monkeypatch):
    for name in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "FOCAL_MEMORY_USER_ID"):
        monkeypatch.delenv(name, raising=False)
    route = build_memory_agent()._rest_handlers[("POST", "/memory")]
    ctx = Ctx()
    saved = await route(ctx, MemoryRequest(user="dashboard:u1", remember={"note": "prefers Sony"}))
    assert isinstance(saved, MemoryResponse) and saved.response["entries"] == 1
    recalled = await route(ctx, MemoryRequest(user="dashboard:u1"))
    assert recalled.history[0]["note"] == "prefers Sony"
    # Another shopper's key sees nothing.
    other = await route(ctx, MemoryRequest(user="dashboard:u2"))
    assert other.history == []
