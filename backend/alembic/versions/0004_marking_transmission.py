"""Marking transmission: статус передачи данных рекламодателя по площадкам.

Столбец marking_profiles.transmission — JSONB {площадка: {status, attempted_at}}; пишется сервисом при сохранении
данных рекламодателя. Константный server_default: добавление столбца занимает миллисекунды (PostgreSQL 11+ не
переписывает таблицу), lock_timeout из alembic/env.py не мешает запросам API.
Downgrade теряет статусы передач — повторное сохранение данных рекламодателя заполнит их заново.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_marking_transmission"
down_revision = "0003_storage_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "marking_profiles",
        sa.Column(
            "transmission",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("marking_profiles", "transmission")
