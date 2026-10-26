import importlib.util
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    CheckConstraint,
    Connection,
    MetaData,
    PrimaryKeyConstraint,
    UniqueConstraint,
    create_engine,
    create_mock_engine,
    inspect,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema

from alembic import command
from app.db import models  # noqa: F401
from app.db.base import Base

BACKEND = Path(__file__).resolve().parents[2]


def load_revision(name: str = "0001_initial"):
    spec = importlib.util.spec_from_file_location(name, BACKEND / f"alembic/versions/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LATER_COLUMNS = {
    "campaigns": ("frozen_until", "created_at"),
    "ad_groups": ("frozen_until",),
    "targets": ("frozen_until",),
    "art_calls": ("operation_id", "seed", "created_at"),
    "businesses": ("created_at",),
    "platform_accounts": ("created_at",),
    "ads": ("created_at",),
    "assets": ("created_at",),
    "llm_calls": ("created_at",),
    "worker_runs": ("slot_key",),
    "change_log": ("idempotency_key",),
}
INITIAL_INDEXES = {"ix_stats_daily_business_id_date", "ix_change_log_business_id_created_at"}


def metadata_before_additions() -> MetaData:
    """Metadata as of 0001_initial: without everything that 0002 and 0003 add or change."""
    metadata = MetaData(naming_convention=Base.metadata.naming_convention)
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata)
    later_checks = {f"ck_{table}_{name}" for table, name, _ in load_revision("0003_storage_integrity").checks()}
    for table in metadata.tables.values():
        for index in [index for index in table.indexes if index.name not in INITIAL_INDEXES]:
            table.indexes.discard(index)
        for constraint in list(table.constraints):
            if constraint.name in later_checks or (isinstance(constraint, UniqueConstraint) and table.name != "users"):
                table.constraints.discard(constraint)
        for column in LATER_COLUMNS.get(table.name, ()):
            table._columns.remove(table.c[column])
    for name in ("change_log", "experiments"):
        for constraint in metadata.tables[name].constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name == f"ck_{name}_status_values":
                constraint.sqltext = sa.text(str(constraint.sqltext).replace("'proposed', ", ""))
    stats = metadata.tables["stats_daily"]
    stats.append_constraint(PrimaryKeyConstraint("platform", "ad_id", "date", name="pk_stats_daily"))
    next(iter(metadata.tables["campaigns"].c.platform_account_id.foreign_keys)).constraint.ondelete = "CASCADE"
    return metadata


def canonical(statement, dialect) -> tuple[str, ...]:
    sql = str(statement.compile(dialect=dialect))
    return tuple(sorted(" ".join(line.strip().rstrip(",").split()) for line in sql.splitlines() if line.strip()))


def test_migration_matches_metadata_without_database() -> None:
    actual = []
    engine = create_mock_engine("postgresql+psycopg://", lambda sql, *args, **kwargs: actual.append(sql))
    context = MigrationContext.configure(connection=engine)
    revision = load_revision()
    metadata = metadata_before_additions()
    with Operations.context(context):
        revision.upgrade()
    actual_ddl = sorted(canonical(statement, engine.dialect) for statement in actual)
    actual.clear()
    metadata.create_all(engine, checkfirst=False)
    expected_ddl = sorted(canonical(statement, engine.dialect) for statement in actual)
    assert actual_ddl == expected_ddl
    actual.clear()
    with Operations.context(context):
        revision.downgrade()
    actual_ddl = sorted(canonical(statement, engine.dialect) for statement in actual)
    actual.clear()
    metadata.drop_all(engine, checkfirst=False)
    expected_ddl = sorted(canonical(statement, engine.dialect) for statement in actual)
    # Explicit index removal is redundant when the parent table is dropped.
    assert [ddl for ddl in actual_ddl if not ddl[0].startswith("DROP INDEX")] == expected_ddl


