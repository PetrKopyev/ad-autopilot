from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.crypto import EncryptedText


def choices(column: str, *values: str) -> CheckConstraint:
    options = ", ".join(repr(value) for value in values)
    return CheckConstraint(f"{column} IN ({options})", name=f"{column}_values")


def platform() -> CheckConstraint:
    return choices("platform", "direct", "vk", "fake")


def non_negative(*columns: str) -> tuple[CheckConstraint, ...]:
    return tuple(CheckConstraint(f"{column} >= 0", name=f"{column}_non_negative") for column in columns)


def created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def external_id_unique(table: str, parent: str) -> Index:
    return Index(
        f"uq_{table}_{parent}_external_id",
        parent,
        "external_id",
        unique=True,
        postgresql_where=text("external_id IS NOT NULL"),
    )


ENTITY_STATUSES = ("active", "paused_auto", "paused_owner", "archived")
AD_STATUSES = ("draft", "moderation", "active", "paused_auto", "paused_owner", "rejected", "archived")
WORKER_KINDS = (
    "heartbeat",
    "notification",
    "budget_guard",
    "sync_moderation",
    "sync_stats",
    "optimize",
    "experiments_tick",
    "rebalance_budget",
    "clients_survey",
    "month_rollover",
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    yandex_uid: Mapped[str] = mapped_column(Text, unique=True)
    login: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    csrf_secret: Mapped[str] = mapped_column(Text)


class OAuthState(Base):
    __tablename__ = "oauth_states"
    __table_args__ = (choices("purpose", "login", "direct", "vk"),)

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    purpose: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    pkce_verifier: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Business(Base):
    __tablename__ = "businesses"
    __table_args__ = (
        choices("medical_level", "none", "maybe", "required"),
        choices("status", "draft", "plan_ready", "running", "paused", "budget_stop"),
        *non_negative("month_budget_kop", "avg_check_kop"),
        CheckConstraint("lead_to_client_rate BETWEEN 0 AND 1", name="lead_to_client_rate_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    site_url: Mapped[str] = mapped_column(Text)
    city: Mapped[str] = mapped_column(Text)
    niche_text: Mapped[str | None] = mapped_column(Text)
    niche_class: Mapped[str | None] = mapped_column(Text)
    medical_level: Mapped[str] = mapped_column(Text, default="none", server_default="none")
    description: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    description_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    month_budget_kop: Mapped[int] = mapped_column(BigInteger)
    avg_check_kop: Mapped[int | None] = mapped_column(BigInteger)
    lead_to_client_rate: Mapped[Decimal | None] = mapped_column(Numeric)
    auto_continue: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    status: Mapped[str] = mapped_column(Text, default="draft", server_default="draft")
    metrica_counter_id: Mapped[int | None] = mapped_column(BigInteger)
    lead_goal_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=list, server_default="{}")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = created()


class MarkingProfile(Base):
    __tablename__ = "marking_profiles"
    __table_args__ = (choices("subject_type", "ip", "ooo", "selfemployed", "person"),)

    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    subject_type: Mapped[str] = mapped_column(Text)
    inn_enc: Mapped[str] = mapped_column(EncryptedText)
    name: Mapped[str] = mapped_column(Text)
    phone_enc: Mapped[str | None] = mapped_column(EncryptedText)
    confirmed_in_platform: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    # Статус последней передачи данных рекламодателя через API площадки: {площадка: {status, attempted_at}}.
    # status — marking_transmitted / marking_manual_required (данные не приняты: владелец заполняет в кабинете).
    transmission: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class PlatformAccount(Base):
    __tablename__ = "platform_accounts"
    __table_args__ = (
        platform(),
        choices("auth_mode", "oauth", "manual_key"),
        choices("status", "ok", "needs_reauth", "revoked"),
        UniqueConstraint("business_id", "platform"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(Text)
    auth_mode: Mapped[str] = mapped_column(Text)
    external_login: Mapped[str | None] = mapped_column(Text)
    access_token_enc: Mapped[str] = mapped_column(EncryptedText)
    refresh_token_enc: Mapped[str | None] = mapped_column(EncryptedText)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    status: Mapped[str] = mapped_column(Text, default="ok", server_default="ok")
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (
        choices("status", "draft", "approved", "launched", "failed"),
        UniqueConstraint("business_id", "version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, default="draft", server_default="draft")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        platform(),
        choices("type", "unified", "text", "vk_plan"),
        choices("status", *ENTITY_STATUSES),
        *non_negative("daily_limit_kop", "period_cap_kop"),
        external_id_unique("campaigns", "platform_account_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    # Кабинет с кампаниями удалить нельзя; при удалении бизнеса кампании уходят каскадом по business_id.
    platform_account_id: Mapped[int] = mapped_column(
        ForeignKey("platform_accounts.id", ondelete="RESTRICT"), index=True
    )
    platform: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    daily_limit_kop: Mapped[int] = mapped_column(BigInteger)
    period_cap_kop: Mapped[int] = mapped_column(BigInteger)
    owner_paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    frozen_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()


class AdGroup(Base):
    __tablename__ = "ad_groups"
    __table_args__ = (choices("status", *ENTITY_STATUSES), external_id_unique("ad_groups", "campaign_id"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str | None] = mapped_column(Text)
    theme: Mapped[str] = mapped_column(Text)
    geo: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    frozen_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Target(Base):
    __tablename__ = "targets"
    __table_args__ = (
        choices("kind", "phrase", "interest", "audience", "negative"),
        choices("status", *ENTITY_STATUSES),
        external_id_unique("targets", "ad_group_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ad_group_id: Mapped[int] = mapped_column(ForeignKey("ad_groups.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    frozen_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (choices("kind", "image", "video", "upload"), *non_negative("cost_kop"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(Text)
    idea_text: Mapped[str | None] = mapped_column(Text)
    prompt: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str] = mapped_column(Text)
    phash: Mapped[str | None] = mapped_column(Text)
    formats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    platform_refs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    cost_kop: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    selected: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = created()


class Ad(Base):
    __tablename__ = "ads"
    __table_args__ = (
        choices("status", *AD_STATUSES),
        choices("role", "base", "control", "variant"),
        choices("moderation_status", "pending", "approved", "rejected"),
        external_id_unique("ads", "ad_group_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ad_group_id: Mapped[int] = mapped_column(ForeignKey("ad_groups.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    # Второй заголовок объявления (генерация ad_text); пусто у площадок без второго заголовка (VK).
    title2: Mapped[str] = mapped_column(Text, default="", server_default="")
    text: Mapped[str] = mapped_column(Text)
    display_url: Mapped[str | None] = mapped_column(Text)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), index=True)
    video_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(Text, default="draft", server_default="draft")
    moderation_status: Mapped[str | None] = mapped_column(Text)
    moderation_reason: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, default="base", server_default="base")
    experiment_id: Mapped[int | None] = mapped_column(
        ForeignKey("experiments.id", ondelete="SET NULL", use_alter=True), index=True
    )
    frozen_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()


class StatsDaily(Base):
    __tablename__ = "stats_daily"
    __table_args__ = (
        platform(),
        Index("ix_stats_daily_business_id_date", "business_id", "date"),
        *non_negative("impressions", "clicks", "cost_kop"),
    )

    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    # Площадку задаёт объявление: одна строка на объявление и день.
    platform: Mapped[str] = mapped_column(Text)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    ad_id: Mapped[int] = mapped_column(ForeignKey("ads.id", ondelete="CASCADE"), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    impressions: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    clicks: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    cost_kop: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    conversions: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict, server_default="{}")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_settled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class BudgetLedger(Base):
    __tablename__ = "budget_ledger"
    __table_args__ = (
        platform(),
        *non_negative("allocated_kop", "period_cap_kop", "spent_kop"),
        CheckConstraint("EXTRACT(DAY FROM month) = 1", name="month_first_day"),
    )

    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    month: Mapped[date] = mapped_column(Date, primary_key=True)
    platform: Mapped[str] = mapped_column(Text, primary_key=True)
    allocated_kop: Mapped[int] = mapped_column(BigInteger)
    period_cap_kop: Mapped[int] = mapped_column(BigInteger)
    spent_kop: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    spent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hard_stop_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LeadsSurvey(Base):
    __tablename__ = "leads_survey"
    __table_args__ = non_negative("leads_count", "clients_count")

    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, primary_key=True)
    leads_count: Mapped[int] = mapped_column(Integer)
    clients_count: Mapped[int | None] = mapped_column(Integer)
    asked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChangeLog(Base):
    __tablename__ = "change_log"
    __table_args__ = (
        platform(),
        choices("actor", "autopilot", "owner", "system", "external"),
        choices(
            "kind", "pause", "resume", "budget", "text", "image", "launch", "revert", "experiment_verdict", "marking"
        ),
        choices("status", "proposed", "pending", "applied", "failed", "reverted"),
        UniqueConstraint("business_id", "idempotency_key"),
        Index("ix_change_log_pending_created_at", "created_at", postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    actor: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    platform: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)
    operations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    reason_code: Mapped[str] = mapped_column(Text)
    reason_params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    reason_text: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    reverts_id: Mapped[int | None] = mapped_column(ForeignKey("change_log.id", ondelete="SET NULL"), index=True)
    # Ключ шага воркера или предложения A/B: одно изменение на ключ, повтор после failed снимает ключ с неудачи.
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index("ix_change_log_business_id_created_at", ChangeLog.business_id, ChangeLog.created_at.desc())
Index(
    "ix_change_log_business_id_platform_created_at",
    ChangeLog.business_id,
    ChangeLog.platform,
    ChangeLog.created_at.desc(),
)


class Experiment(Base):
    __tablename__ = "experiments"
    __table_args__ = (
        choices("hypothesis", "text", "image", "audience"),
        choices("status", "proposed", "running", "concluded", "cancelled"),
        choices("verdict", "variant_wins", "control_wins", "no_difference", "inconclusive"),
        CheckConstraint("control_ad_id <> variant_ad_id", name="distinct_arms"),
        *non_negative("planned_n_per_arm"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    ad_group_id: Mapped[int] = mapped_column(ForeignKey("ad_groups.id", ondelete="CASCADE"), index=True)
    hypothesis: Mapped[str] = mapped_column(Text)
    control_ad_id: Mapped[int] = mapped_column(ForeignKey("ads.id", ondelete="CASCADE"), index=True)
    variant_ad_id: Mapped[int] = mapped_column(ForeignKey("ads.id", ondelete="CASCADE"), index=True)
    metric: Mapped[str] = mapped_column(Text)
    planned_n_per_arm: Mapped[int] = mapped_column(BigInteger)
    min_effect: Mapped[Decimal] = mapped_column(Numeric)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="running", server_default="running")
    verdict: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    change_id: Mapped[int | None] = mapped_column(ForeignKey("change_log.id", ondelete="SET NULL"), index=True)


class WorkerRun(Base):
    __tablename__ = "worker_runs"
    __table_args__ = (
        choices("kind", *WORKER_KINDS),
        choices("status", "running", "ok", "failed", "proposed"),
        *non_negative("units_spent"),
        # Слот шага или ключ уведомления; у heartbeat ключа нет.
        UniqueConstraint("business_id", "kind", "slot_key"),
        Index("ix_worker_runs_business_id_started_at", "business_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    business_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    units_spent: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    log: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    slot_key: Mapped[str | None] = mapped_column(Text)


class LLMCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = non_negative("tokens_in", "tokens_out", "cost_kop")

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(BigInteger)
    tokens_out: Mapped[int] = mapped_column(BigInteger)
    cost_kop: Mapped[int] = mapped_column(BigInteger)
    # Статус ответа YandexGPT как есть: список задаёт внешний API, неизвестный статус не должен терять учёт трат.
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created()


class ArtCall(Base):
    __tablename__ = "art_calls"
    __table_args__ = (
        choices("status", "pending", "succeeded", "rejected", "unknown"),
        *non_negative("cost_kop"),
        Index(
            "uq_art_calls_operation_id",
            "operation_id",
            unique=True,
            postgresql_where=text("operation_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    cost_kop: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text)
    operation_id: Mapped[str | None] = mapped_column(Text)
    seed: Mapped[int | None] = mapped_column(BigInteger)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()


class AiSpendLedger(Base):
    """Учёт трат на ИИ для лимитов владельца и суточного потолка сервиса.

    Отдельная таблица без внешних ключей: удаление бизнеса или аккаунта не обнуляет лимит (иначе
    трату оператору можно было бы сжигать бесконечно пересозданием аккаунта). owner_key — хэш
    yandex_uid: персональный идентификатор в базе не храним. Строка с final=false — резерв
    оценки до вызова (защита от параллельных запросов); после ответа строка уточняется до
    фактической стоимости либо удаляется при отказе."""

    __tablename__ = "ai_spend_ledger"
    __table_args__ = (*non_negative("cost_kop"), Index("ix_ai_spend_ledger_owner", "owner_key", "created_at"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_key: Mapped[str] = mapped_column(Text)
    business_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(Text)
    cost_kop: Mapped[int] = mapped_column(BigInteger)
    final: Mapped[bool] = mapped_column(default=False, server_default="false")
    created_at: Mapped[datetime] = created()
