"""Включения, которые ждали проверки объявлений pending-записью (error = MODERATION_PENDING), становятся применёнными:
действие сделано, площадка проверяет. Их кампании получают review_since, включённые объявления — статус moderation.
Кампания остановленного бизнеса (не running, kill_switch) получает паузу: включение ждало одобрения после остановки.

Откат: записи с пометкой migrated_review снова pending с прежним текстом, локальные статусы — прежние из пометки.
"""

import json

import sqlalchemy as sa

from alembic import op

revision = "0009_moderation_wait_state"
down_revision = "0008_campaign_review_since"
branch_labels = None
depends_on = None

MODERATION_PENDING = "Площадка ещё проверяет объявление. Включим его, как только одобрит."


def _enabled(operations: list[dict], entity_type: str) -> list[str]:
    return [
        op["entity_id"]
        for op in operations
        if op.get("entity_type") == entity_type and op.get("field") == "status" and op.get("after") == "active"
    ]


def _ads(bind, business_id: int, platform: str, ids: list[str]):
    return bind.execute(
        sa.text(
            """
            SELECT ads.id, ads.external_id, ads.status FROM ads
            JOIN ad_groups ON ad_groups.id = ads.ad_group_id
            JOIN campaigns ON campaigns.id = ad_groups.campaign_id
            WHERE campaigns.business_id = :business AND campaigns.platform = :platform
            AND ads.external_id = ANY(:ids)
            """
        ),
        {"business": business_id, "platform": platform, "ids": ids},
    ).all()


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT change_log.id, change_log.business_id, change_log.platform, change_log.operations,
                   change_log.evidence, change_log.created_at, businesses.status AS business_status,
                   businesses.settings AS settings
            FROM change_log JOIN businesses ON businesses.id = change_log.business_id
            WHERE change_log.status = 'pending' AND change_log.evidence -> '_service' ->> 'error' = :error
            """
        ),
        {"error": MODERATION_PENDING},
    ).all()
    for row in rows:
        service = row.evidence["_service"]
        meta = (row.settings or {}).get("_worker") or {}
        running = row.business_status == "running" and not meta.get("kill_switch") and not meta.get("pending_stop")
        campaign = bind.execute(
            sa.text(
                "SELECT id, status FROM campaigns WHERE business_id = :business AND platform = :platform "
                "AND external_id = :external"
            ),
            {"business": row.business_id, "platform": row.platform, "external": service["refs"]["campaign_id"]},
        ).first()
        ads = _ads(bind, row.business_id, row.platform, _enabled(row.operations, "ad"))
        previous = {"campaign": campaign.status if campaign else None, "ads": {str(ad.id): ad.status for ad in ads}}
        if campaign is not None:
            bind.execute(
                sa.text("UPDATE campaigns SET status = :status, review_since = :since WHERE id = :id"),
                {
                    "status": "active" if running else "paused_auto",
                    "since": service.get("awaiting_since") or row.created_at,
                    "id": campaign.id,
                },
            )
        for ad in ads:
            if ad.status not in {"active", "moderation", "rejected"}:
                bind.execute(sa.text("UPDATE ads SET status = 'moderation' WHERE id = :id"), {"id": ad.id})
        review = sorted(_enabled(row.operations, "ad") + _enabled(row.operations, "campaign"))
        evidence = row.evidence | {
            "_service": service | {"error": None, "differences": [], "review": review, "migrated_review": previous}
        }
        bind.execute(
            sa.text("UPDATE change_log SET status = 'applied', evidence = CAST(:evidence AS jsonb) WHERE id = :id"),
            {"evidence": json.dumps(evidence, ensure_ascii=False), "id": row.id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, business_id, platform, evidence FROM change_log "
            "WHERE evidence -> '_service' -> 'migrated_review' IS NOT NULL"
        )
    ).all()
    for row in rows:
        service = dict(row.evidence["_service"])
        previous = service.pop("migrated_review")
        service.pop("review", None)
        service.pop("differences", None)
        service["error"] = MODERATION_PENDING
        if previous["campaign"] is not None:
            bind.execute(
                sa.text(
                    "UPDATE campaigns SET status = :status, review_since = NULL WHERE business_id = :business "
                    "AND platform = :platform AND external_id = :external"
                ),
                {
                    "status": previous["campaign"],
                    "business": row.business_id,
                    "platform": row.platform,
                    "external": service["refs"]["campaign_id"],
                },
            )
        for ad_id, status in previous["ads"].items():
            bind.execute(
                sa.text("UPDATE ads SET status = :status WHERE id = :id"), {"status": status, "id": int(ad_id)}
            )
        bind.execute(
            sa.text("UPDATE change_log SET status = 'pending', evidence = CAST(:evidence AS jsonb) WHERE id = :id"),
            {"evidence": json.dumps(row.evidence | {"_service": service}, ensure_ascii=False), "id": row.id},
        )
