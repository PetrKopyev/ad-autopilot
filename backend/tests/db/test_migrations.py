import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, MetaData, create_engine, create_mock_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema

from alembic import command
from app.db import models  # noqa: F401
from app.db.base import Base

BACKEND = Path(__file__).resolve().parents[2]


def load_revision():
    spec = importlib.util.spec_from_file_location("initial_revision", BACKEND / "alembic/versions/0001_initial.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def metadata_before_additions() -> MetaData:
    """Metadata as of 0001_initial: without the columns, index and statuses added by 0002."""
    metadata = MetaData(naming_convention=Base.metadata.naming_convention)
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata)
    for name in ("campaigns", "ad_groups", "targets"):
        metadata.tables[name]._columns.remove(metadata.tables[name].c.frozen_until)
    art_calls = metadata.tables["art_calls"]
    for index in list(art_calls.indexes):
        art_calls.indexes.discard(index)
    for column in ("operation_id", "seed"):
        art_calls._columns.remove(art_calls.c[column])
    for name in ("change_log", "experiments"):
        for constraint in metadata.tables[name].constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name == f"ck_{name}_status_values":
                constraint.sqltext = sa.text(str(constraint.sqltext).replace("'proposed', ", ""))
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


@pytest.mark.db
def test_migration_upgrade_downgrade(test_settings) -> None:
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
                command.upgrade(config, "head")
                assert set(inspect(connection).get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}
                context = MigrationContext.configure(
                    connection, opts={"compare_type": True, "compare_server_default": True}
                )
                assert compare_metadata(context, Base.metadata) == []
                command.downgrade(config, "base")
                assert inspect(connection).get_table_names() == ["alembic_version"]
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


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
