from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    bot_token: SecretStr = SecretStr("")
    owner_id: int = 0
    notification_chat_id: int | None = None
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")
    check_interval: int = Field(default=600, ge=600)
    wb_interval: int = Field(default=600, ge=600)
    ozon_interval: int = Field(default=600, ge=600)
    request_gap: float = Field(default=3, ge=1)
    request_timeout: float = Field(default=20, ge=1, le=60)
    browser_enabled: bool = False
    chromium_path: str | None = None
    wb_dest: str = "-1257786"
    scenario_timeout: int = Field(default=1800, ge=60)
    max_batch: int = Field(default=30, ge=1, le=100)
    display_timezone: str = "Europe/Moscow"

    @model_validator(mode="after")
    def timezone_exists(self):
        from zoneinfo import ZoneInfo

        ZoneInfo(self.display_timezone)
        return self

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir.resolve() / 'paintwatch.sqlite3'}"

    def interval(self, market: str) -> int:
        return max(self.check_interval, getattr(self, f"{market}_interval"))

    def require_telegram(self) -> None:
        if not self.bot_token.get_secret_value() or self.owner_id <= 0:
            raise ValueError(
                "Задайте BOT_TOKEN и положительный OWNER_ID в защищённом .env"
            )
