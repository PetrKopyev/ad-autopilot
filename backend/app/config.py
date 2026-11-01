from decimal import Decimal
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.settings import DomainSettings

PLACEHOLDER_SECRET_KEY = "replace-with-random-secret"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    database_url: str
    app_base_url: str
    secret_key: SecretStr
    token_encryption_key: SecretStr
    platform_mode: Literal["live", "fake"] = "live"
    dry_run: bool = False
    demo_login: bool = False
    media_dir: Path = Path("media")
    yandex_client_id: str = ""
    yandex_client_secret: SecretStr = SecretStr("")
    direct_api_url: str = "https://api.direct.yandex.com/json/v5"
    direct_api_v501_url: str = "https://api.direct.yandex.com/json/v501"
    direct_sandbox: bool = False
    # Суммы стратегии в API Директа: по умолчанию считаем их без НДС и делим бюджет на (1 + НДС), чтобы трата
    # с НДС не превысила бюджет. true — суммы API уже с НДС и передаются как есть.
    direct_amounts_include_vat: bool = False
    vat_percent: int = Field(default=22, ge=0, le=100)
    # Суммы лимитов в API VK Рекламы: по умолчанию уходят как есть. true — ставка НДС сверх лимита VK
    # (зеркально Директу при DIRECT_AMOUNTS_INCLUDE_VAT=false): лимит делим на (1 + НДС).
    vk_amounts_include_vat: bool = False
    vk_package_id: int | None = Field(default=None, gt=0)
    # Цель оптимизации campaign VK (Top@Mail/приложения). Без неё часть пакетов не создаётся, а цель Метрики
    # VK через API не привязывает: source_id счётчика Метрики живьём отвергнут («Bad value», 01.11).
    vk_priced_goal_source_id: int | None = Field(default=None, gt=0)
    vk_priced_goal_name: str = Field(default="Заявка", min_length=1)
    vk_client_id: str = ""
    vk_client_secret: SecretStr = SecretStr("")
    vk_oauth_enabled: bool = False
    yc_folder_id: str = ""
    yc_api_key: SecretStr = SecretStr("")
    yandexgpt_model: str = "yandexgpt/latest"
    yandexart_model: str = "aliceai-image-art-3.0/latest"
    budget_reserve_pct: int = Field(default=8, ge=0, le=100)
    ai_spend_onboarding_limit_rub: Decimal = Field(default=Decimal("60"), ge=0)
    ai_spend_weekly_limit_rub: Decimal = Field(default=Decimal("25"), ge=0)
    tz_default: str = "Europe/Moscow"

    @field_validator("secret_key")
    @classmethod
    def strong_secret_key(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if len(secret) < 32 or secret == PLACEHOLDER_SECRET_KEY:
            raise ValueError("SECRET_KEY must be at least 32 characters and not the .env.example placeholder")
        return value

    @model_validator(mode="after")
    def demo_only_locally(self) -> Self:
        url = urlsplit(self.app_base_url)
        if self.demo_login and (url.scheme not in {"http", "https"} or url.hostname not in {"localhost", "127.0.0.1"}):
            raise ValueError("DEMO_LOGIN is allowed only on a local stand: APP_BASE_URL must be localhost or 127.0.0.1")
        return self


def get_domain_settings(config: Settings | None = None) -> DomainSettings:
    config = config if config is not None else settings
    return DomainSettings(budget_reserve=Decimal(config.budget_reserve_pct) / 100)


settings = Settings()
