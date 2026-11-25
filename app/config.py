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
    model_config = SettingsConfigDict(env_prefix="FP_", env_file=".env", extra="ignore", frozen=True)

    data_dir: Path = Path.home() / ".fineprint"
    shared_secret: SecretStr = SecretStr("")
    public_url: str = ""
    calendar_token: SecretStr = SecretStr("")
    telegram_token: SecretStr = SecretStr("")
    gmail_token_file: Path | None = None

    jev_model: str = "jev-latest"
    ollama_model: str = "gpt-oss:120b"
    pinecone_namespace: str = "clauses"
    box_folder_id: str = "0"
    llama_project_id: str = ""
    material_threshold: float = Field(0.7, ge=0, le=1)

    typesafe_base: str = ""
    typesafe_key: SecretStr = SecretStr("")
    ollama_base: str = ""
    ollama_key: SecretStr = SecretStr("")
    # Pinecone's data plane lives on a per-index host: this is that host (direct) or the gateway
    # path that forwards to it.
    pinecone_base: str = ""
    pinecone_key: SecretStr = SecretStr("")
    box_base: str = ""
    box_key: SecretStr = SecretStr("")
    box_upload_base: str = ""
    box_upload_key: SecretStr = SecretStr("")
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
    def gmail_token_path(self) -> Path:
        return self.gmail_token_file or self.data_dir / "gmail.json"

    @property
    def typesafe(self) -> Provider:
        return Provider(name="typesafe", base_url=self.typesafe_base, key=self.typesafe_key)

    @property
    def ollama(self) -> Provider:
        return Provider(name="ollama", base_url=self.ollama_base, key=self.ollama_key)

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
    def box(self) -> Provider:
        return Provider(name="box", base_url=self.box_base, key=self.box_key)

    @property
    def box_upload(self) -> Provider:
        return Provider(name="box_upload", base_url=self.box_upload_base, key=self.box_upload_key)

    @property
    def llama(self) -> Provider:
        return Provider(name="llama", base_url=self.llama_base, key=self.llama_key)


settings = Settings()
