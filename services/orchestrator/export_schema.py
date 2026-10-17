"""Regenerate the shared API contract without loading local credentials."""

import json
from pathlib import Path

from laai_orchestrator.config import Config
from laai_orchestrator.main import create_app

config = Config(
    _env_file=None,
    supabase_url="https://example.supabase.co",
    supabase_publishable_key="schema-only",
    supabase_secret_key="schema-only",
)
destination = Path(__file__).resolve().parents[2] / "packages/shared/openapi.json"
destination.write_text(json.dumps(create_app(config).openapi(), indent=2) + "\n")
print(f"Exported {destination}")
