"""Settings, validated once at start-up from the environment (prefix ``FP_``) or a ``.env`` file.

Every external provider is reached through one base URL. On a Sprite that base URL is the
connector gateway (``https://api.sprites.dev/v1/gateway/<provider>/<conn_id>``) and no key is
needed, because the gateway attaches the credential. For local development, point the base URL
straight at the provider and set the matching key so requests carry a bearer token.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Provider(BaseModel):
    """Where to send requests for one external service."""

    model_config = ConfigDict(frozen=True)

    name: str
    base_url: str = ""
    key: SecretStr = SecretStr("")
    extra_headers: dict[str, str] = Field(default_factory=dict)
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.strip().rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.base_url)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FP_", env_file=".env", extra="ignore", frozen=True, env_ignore_empty=True
    )

    data_dir: Path = Path.home() / ".fineprint"
    shared_secret: SecretStr = SecretStr("")
    public_url: str = ""
    calendar_token: SecretStr = SecretStr("")
    telegram_token: SecretStr = SecretStr("")
    # The bot answers one chat. Either fix it here, or claim it once with `/start <claim code>`.
    owner_chat_id: int | None = None
    owner_claim_code: SecretStr = SecretStr("")
    # Shared code for colleagues and judges: `/start <join code>` in Telegram, or in an email subject.
    join_code: SecretStr = SecretStr("")

    # Both models are reached through one OpenCode key: Jev (TypeSafe's decision model) for
    # judgments, a chat model for writing. Free OpenCode models refuse calls from outside OpenCode.
    judge_model: str = "jev-1.14"
    writer_model: str = "glm-5.3-flash"
    pinecone_index: str = "fine-print"
    pinecone_namespace: str = "clauses"
    llama_project_id: str = ""
    # Shared inbox members forward mail to (AgentMail).
    agentmail_address: str = ""
    material_threshold: float = Field(0.7, ge=0, le=1)

    # On a Sprite: a Custom API connector's gateway base for https://opencode.ai/inference.
    opencode_base: str = "https://opencode.ai/inference"
    opencode_key: SecretStr = SecretStr("")
    # The Sprites Pinecone connector routes by index name, so this is the connector's gateway base.
    pinecone_base: str = ""
    pinecone_key: SecretStr = SecretStr("")
    llama_base: str = "https://api.cloud.llamaindex.ai"
    llama_key: SecretStr = SecretStr("")

    @field_validator("public_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.strip().rstrip("/")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "fineprint.db"

    @property
    def opencode(self) -> Provider:
        return Provider(name="opencode", base_url=self.opencode_base, key=self.opencode_key)

    @property
    def pinecone(self) -> Provider:
        return Provider(
            name="pinecone",
            base_url=self.pinecone_base,
            key=self.pinecone_key,
            extra_headers={"X-Pinecone-API-Version": "2025-04"},
            auth_header="Api-Key",
            auth_prefix="",
        )

    @property
    def llama(self) -> Provider:
        return Provider(name="llama", base_url=self.llama_base, key=self.llama_key)


settings = Settings()
