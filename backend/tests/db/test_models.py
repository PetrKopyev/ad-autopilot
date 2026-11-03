from sqlalchemy import BigInteger, CheckConstraint, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, dialect
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateIndex

from app.db import models
from app.db.base import Base

TABLES = {
    "users",
    "sessions",
    "oauth_states",
    "businesses",
    "marking_profiles",
    "platform_accounts",
    "plans",
    "campaigns",
    "ad_groups",
    "targets",
    "ads",
    "assets",
    "stats_daily",
    "budget_ledger",
    "leads_survey",
    "experiments",
    "change_log",
    "worker_runs",
    "llm_calls",
    "art_calls",
    # Учёт трат на ИИ без внешних ключей: удаление бизнеса/аккаунта лимит не обнуляет (В11).
    "ai_spend_ledger",
}


def test_tables_and_mappers() -> None:
    configure_mappers()
    assert set(Base.metadata.tables) == TABLES
    assert all(table.primary_key.columns for table in Base.metadata.tables.values())


def test_indexes_and_unique_keys() -> None:
    indexes = {index.name: index for table in Base.metadata.tables.values() for index in table.indexes}
    foreign_key_indexes = {
        f"ix_{table}_{column}"
        for table, column in (
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
    }
    partial_unique = {
        "uq_campaigns_platform_account_id_external_id": "external_id IS NOT NULL",
        "uq_ad_groups_campaign_id_external_id": "external_id IS NOT NULL",
        "uq_ads_ad_group_id_external_id": "external_id IS NOT NULL",
        "uq_targets_ad_group_id_external_id": "external_id IS NOT NULL",
        "uq_art_calls_operation_id": "operation_id IS NOT NULL",
    }
    assert set(indexes) == foreign_key_indexes | set(partial_unique) | {
        "ix_stats_daily_business_id_date",
        "ix_change_log_business_id_created_at",
        "ix_change_log_business_id_platform_created_at",
        "ix_change_log_pending_created_at",
        "ix_worker_runs_business_id_started_at",
        # ai_spend_ledger: внешних ключей нет, индексы объявлены явно.
        "ix_ai_spend_ledger_owner",
        "ix_ai_spend_ledger_business_id",
        "ix_ai_spend_ledger_created_at",
    }
    for name, where in partial_unique.items():
        assert indexes[name].unique and str(indexes[name].dialect_options["postgresql"]["where"]) == where
    assert str(indexes["ix_change_log_pending_created_at"].dialect_options["postgresql"]["where"]) == (
        "status = 'pending'"
    )
    assert [c.name for c in indexes["ix_stats_daily_business_id_date"].columns] == ["business_id", "date"]
    change_index = str(CreateIndex(indexes["ix_change_log_business_id_created_at"]).compile(dialect=dialect()))
    assert "(business_id, created_at DESC)" in change_index
    platform_index = str(
        CreateIndex(indexes["ix_change_log_business_id_platform_created_at"]).compile(dialect=dialect())
    )
    assert "(business_id, platform, created_at DESC)" in platform_index
    assert list(models.StatsDaily.__table__.primary_key.columns.keys()) == ["ad_id", "date"]
    assert list(models.BudgetLedger.__table__.primary_key.columns.keys()) == ["business_id", "month", "platform"]
    unique = {
        table.name: [list(c.columns.keys()) for c in table.constraints if isinstance(c, UniqueConstraint)]
        for table in Base.metadata.tables.values()
    }
    assert unique["users"] == [["yandex_uid"]]
    assert unique["platform_accounts"] == [["business_id", "platform"]]
    assert unique["plans"] == [["business_id", "version"]]
    assert unique["change_log"] == [["business_id", "idempotency_key"]]
    assert unique["worker_runs"] == [["business_id", "kind", "slot_key"]]
    platform_account = next(iter(models.Campaign.__table__.c.platform_account_id.foreign_keys))
    assert platform_account.ondelete == "RESTRICT"


def test_money_json_arrays_defaults_and_cascades() -> None:
    money_columns = [c for table in Base.metadata.tables.values() for c in table.c if c.name.endswith("_kop")]
    assert len(money_columns) == 12
    assert all(isinstance(column.type, BigInteger) for column in money_columns)
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table.name in {"users", "businesses"}:
                assert fk.ondelete == "CASCADE"
    business = models.Business.__table__
    assert business.c.avg_check_kop.nullable
    assert business.c.auto_continue.default.arg is True
    assert str(business.c.auto_continue.server_default.arg) == "true"
    assert isinstance(business.c.lead_goal_ids.type, ARRAY)
    for model, names in [
        (models.Business, ["description", "settings"]),
        (models.MarkingProfile, ["confirmed_in_platform"]),
        (models.Plan, ["payload"]),
        (models.AdGroup, ["geo"]),
        (models.Asset, ["formats", "platform_refs"]),
        (models.StatsDaily, ["conversions"]),
        (models.Experiment, ["stats"]),
        (models.ChangeLog, ["operations", "reason_params", "evidence"]),
        (models.WorkerRun, ["log"]),
    ]:
        assert all(isinstance(model.__table__.c[name].type, JSONB) for name in names)
    assert models.ChangeLog.__table__.c.reverts_id.references(models.ChangeLog.__table__.c.id)
    assert models.Ad.__table__.c.frozen_until.nullable


def test_closed_enumerations_have_string_check_constraints() -> None:
    expected = {
        "oauth_states": {"purpose"},
        "businesses": {"medical_level", "status"},
        "marking_profiles": {"subject_type"},
        "platform_accounts": {"platform", "auth_mode", "status"},
        "plans": {"status"},
        "campaigns": {"platform", "type", "status"},
        "ad_groups": {"status"},
        "targets": {"kind", "status"},
        "ads": {"status", "role", "moderation_status"},
        "assets": {"kind"},
        "stats_daily": {"platform"},
        "budget_ledger": {"platform"},
        "experiments": {"hypothesis", "status", "verdict"},
        "change_log": {"actor", "kind", "platform", "status"},
        "worker_runs": {"kind", "status"},
        "art_calls": {"status"},
    }
    for table_name, columns in expected.items():
        table = Base.metadata.tables[table_name]
        checks = [str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)]
        for name in columns:
            assert isinstance(table.c[name].type, Text)
            assert any(sql.startswith(f"{name} IN (") for sql in checks)
