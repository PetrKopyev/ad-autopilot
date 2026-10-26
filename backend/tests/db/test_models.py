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
}


def test_tables_and_mappers() -> None:
    configure_mappers()
    assert set(Base.metadata.tables) == TABLES
    assert all(table.primary_key.columns for table in Base.metadata.tables.values())


def test_indexes_and_unique_keys() -> None:
    indexes = {index.name: index for table in Base.metadata.tables.values() for index in table.indexes}
    assert set(indexes) == {
        "ix_stats_daily_business_id_date",
        "ix_change_log_business_id_created_at",
        "ix_art_calls_operation_id",
    }
    assert [c.name for c in indexes["ix_stats_daily_business_id_date"].columns] == ["business_id", "date"]
    change_index = str(CreateIndex(indexes["ix_change_log_business_id_created_at"]).compile(dialect=dialect()))
    assert "(business_id, created_at DESC)" in change_index
    assert list(models.StatsDaily.__table__.primary_key.columns.keys()) == ["platform", "ad_id", "date"]
    assert list(models.BudgetLedger.__table__.primary_key.columns.keys()) == ["business_id", "month", "platform"]
    assert any(
        isinstance(constraint, UniqueConstraint) and list(constraint.columns.keys()) == ["yandex_uid"]
        for constraint in models.User.__table__.constraints
    )


def test_money_json_arrays_defaults_and_cascades() -> None:
    money_columns = [c for table in Base.metadata.tables.values() for c in table.c if c.name.endswith("_kop")]
    assert len(money_columns) == 11
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
        "campaigns": {"platform", "type"},
        "targets": {"kind"},
        "ads": {"status", "role"},
        "assets": {"kind"},
        "stats_daily": {"platform"},
        "budget_ledger": {"platform"},
        "experiments": {"hypothesis", "status", "verdict"},
        "change_log": {"actor", "kind", "platform", "status"},
    }
    for table_name, columns in expected.items():
        table = Base.metadata.tables[table_name]
        checks = [str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)]
        for name in columns:
            assert isinstance(table.c[name].type, Text)
            assert any(sql.startswith(f"{name} IN (") for sql in checks)
