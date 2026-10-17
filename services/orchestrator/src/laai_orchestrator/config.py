from pydantic import Field, HttpUrl, SecretStr
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
