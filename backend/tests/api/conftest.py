import asyncio
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.db.models import Asset, Business, Plan, Session, User
from tests.services.test_onboarding import BUDGET_KOP, NOW, SITE, TODAY, make_world, onboard, picture

SESSIONS = {"test-session": 1, "other-session": 2}


@pytest.fixture
def api(tmp_path) -> Iterator[tuple[TestClient, SimpleNamespace]]:
    from app.api import business, plan
    from app.api.stats import get_today
    from app.auth.sessions import CSRF_COOKIE, SESSION_COOKIE, get_repository

    world = make_world(tmp_path)
    asyncio.run(onboard(world))
    draft = asyncio.run(world.plans.get_plan(world.business.id, NOW, TODAY))
    other = Business(user_id=2, site_url=SITE, city="Самара", month_budget_kop=BUDGET_KOP, status="plan_ready")
    world.repository.add(other)
    (world.media / str(other.id)).mkdir(parents=True)
    (world.media / str(other.id) / "x.jpg").write_bytes(picture(9))
    foreign_asset = Asset(business_id=other.id, kind="upload", file_path=f"{other.id}/x.jpg", selected=True)
    foreign_plan = Plan(business_id=other.id, version=1, status="draft", payload={"groups": [], "assets": {}})
    world.repository.add(foreign_asset)
    world.repository.add(foreign_plan)

    class Auth:
        def get_session(self, value):
            user_id = SESSIONS.get(value)
            return Session(id=value, user_id=user_id, csrf_secret="test-csrf") if user_id else None

        def get_user(self, user_id):
            return User(id=user_id)

        def get_business(self, user_id, business_id):
            found = [
                item for item in world.repository.rows(Business, user_id=user_id) if business_id in (None, item.id)
            ]
            if not found:
                raise HTTPException(404, "Сначала заполните сведения о бизнесе.")
            return found[0]

    app = FastAPI()
    app.include_router(business.router)
    app.include_router(plan.router)
    app.dependency_overrides[get_repository] = Auth
    app.dependency_overrides[business.get_onboarding_service] = lambda: world.onboarding
    app.dependency_overrides[plan.get_plan_service] = lambda: world.plans
    app.dependency_overrides[plan.get_launch_service] = lambda: world.launches
    app.dependency_overrides[plan.get_video_builder] = lambda: world.plans.build_videos
    app.dependency_overrides[get_today] = lambda: TODAY
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, "test-session")
        client.cookies.set(CSRF_COOKIE, "test-csrf")
        client.headers["X-CSRF-Token"] = "test-csrf"
        yield (
            client,
            SimpleNamespace(
                world=world, plan=draft, other=other, foreign_asset=foreign_asset, foreign_plan=foreign_plan
            ),
        )
