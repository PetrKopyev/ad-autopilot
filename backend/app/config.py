from decimal import Decimal
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    database_url: str
    app_base_url: str
    secret_key: SecretStr
    token_encryption_key: SecretStr
    platform_mode: Literal["live", "fake"] = "live"
    dry_run: bool = True
    yandex_client_id: str = ""
    yandex_client_secret: SecretStr = SecretStr("")
    direct_api_url: str = "https://api.direct.yandex.com/json/v5"
    direct_api_v501_url: str = "https://api.direct.yandex.com/v501"
    direct_sandbox: bool = False
    vk_client_id: str = ""
    vk_client_secret: SecretStr = SecretStr("")
    yc_folder_id: str = ""
    yc_api_key: SecretStr = SecretStr("")
    yandexgpt_model: str = "yandexgpt/latest"
    budget_reserve_pct: int = Field(default=8, ge=0, le=100)
    ai_spend_onboarding_limit_rub: Decimal = Field(default=Decimal("60"), ge=0)
    ai_spend_weekly_limit_rub: Decimal = Field(default=Decimal("25"), ge=0)
    tz_default: str = "Europe/Moscow"


settings = Settings()
