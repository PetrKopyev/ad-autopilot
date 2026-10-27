"""Экраны на настоящем PostgreSQL: ScreensRepository читает часть столбцов (load_only с raiseload)."""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.autopilot import ScreensRepository
from app.api.stats import find_business, get_business
from app.auth.sessions import AuthRepository
from app.db.models import ChangeLog, MarkingProfile, User
from app.services.screens import ScreensService
from tests.worker.test_repositories import NOW, TODAY, tree, worker

pytestmark = pytest.mark.db


def screens(db_session) -> ScreensService:
    # Как в запросе: объекты читаются заново, а не берутся из сессии, где их только что создали.
    db_session.expire_all()
    return ScreensService(worker(ScreensRepository(db_session)))


def test_settings_show_saved_marking_masked(db_session) -> None:
    world = tree(ScreensRepository(db_session))
    db_session.add(
        MarkingProfile(
            business_id=world.business.id,
            subject_type="ip",
            inn_enc="526317984689",
            name="ИП Иванов",
            phone_enc="+79001231122",
            confirmed_in_platform={"direct": True},
        )
    )
    db_session.commit()
    marking = screens(db_session).business(world.business)["marking"]
    assert marking == {
        "subject_type": "ip",
        "inn": "",
        "name": "ИП Иванов",
        "phone": "",
        "confirmed_in_platform": {"direct": True},
        "saved": True,
        "inn_masked": "•••4689",
        "phone_masked": "+7•••1122",
    }


def test_ad_paused_by_owner_has_no_repeated_reason(db_session) -> None:
    repository = ScreensRepository(db_session)
    world = tree(repository)
    ad = world.ads["direct"][0]
    ad.status = "paused_owner"
    operation = {"entity_type": "ad", "entity_id": ad.external_id, "field": "status"}
    repository.save(
        ad,
        ChangeLog(
            business_id=world.business.id,
            actor="owner",
            kind="pause",
            platform="direct",
            entity_type="ad",
            entity_id=ad.external_id,
            operations=[operation | {"op": "set_status", "before": "active", "after": "paused_owner"}],
            reason_code="OWNER_AD_STATUS",
            reason_params={},
            reason_text=f"Вы остановили объявление «{ad.title}».",
            evidence={},
            status="applied",
            created_at=NOW,
        ),
    )
    items = screens(db_session).ads(world.business, TODAY, TODAY)["items"]
    row = next(item for item in items if item["id"] == ad.id)
    assert row["status"] == "paused_owner" and row["reason"] is None


def test_user_without_business_gets_none_and_screens_get_404(db_session) -> None:
    user = User(yandex_uid="nobiz-" + uuid4().hex, login="test", consent_at=NOW)
    db_session.add(user)
    db_session.commit()
    repository = AuthRepository(db_session)
    assert find_business(user, repository, None) is None
    with pytest.raises(HTTPException) as error:
        get_business(find_business(user, repository, None))
    assert error.value.status_code == 404
    world = tree(ScreensRepository(db_session))
    with pytest.raises(HTTPException) as error:
        find_business(user, repository, world.business.id)
    assert error.value.status_code == 404
