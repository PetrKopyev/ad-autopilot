"""Storage integrity: unique keys, idempotency keys, FK indexes, checks and created_at.

Прод на момент 0003 пуст, поэтому всё сделано одной транзакцией без CONCURRENTLY и NOT VALID.
Правило для 0004 и дальше (таблицы уже с данными, API и воркер работают во время выкатки):
- индекс — ``op.create_index(..., postgresql_concurrently=True)`` внутри ``with op.get_context().autocommit_block()``;
- CHECK и внешний ключ — сначала ``NOT VALID`` (``postgresql_not_valid=True``), затем отдельным шагом ``VALIDATE``;
- уникальность — ``CREATE UNIQUE INDEX CONCURRENTLY``, затем ``ADD CONSTRAINT ... USING INDEX``;
- ``lock_timeout`` (5 секунд, alembic/env.py) не снимать: миграция лучше упадёт и повторится, чем повесит API в очереди
  за ACCESS EXCLUSIVE.

Данные при upgrade:
- slot_key и idempotency_key заполняются из старых ключей (log[0].key, evidence.worker_key/experiment_key); при
  повторах одного ключа ключ получает одна строка — успешная (ok / не failed), из них последняя, остальные остаются
  без ключа и видны в истории;
- снимок business.settings["_ai"] удаляется: недельный лимит ИИ теперь считается по created_at;
- created_at уже существующих llm_calls и art_calls — 1970-01-01: время прошлых вызовов неизвестно, и они не должны
  попасть в неделю выкатки и исчерпать недельный лимит ИИ; у остальных таблиц — время миграции.
Строки stats_daily, у которых площадка не совпадает с площадкой кампании объявления, старая синхронизация не писала
(она искала объявление в кампаниях той же площадки); если такие всё же есть, миграция останавливается с их числом —
это ошибка данных, её разбирают вручную. Так же — дубли кабинетов, версий плана, внешних id и operation_id: миграция
падает с ошибкой PostgreSQL, где указан дублирующийся ключ.

Совместимость: старый код работает на новой схеме (новые столбцы допускают NULL или имеют default, worker_key
в evidence и key в log пишутся по-прежнему). Строки, которые старый воркер запишет между миграцией и своим
перезапуском, останутся без ключей, поэтому воркер останавливают до миграции, а сервис migrate идёт раньше api и
worker. Downgrade возвращает схему 0002 без потери строк; теряются только новые столбцы (created_at, ключи
идемпотентности); старый код после downgrade снова создаёт снимок "_ai" с текущей
суммы, то есть траты ИИ текущей недели до отката в лимит не попадут.
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_storage_integrity"
down_revision = "0002_autopilot_additions"
branch_labels = None
depends_on = None

ENTITY_STATUSES = "status IN ('active', 'paused_auto', 'paused_owner', 'archived')"
CREATED_AT = ("businesses", "platform_accounts", "campaigns", "ads", "assets", "llm_calls", "art_calls")
UNKNOWN_CREATED_AT = ("llm_calls", "art_calls")
FK_INDEXES = (
    ("sessions", "user_id"),
    ("oauth_states", "user_id"),
    ("businesses", "user_id"),
    ("campaigns", "business_id"),
    ("campaigns", "platform_account_id"),
    ("ad_groups", "campaign_id"),
    ("targets", "ad_group_id"),
    ("assets", "business_id"),
    ("ads", "ad_group_id"),
    ("ads", "asset_id"),
    ("ads", "video_id"),
    ("ads", "experiment_id"),
    ("stats_daily", "campaign_id"),
    ("change_log", "reverts_id"),
    ("experiments", "business_id"),
    ("experiments", "ad_group_id"),
    ("experiments", "control_ad_id"),
    ("experiments", "variant_ad_id"),
    ("experiments", "change_id"),
    ("llm_calls", "business_id"),
    ("art_calls", "business_id"),
)
EXTERNAL_IDS = (
    ("campaigns", "platform_account_id"),
    ("ad_groups", "campaign_id"),
    ("ads", "ad_group_id"),
    ("targets", "ad_group_id"),
)
NON_NEGATIVE = (
    ("businesses", ("month_budget_kop", "avg_check_kop")),
    ("campaigns", ("daily_limit_kop", "period_cap_kop")),
    ("assets", ("cost_kop",)),
    ("stats_daily", ("impressions", "clicks", "cost_kop")),
    ("budget_ledger", ("allocated_kop", "period_cap_kop", "spent_kop")),
    ("leads_survey", ("leads_count", "clients_count")),
    ("experiments", ("planned_n_per_arm",)),
    ("worker_runs", ("units_spent",)),
    ("llm_calls", ("tokens_in", "tokens_out", "cost_kop")),
    ("art_calls", ("cost_kop",)),
)
CHECKS = (
    ("businesses", "lead_to_client_rate_range", "lead_to_client_rate BETWEEN 0 AND 1"),
    ("budget_ledger", "month_first_day", "EXTRACT(DAY FROM month) = 1"),
    ("experiments", "distinct_arms", "control_ad_id <> variant_ad_id"),
    ("campaigns", "status_values", ENTITY_STATUSES),
    ("ad_groups", "status_values", ENTITY_STATUSES),
    ("targets", "status_values", ENTITY_STATUSES),
    ("ads", "moderation_status_values", "moderation_status IN ('pending', 'approved', 'rejected')"),
    ("art_calls", "status_values", "status IN ('pending', 'succeeded', 'rejected', 'unknown')"),
    ("worker_runs", "status_values", "status IN ('running', 'ok', 'failed', 'proposed')"),
    (
        "worker_runs",
        "kind_values",
        "kind IN ('heartbeat', 'notification', 'budget_guard', 'sync_moderation', 'sync_stats', 'optimize', "
        "'experiments_tick', 'rebalance_budget', 'clients_survey', 'month_rollover')",
    ),
)


def checks() -> list[tuple[str, str, str]]:
    money = [
        (table, f"{column}_non_negative", f"{column} >= 0") for table, columns in NON_NEGATIVE for column in columns
    ]
    return money + list(CHECKS)


def upgrade() -> None:
    foreign = op.get_bind().scalar(
        sa.text(
            """
            SELECT count(*) FROM stats_daily AS stats
            JOIN ads ON ads.id = stats.ad_id
            JOIN ad_groups ON ad_groups.id = ads.ad_group_id
            JOIN campaigns ON campaigns.id = ad_groups.campaign_id
            WHERE campaigns.platform <> stats.platform
            """
        )
    )
    if foreign:
        raise RuntimeError(
            f"stats_daily: {foreign} строк с площадкой, отличной от площадки кампании; разберите вручную."
        )
    op.execute("UPDATE businesses SET settings = settings - '_ai' WHERE settings ? '_ai'")

    for table in CREATED_AT:
        past = table in UNKNOWN_CREATED_AT
        default = sa.text("'1970-01-01 00:00:00+00'") if past else sa.text("now()")
        op.add_column(
            table, sa.Column("created_at", sa.DateTime(timezone=True), server_default=default, nullable=False)
        )
        if past:
            op.alter_column(table, "created_at", server_default=sa.text("now()"))
    op.add_column("worker_runs", sa.Column("slot_key", sa.Text(), nullable=True))
    op.add_column("change_log", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE worker_runs AS run SET slot_key = ranked.key
        FROM (
            SELECT id, log -> 0 ->> 'key' AS key,
                   row_number() OVER (
                       PARTITION BY business_id, kind, log -> 0 ->> 'key' ORDER BY status = 'ok' DESC, id DESC
                   ) AS position
            FROM worker_runs
            WHERE kind <> 'heartbeat' AND log -> 0 ->> 'key' IS NOT NULL
        ) AS ranked
        WHERE run.id = ranked.id AND ranked.position = 1
        """
    )
    op.execute(
        """
        UPDATE change_log AS change SET idempotency_key = ranked.key
        FROM (
            SELECT id, coalesce(evidence ->> 'worker_key', evidence ->> 'experiment_key') AS key,
                   row_number() OVER (
                       PARTITION BY business_id, coalesce(evidence ->> 'worker_key', evidence ->> 'experiment_key')
                       ORDER BY status <> 'failed' DESC, id DESC
                   ) AS position
            FROM change_log
            WHERE coalesce(evidence ->> 'worker_key', evidence ->> 'experiment_key') IS NOT NULL
        ) AS ranked
        WHERE change.id = ranked.id AND ranked.position = 1
        """
    )

    op.create_unique_constraint(
        op.f("uq_platform_accounts_business_id"), "platform_accounts", ["business_id", "platform"]
    )
    op.create_unique_constraint(op.f("uq_plans_business_id"), "plans", ["business_id", "version"])
    op.create_unique_constraint(op.f("uq_change_log_business_id"), "change_log", ["business_id", "idempotency_key"])
    op.create_unique_constraint(op.f("uq_worker_runs_business_id"), "worker_runs", ["business_id", "kind", "slot_key"])
    for table, parent in EXTERNAL_IDS:
        op.create_index(
            f"uq_{table}_{parent}_external_id",
            table,
            [parent, "external_id"],
            unique=True,
            postgresql_where=sa.text("external_id IS NOT NULL"),
        )
    op.drop_index(op.f("ix_art_calls_operation_id"), table_name="art_calls")
    op.create_index(
        "uq_art_calls_operation_id",
        "art_calls",
        ["operation_id"],
        unique=True,
        postgresql_where=sa.text("operation_id IS NOT NULL"),
    )

    for table, column in FK_INDEXES:
        op.create_index(op.f(f"ix_{table}_{column}"), table, [column])
    op.create_index(
        "ix_change_log_pending_created_at", "change_log", ["created_at"], postgresql_where=sa.text("status = 'pending'")
    )
    op.create_index(
        "ix_change_log_business_id_platform_created_at",
        "change_log",
        ["business_id", "platform", sa.literal_column("created_at DESC")],
    )
    op.create_index("ix_worker_runs_business_id_started_at", "worker_runs", ["business_id", "started_at"])

    op.drop_constraint(op.f("pk_stats_daily"), "stats_daily", type_="primary")
    op.create_primary_key(op.f("pk_stats_daily"), "stats_daily", ["ad_id", "date"])
    op.drop_constraint(op.f("fk_campaigns_platform_account_id_platform_accounts"), "campaigns", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_campaigns_platform_account_id_platform_accounts"),
        "campaigns",
        "platform_accounts",
        ["platform_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    for table, name, condition in checks():
        op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, condition)


def downgrade() -> None:
    for table, name, _ in reversed(checks()):
        op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")

    op.drop_constraint(op.f("fk_campaigns_platform_account_id_platform_accounts"), "campaigns", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_campaigns_platform_account_id_platform_accounts"),
        "campaigns",
        "platform_accounts",
        ["platform_account_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(op.f("pk_stats_daily"), "stats_daily", type_="primary")
    op.create_primary_key(op.f("pk_stats_daily"), "stats_daily", ["platform", "ad_id", "date"])

    op.drop_index("ix_worker_runs_business_id_started_at", table_name="worker_runs")
    op.drop_index("ix_change_log_business_id_platform_created_at", table_name="change_log")
    op.drop_index("ix_change_log_pending_created_at", table_name="change_log")
    for table, column in reversed(FK_INDEXES):
        op.drop_index(op.f(f"ix_{table}_{column}"), table_name=table)

    op.drop_index("uq_art_calls_operation_id", table_name="art_calls")
    op.create_index(op.f("ix_art_calls_operation_id"), "art_calls", ["operation_id"])
    for table, parent in reversed(EXTERNAL_IDS):
        op.drop_index(f"uq_{table}_{parent}_external_id", table_name=table)
    op.drop_constraint(op.f("uq_worker_runs_business_id"), "worker_runs", type_="unique")
    op.drop_constraint(op.f("uq_change_log_business_id"), "change_log", type_="unique")
    op.drop_constraint(op.f("uq_plans_business_id"), "plans", type_="unique")
    op.drop_constraint(op.f("uq_platform_accounts_business_id"), "platform_accounts", type_="unique")

    op.drop_column("change_log", "idempotency_key")
    op.drop_column("worker_runs", "slot_key")
    for table in reversed(CREATED_AT):
        op.drop_column(table, "created_at")