@contextmanager
def scratch_schema() -> Iterator[tuple[Connection, Config]]:
    """Отдельная схема в транзакции, которая в конце откатывается вместе со всеми миграциями."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_engine(database_url)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.skip("TEST_DATABASE_URL must point to PostgreSQL")
    config = Config(str(BACKEND / "alembic.ini"))
    schema = "test_migrations_" + uuid4().hex
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(CreateSchema(schema))
                connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                config.attributes["connection"] = connection
                yield connection, config
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def assert_matches_metadata(connection: Connection) -> None:
    assert set(inspect(connection).get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}
    context = MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True})
    assert compare_metadata(context, Base.metadata) == []


@pytest.mark.db
def test_migration_upgrade_downgrade(test_settings) -> None:
    with scratch_schema() as (connection, config):
        command.upgrade(config, "head")
        assert_matches_metadata(connection)
        command.downgrade(config, "base")
        assert inspect(connection).get_table_names() == ["alembic_version"]
        command.upgrade(config, "head")
        assert_matches_metadata(connection)


SCHEMA_CATALOG = """
SELECT 'constraint', rel.relname, con.conname, pg_get_constraintdef(con.oid)
FROM pg_constraint AS con JOIN pg_class AS rel ON rel.oid = con.conrelid
WHERE rel.relnamespace = current_schema()::regnamespace
UNION ALL
SELECT 'index', tablename, indexname, replace(indexdef, current_schema() || '.', '')
FROM pg_indexes WHERE schemaname = current_schema()
UNION ALL
SELECT 'column', table_name, column_name, concat_ws(' ', data_type, is_nullable, column_default)
FROM information_schema.columns WHERE table_schema = current_schema()
"""


@pytest.mark.db
def test_migrated_schema_has_exactly_the_model_constraints_indexes_and_defaults(test_settings) -> None:
    """compare_metadata не сравнивает CHECK, условия частичных индексов и ON DELETE — сверяем каталоги PostgreSQL."""
    with scratch_schema() as (connection, config):
        command.upgrade(config, "head")
        migrated = {row for row in connection.execute(text(SCHEMA_CATALOG)) if row[1] != "alembic_version"}
    with scratch_schema() as (connection, _):
        Base.metadata.create_all(connection)
        created = set(connection.execute(text(SCHEMA_CATALOG)))
    assert migrated == created


def insert(connection: Connection, table: str, **values) -> int | None:
    columns = ", ".join(values)
    params = ", ".join(
        f"CAST(:{name} AS jsonb)" if isinstance(value, dict | list) else f":{name}" for name, value in values.items()
    )
    returning = " RETURNING id" if table not in {"stats_daily", "budget_ledger"} else ""
    result = connection.execute(
        text(f"INSERT INTO {table} ({columns}) VALUES ({params}){returning}"),
        {name: json.dumps(value) if isinstance(value, dict | list) else value for name, value in values.items()},
    )
    return result.scalar() if returning else None


def seed_0002(connection: Connection) -> dict[str, int]:
    """Данные, которые могла оставить схема 0002, включая повторы ключей и строки статистики чужой площадки."""
    ids = {}
    ids["user"] = insert(connection, "users", yandex_uid="u", login="l", consent_at=datetime(2026, 3, 1, tzinfo=UTC))
    ids["business"] = insert(
        connection,
        "businesses",
        user_id=ids["user"],
        site_url="https://example.test",
        city="Самара",
        month_budget_kop=100_000,
        settings={"_ai": {"week": "2026-03-02", "base": 10}, "_worker": {"started_on": "2026-03-01"}},
    )
    business = ids["business"]
    ids["account"] = insert(
        connection,
        "platform_accounts",
        business_id=business,
        platform="direct",
        auth_mode="oauth",
        access_token_enc="t",
    )
    ids["campaign"] = insert(
        connection,
        "campaigns",
        business_id=business,
        platform_account_id=ids["account"],
        platform="direct",
        external_id="c-1",
        type="unified",
        name="Т",
        status="active",
        daily_limit_kop=100,
        period_cap_kop=1_000,
    )
    ids["group"] = insert(
        connection, "ad_groups", campaign_id=ids["campaign"], external_id="g-1", theme="Т", geo={}, status="active"
    )
    ids["ad"] = insert(
        connection, "ads", ad_group_id=ids["group"], external_id="a-1", title="Т", text="Т", status="active"
    )
    insert(
        connection,
        "stats_daily",
        business_id=business,
        platform="direct",
        campaign_id=ids["campaign"],
        ad_id=ids["ad"],
        date=date(2026, 3, 2),
        cost_kop=100,
    )
    run = {"business_id": business, "kind": "optimize", "units_spent": 0}
    ids["run_failed"] = insert(
        connection,
        "worker_runs",
        **run,
        status="failed",
        log=[{"key": "optimize:2026-03-02"}, {"reason_code": "JOB_FAILED"}],
    )
    ids["run_ok"] = insert(connection, "worker_runs", **run, status="ok", log=[{"key": "optimize:2026-03-02"}])
    ids["run_retry"] = insert(connection, "worker_runs", **run, status="failed", log=[{"key": "optimize:2026-03-02"}])
    ids["heartbeat"] = insert(connection, "worker_runs", kind="heartbeat", status="ok", units_spent=0, log=[])
    ids["notice"] = insert(
        connection,
        "worker_runs",
        business_id=business,
        kind="notification",
        status="ok",
        units_spent=0,
        log=[{"key": "month_notice:2026-03-01", "text": "Т"}],
    )
    change = {
        "business_id": business,
        "actor": "autopilot",
        "kind": "pause",
        "platform": "direct",
        "entity_type": "ad",
        "entity_id": "a-1",
        "operations": [],
        "reason_code": "X",
        "reason_text": "Т",
    }
    ids["change_failed"] = insert(
        connection, "change_log", **change, status="failed", evidence={"worker_key": "optimize:2026-03-02:1:a-1"}
    )
    ids["change_applied"] = insert(
        connection, "change_log", **change, status="applied", evidence={"worker_key": "optimize:2026-03-02:1:a-1"}
    )
    ids["proposal"] = insert(
        connection, "change_log", **change, status="proposed", evidence={"experiment_key": "experiment:1:text"}
    )
    ids["owner"] = insert(connection, "change_log", **change, status="applied", evidence={})
    ids["llm"] = insert(
        connection,
        "llm_calls",
        business_id=business,
        purpose="x",
        model="m",
        tokens_in=1,
        tokens_out=1,
        cost_kop=10,
        status="DONE",
    )
    ids["art"] = insert(connection, "art_calls", business_id=business, cost_kop=200, status="succeeded")
    return ids


@pytest.mark.db
def test_upgrade_with_data_backfills_keys_and_downgrade_keeps_rows(test_settings) -> None:
    with scratch_schema() as (connection, config):
        command.upgrade(config, "0002_autopilot_additions")
        ids = seed_0002(connection)
        command.upgrade(config, "head")
        assert_matches_metadata(connection)
        runs = dict(connection.execute(text("SELECT id, slot_key FROM worker_runs")).all())
        assert runs == {
            ids["run_failed"]: None,
            ids["run_ok"]: "optimize:2026-03-02",
            ids["run_retry"]: None,
            ids["heartbeat"]: None,
            ids["notice"]: "month_notice:2026-03-01",
        }
        keys = dict(connection.execute(text("SELECT id, idempotency_key FROM change_log")).all())
        assert keys == {
            ids["change_failed"]: None,
            ids["change_applied"]: "optimize:2026-03-02:1:a-1",
            ids["proposal"]: "experiment:1:text",
            ids["owner"]: None,
        }
        assert connection.execute(text("SELECT platform, cost_kop FROM stats_daily")).all() == [("direct", 100)]
        assert connection.execute(text("SELECT settings FROM businesses")).scalar() == {
            "_worker": {"started_on": "2026-03-01"}
        }
        # Прошлые траты ИИ не попадают в неделю выкатки, новые строки получают текущее время.
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        assert connection.execute(text("SELECT created_at FROM llm_calls")).scalar() == epoch
        assert connection.execute(text("SELECT created_at FROM art_calls")).scalar() == epoch
        insert(
            connection,
            "llm_calls",
            business_id=ids["business"],
            purpose="x",
            model="m",
            tokens_in=1,
            tokens_out=1,
            cost_kop=1,
            status="DONE",
        )
        insert(connection, "art_calls", business_id=ids["business"], cost_kop=1, status="pending")
        fresh = "SELECT bool_and(created_at > now() - interval '1 minute') FROM {} WHERE id <> :id"
        assert connection.execute(text(fresh.format("llm_calls")), {"id": ids["llm"]}).scalar()
        assert connection.execute(text(fresh.format("art_calls")), {"id": ids["art"]}).scalar()

        command.downgrade(config, "0002_autopilot_additions")
        counts = {
            table: connection.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            for table in ("worker_runs", "change_log", "stats_daily", "campaigns", "ads", "llm_calls")
        }
        assert counts == {"worker_runs": 5, "change_log": 4, "stats_daily": 1, "campaigns": 1, "ads": 1, "llm_calls": 2}
        assert connection.execute(text("SELECT count(*) FROM art_calls")).scalar() == 2
        columns = {column["name"] for column in inspect(connection).get_columns("change_log")}
        assert "idempotency_key" not in columns
        # Старый код снова видит свои ключи: они остались в evidence и log.
        assert (
            connection.execute(
                text("SELECT count(*) FROM change_log WHERE evidence ->> 'worker_key' IS NOT NULL")
            ).scalar()
            == 2
        )

        command.upgrade(config, "head")
        assert_matches_metadata(connection)
        assert (
            connection.execute(text("SELECT slot_key FROM worker_runs WHERE id = :id"), {"id": ids["run_ok"]}).scalar()
            == "optimize:2026-03-02"
        )
        command.downgrade(config, "base")
        assert inspect(connection).get_table_names() == ["alembic_version"]


@pytest.mark.db
def test_upgrade_stops_on_statistics_of_another_platform(test_settings) -> None:
    with scratch_schema() as (connection, config):
        command.upgrade(config, "0002_autopilot_additions")
        ids = seed_0002(connection)
        insert(
            connection,
            "stats_daily",
            business_id=ids["business"],
            platform="vk",
            campaign_id=ids["campaign"],
            ad_id=ids["ad"],
            date=date(2026, 3, 2),
            cost_kop=999,
        )
        with pytest.raises(RuntimeError, match="stats_daily: 1 строк"):
            command.upgrade(config, "head")


@pytest.mark.db
def test_downgrade_of_0002_turns_proposed_rows_into_old_statuses(test_settings) -> None:
    with scratch_schema() as (connection, config):
        command.upgrade(config, "0002_autopilot_additions")
        ids = seed_0002(connection)
        insert(connection, "ads", ad_group_id=ids["group"], external_id="a-2", title="Т", text="Т", status="active")
        variant = connection.execute(text("SELECT id FROM ads WHERE external_id = 'a-2'")).scalar()
        experiment = insert(
            connection,
            "experiments",
            business_id=ids["business"],
            ad_group_id=ids["group"],
            hypothesis="text",
            control_ad_id=ids["ad"],
            variant_ad_id=variant,
            metric="ctr",
            planned_n_per_arm=100,
            min_effect=0.2,
            deadline=datetime(2026, 3, 20, tzinfo=UTC),
            status="proposed",
        )
        command.downgrade(config, "0001_initial")
        assert (
            connection.execute(text("SELECT status FROM experiments WHERE id = :id"), {"id": experiment}).scalar()
            == "cancelled"
        )
        assert (
            connection.execute(text("SELECT status FROM change_log WHERE id = :id"), {"id": ids["proposal"]}).scalar()
            == "failed"
        )
        assert connection.execute(text("SELECT count(*) FROM change_log WHERE status = 'proposed'")).scalar() == 0


@pytest.mark.db
def test_upgrade_without_external_connection_commits(test_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        pytest.skip("TEST_DATABASE_URL must point to PostgreSQL")
    schema = "test_migrations_" + uuid4().hex
    scoped_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        monkeypatch.setattr(test_settings, "database_url", scoped_url.render_as_string(hide_password=False))
        command.upgrade(Config(str(BACKEND / "alembic.ini")), "head")
        with engine.connect() as connection:
            assert set(inspect(connection).get_table_names(schema=schema)) == set(Base.metadata.tables) | {
                "alembic_version"
            }
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
