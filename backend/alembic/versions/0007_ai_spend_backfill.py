"""Бэкфилл ai_spend_ledger из llm_calls/art_calls и индекс created_at (В10 круга 3).

До ledger лимиты считались по llm_calls/art_calls, которые удаляются каскадом с бизнесом:
без переноса пулы владельцев и суточный потолок начинались бы с нуля, и обход лимита
пересозданием аккаунта работал бы до понедельника. Переносим траты последних 8 дней
(окно недельного пула). owner_key считается тем же HMAC, что в OnboardingRepository.owner_key:
ключ — AI_OWNER_KEY_SECRET из окружения миграции или его fallback SECRET_KEY.

Откат: индекс удаляется; перенесённые строки остаются — они неотличимы от обычных и
безвредны: лимиты смотрят максимум неделю назад, свипер удаляет строки старше 14 дней.
"""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from alembic import op

revision = "0007_ai_spend_backfill"
down_revision = "0006_ads_title2"
branch_labels = None
depends_on = None

BACKFILL_DAYS = 8


def _owner_keys(bind) -> dict[int, str]:
    from app.config import settings

    key = (settings.ai_owner_key_secret or settings.secret_key.get_secret_value()).encode()
    rows = bind.execute(sa.text("SELECT id, yandex_uid FROM users WHERE yandex_uid IS NOT NULL"))
    return {row.id: hmac.new(key, row.yandex_uid.encode(), hashlib.sha256).hexdigest() for row in rows}


def upgrade() -> None:
    op.create_index("ix_ai_spend_ledger_created_at", "ai_spend_ledger", ["created_at"])
    bind = op.get_bind()
    keys = _owner_keys(bind)
    if not keys:
        return
    cutoff = datetime.now(UTC) - timedelta(days=BACKFILL_DAYS)
    rows = bind.execute(
        sa.text(
            """
            SELECT calls.business_id, calls.kind, calls.cost_kop, calls.created_at, businesses.user_id
            FROM (
                SELECT business_id, 'llm' AS kind, cost_kop, created_at FROM llm_calls
                UNION ALL
                SELECT business_id, 'art', cost_kop, created_at FROM art_calls
            ) calls
            JOIN businesses ON businesses.id = calls.business_id
            WHERE calls.cost_kop > 0 AND calls.created_at >= :cutoff
            """
        ),
        {"cutoff": cutoff},
    )
    op.bulk_insert(
        sa.table(
            "ai_spend_ledger",
            sa.column("owner_key", sa.Text),
            sa.column("business_id", sa.BigInteger),
            sa.column("kind", sa.Text),
            sa.column("cost_kop", sa.BigInteger),
            sa.column("final", sa.Boolean),
            sa.column("created_at", sa.DateTime(timezone=True)),
        ),
        [
            {
                "owner_key": keys[row.user_id],
                "business_id": row.business_id,
                "kind": row.kind,
                "cost_kop": row.cost_kop,
                "final": True,
                "created_at": row.created_at,
            }
            for row in rows
            if row.user_id in keys
        ],
    )


def downgrade() -> None:
    # Перенесённые строки намеренно не удаляются: неотличимы от обычных и удаляются свипером по сроку.
    op.drop_index("ix_ai_spend_ledger_created_at", table_name="ai_spend_ledger")
