import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, create_mock_engine, inspect, text
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


def canonical(statement, dialect) -> tuple[str, ...]:
    sql = str(statement.compile(dialect=dialect))
    return tuple(sorted(" ".join(line.strip().rstrip(",").split()) for line in sql.splitlines() if line.strip()))


def test_migration_matches_metadata_without_database() -> None:
    actual = []
    engine = create_mock_engine("postgresql+psycopg://", lambda sql, *args, **kwargs: actual.append(sql))
    context = MigrationContext.configure(connection=engine)
    revision = load_revision()
    with Operations.context(context):
        revision.upgrade()
    actual_ddl = sorted(canonical(statement, engine.dialect) for statement in actual)
    actual.clear()
    Base.metadata.create_all(engine, checkfirst=False)
    expected_ddl = sorted(canonical(statement, engine.dialect) for statement in actual)
    assert actual_ddl == expected_ddl
    actual.clear()
    with Operations.context(context):
        revision.downgrade()
    actual_ddl = sorted(canonical(statement, engine.dialect) for statement in actual)
    actual.clear()
    Base.metadata.drop_all(engine, checkfirst=False)
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
