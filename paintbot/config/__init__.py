from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    bot_token: SecretStr = Field(min_length=10)
    owner_id: int = Field(gt=0)
    group_id: int | None = None
    database_path: Path = Path("var/paintbot.db")
    check_interval_seconds: int = Field(default=600, ge=600)
    request_gap_seconds: float = Field(default=5, ge=2, le=60)
    scenario_ttl_seconds: int = Field(default=900, ge=60)
    log_level: str = "INFO"

    @field_validator("group_id", mode="before")
    @classmethod
    def blank_group(cls, value):
        return None if value == "" else value
