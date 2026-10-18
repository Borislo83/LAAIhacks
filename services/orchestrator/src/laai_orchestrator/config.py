from uuid import UUID

from pydantic import Field, HttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: HttpUrl
    supabase_publishable_key: SecretStr = Field(min_length=1)
    supabase_secret_key: SecretStr = Field(min_length=1)
    agents_url: HttpUrl = HttpUrl("http://127.0.0.1:8000")
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    asi_one_api_key: SecretStr = SecretStr("")
    asi_one_model: str = "asi1-mini"
    # Local prototyping only: lets /v1/chat run without a Supabase sign-in.
    chat_auth_required: bool = True
    amazon_cart_executor_token: SecretStr = SecretStr("")
    # Sends confirmed iMessage drafts. Empty disables sending (503). Never sent to clients.
    imessage_executor_token: SecretStr = SecretStr("")
    # Opens a YouTube video in the laptop's browser for "play X". Empty: link only.
    laptop_executor_token: SecretStr = SecretStr("")
    # Local development only. Accepts the fixture tokens in DEMO_TOKENS from loopback clients
    # and maps them to DEMO_USER_ID, which must be an existing Supabase Auth user.
    demo_auth_mode: bool = False
    demo_user_id: UUID | None = None

    @model_validator(mode="after")
    def demo_user_required(self):
        if self.demo_auth_mode and self.demo_user_id is None:
            raise ValueError("DEMO_AUTH_MODE requires DEMO_USER_ID (an existing Auth user)")
        return self


# Fixture bearer tokens for DEMO_AUTH_MODE. They are public by design and only ever
# accepted from loopback clients, so they can live in the repository and docs.
DEMO_TOKENS = {
    "demo-web": {"web"},
    "demo-g2": {"g2"},
    "demo-agent": {"agent"},
    "demo-executor": {"executor"},
    "demo-all": {"web", "g2", "agent", "executor"},
}
LOOPBACK_CLIENTS = {"127.0.0.1", "::1", "localhost"}
# Local tunnels and proxies (ngrok, Cloudflare, nginx) connect from loopback but add these.
PROXY_HEADERS = {"x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip"}
