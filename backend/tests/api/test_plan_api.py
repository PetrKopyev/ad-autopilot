import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.content.images import MAX_BYTES
from app.db.models import ArtCall, Asset, Campaign, LLMCall
from app.services.plan import REGENERATE_COUNT
from tests.services.test_onboarding import BUDGET_KOP, picture


def png(size: int = 600) -> bytes:
    output = BytesIO()
    Image.open(BytesIO(picture(size))).resize((size, size)).save(output, format="PNG")
    return output.getvalue()


def own_asset(context) -> int:
    return context.plan["groups"][0]["assets"][0]["id"]


def upload(client: TestClient, group_id: object = 1, data: bytes | None = None, **file) -> object:
    name, content_type = file.get("name", "photo.jpg"), file.get("content_type", "image/jpeg")
    return client.post(
        "/api/plan/upload-photo",
        data={"group_id": str(group_id)},
        files={"file": (name, png() if data is None else data, content_type)},
    )


def routes(context) -> list[tuple[str, str, dict]]:
    return [
        ("GET", "/api/plan", {}),
        ("POST", "/api/plan/regenerate-images", {"json": {"group_id": 1}}),
        ("POST", "/api/plan/upload-photo", {"data": {"group_id": "1"}, "files": {"file": ("a.png", png())}}),
        ("PUT", "/api/plan/assets", {"json": {"asset_ids": [own_asset(context)]}}),
        ("POST", "/api/plan/launch", {"json": {"plan_id": context.plan["id"], "medical_acknowledged": True}}),
        ("GET", f"/api/plan/assets/{own_asset(context)}/file", {}),
        ("GET", "/api/business/site-check", {}),
    ]


def test_login_required(api) -> None:
    client, context = api
    client.cookies.clear()
    for method, path, options in routes(context):
        assert client.request(method, path, **options).status_code == 401, path


def test_csrf_required_for_changes(api) -> None:
    client, context = api
    del client.headers["X-CSRF-Token"]
    for method, path, options in routes(context):
        if method != "GET":
            assert client.request(method, path, **options).status_code == 403, path
    assert not context.world.repository.rows(Campaign)


def test_foreign_business_is_not_found(api) -> None:
    client, context = api
    for method, path, options in routes(context):
        response = client.request(method, path, params={"business_id": context.other.id}, **options)
        assert response.status_code == 404, path
    client.cookies.set("__Host-session", "other-session")
    assert client.get(f"/api/plan/assets/{own_asset(context)}/file").status_code == 404


def test_foreign_plan_and_asset_are_not_found(api) -> None:
    client, context = api
    asset_id = context.foreign_asset.id
    assert client.get(f"/api/plan/assets/{asset_id}/file").status_code == 404
    assert client.put("/api/plan/assets", json={"asset_ids": [asset_id]}).status_code == 404
    response = client.post("/api/plan/launch", json={"plan_id": context.foreign_plan.id})
    assert response.status_code == 404 and not context.world.repository.rows(Campaign)
    own = client.get(f"/api/plan/assets/{own_asset(context)}/file")
    assert own.status_code == 200 and own.headers["content-type"] == "image/jpeg"


def uploads(context) -> int:
    return len(context.world.repository.rows(Asset, business_id=context.world.business.id, kind="upload"))


def test_upload_accepts_image_by_content_not_by_name(api) -> None:
    client, context = api
    response = upload(client, name="photo.txt", content_type="text/plain")
    assert response.status_code == 200, response.text
    assert uploads(context) == 1
    response = upload(client, data=b"GIF89a not really an image", name="photo.jpg", content_type="image/jpeg")
    assert response.status_code == 422 and "JPG, PNG или WebP" in response.json()["detail"]
    assert uploads(context) == 1


def test_upload_size_is_limited_before_parsing(api, monkeypatch) -> None:
    client, context = api
    from app.api.plan import MAX_UPLOAD

    calls = []
    monkeypatch.setattr(context.world.plans, "upload_photo", lambda *args: calls.append(args))
    body = b"x" * (MAX_UPLOAD + 1)
    headers = {"Content-Type": "multipart/form-data; boundary=b"}
    response = client.post("/api/plan/upload-photo", content=body, headers=headers)
    assert response.status_code == 413 and "10 МБ" in response.json()["detail"]

    def chunks():
        for _ in range(MAX_UPLOAD // 65536 + 2):
            yield b"x" * 65536

    response = client.post("/api/plan/upload-photo", content=chunks(), headers=headers)
    assert response.status_code == 413
    response = upload(client, data=b"\xff" * (MAX_BYTES + 1))
    assert response.status_code == 413 and not calls


def test_upload_is_rejected_with_busy_text_when_decode_queue_is_full(api, monkeypatch) -> None:
    from app.content import images

    client, context = api
    monkeypatch.setattr(images, "UPLOAD_QUEUE", 0)
    response = upload(client)
    assert response.status_code == 503 and response.headers["Retry-After"] == "60"
    assert response.json()["detail"] == "Сервер сейчас занят. Попробуйте загрузить фото через минуту."
    assert uploads(context) == 0


def test_upload_is_rejected_with_busy_text_when_video_builds_hold_decode_slots(api, monkeypatch) -> None:
    from app.content import images

    client, context = api
    monkeypatch.setattr(images, "UPLOAD_WAIT", 0.3)
    # Слоты разбора в потоках держат сборки роликов: очередь цикла событий свободна, ждать приходится в потоке.
    images.DECODE_SLOTS.acquire()
    images.DECODE_SLOTS.acquire()
    # Загрузка в обход очереди ждала бы слот без срока: запрос идёт в своём потоке со сроком, и такая поломка роняет
    # тест, а не вешает его. Слоты отпускаются и тогда — зависший запрос доделывается, и пул закрывается.
    with ThreadPoolExecutor(1) as pool:
        try:
            response = pool.submit(upload, client).result(timeout=5)
        finally:
            images.DECODE_SLOTS.release()
            images.DECODE_SLOTS.release()
    assert response.status_code == 503 and response.headers["Retry-After"] == "60"
    assert response.json()["detail"] == "Сервер сейчас занят. Попробуйте загрузить фото через минуту."
    assert uploads(context) == 0
    assert upload(client).status_code == 200 and uploads(context) == 1


def test_upload_streamed_without_content_length_is_accepted(api) -> None:
    client, context = api
    boundary = "upload-boundary"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="group_id"\r\n\r\n1\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="фото.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode()
        + png()
        + f"\r\n--{boundary}--\r\n".encode()
    )

    def chunks():
        for start in range(0, len(body), 4096):
            yield body[start : start + 4096]

    response = client.post(
        "/api/plan/upload-photo",
        content=chunks(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert response.status_code == 200, response.text
    assert uploads(context) == 1


@pytest.mark.parametrize("group_id", ["", "abc", "1.5"])
def test_upload_requires_group_and_file(api, group_id) -> None:
    client, _ = api
    assert upload(client, group_id=group_id).status_code == 422
    assert client.post("/api/plan/upload-photo", data={"group_id": "1"}).status_code == 422


def test_plan_budget_is_in_kopecks(api) -> None:
    client, _ = api
    plan = client.get("/api/plan").json()
    assert plan["month_budget_kop"] == BUDGET_KOP
    assert plan["direct_budget_kop"] + plan["vk_budget_kop"] == BUDGET_KOP
    assert all(type(group["daily_limit_kop"]) is int for group in plan["groups"])


def test_launch_twice_is_idempotent_and_parallel_launch_conflicts(api) -> None:
    client, context = api
    repository = context.world.repository
    body = {"plan_id": context.plan["id"], "medical_acknowledged": True}
    repository.locked = True
    response = client.post("/api/plan/launch", json=body)
    assert response.status_code == 409 and "уже выполняется" in response.json()["detail"]
    assert repository.latest_plan(context.world.business.id).status == "draft" and not repository.rows(Campaign)
    repository.locked = False
    first = client.post("/api/plan/launch", json=body)
    assert first.status_code == 200, first.text
    campaigns = len(repository.rows(Campaign))
    again = client.post("/api/plan/launch", json=body)
    assert again.status_code == 200 and again.json() == first.json()
    assert len(repository.rows(Campaign)) == campaigns


def test_launch_blocked_is_conflict_with_reasons(api) -> None:
    client, context = api
    context.world.business.lead_goal_ids = []
    response = client.post("/api/plan/launch", json={"plan_id": context.plan["id"]})
    assert response.status_code == 409 and "счётчика Метрики" in response.json()["detail"]


def test_launch_conflict_inside_is_reported_as_conflict_not_gateway_error(api, monkeypatch) -> None:
    """ChangeConflict посреди запуска — понятный 409, а не 502: созданное попыткой остановлено, план помечен."""
    from app.services.changes import ChangeConflict

    client, context = api
    repository = context.world.repository

    async def conflict(*args) -> None:
        raise ChangeConflict("Площадка ещё не подтвердила предыдущее изменение.")

    monkeypatch.setattr(context.world.launches, "activate", conflict)
    response = client.post("/api/plan/launch", json={"plan_id": context.plan["id"], "medical_acknowledged": True})
    assert response.status_code == 409, response.text
    assert "не подтвердила предыдущее изменение" in response.json()["detail"]
    assert repository.latest_plan(context.world.business.id).status == "failed"
    created = repository.rows(Campaign)
    assert created and all(campaign.status == "paused_auto" for campaign in created)


def spent(context) -> int:
    repository = context.world.repository
    return sum(row.cost_kop for model in (ArtCall, LLMCall) for row in repository.rows(model))


def test_regenerate_charges_once_and_parallel_request_is_rejected_free(api) -> None:
    client, context = api
    repository = context.world.repository
    calls, remaining = len(repository.rows(ArtCall)), context.plan["generations_remaining"]
    response = client.post("/api/plan/regenerate-images", json={"group_id": 1})
    assert response.status_code == 200, response.text
    assert len(repository.rows(ArtCall)) - calls == REGENERATE_COUNT
    assert response.json()["generations_remaining"] == remaining - 1
    after = spent(context)
    repository.locked = True
    response = client.post("/api/plan/regenerate-images", json={"group_id": 1})
    assert response.status_code == 409 and "уже выполняется" in response.json()["detail"]
    assert spent(context) == after and len(repository.rows(ArtCall)) - calls == REGENERATE_COUNT


def test_regenerate_is_rate_limited_per_user(api, monkeypatch) -> None:
    client, context = api
    monkeypatch.setattr(context.world.plans, "regenerate_images", lambda *args: asyncio.sleep(0, result={}))
    statuses = [client.post("/api/plan/regenerate-images", json={"group_id": 1}).status_code for _ in range(7)]
    assert statuses == [200] * 6 + [429]
    response = client.post("/api/plan/regenerate-images", json={"group_id": 1})
    assert "Картинки обновляются слишком часто" in response.json()["detail"] and response.headers["Retry-After"]
    # Лимит по пользователю: другой владелец с того же адреса не заблокирован.
    client.cookies.set("__Host-session", "other-session")
    assert client.post("/api/plan/regenerate-images", json={"group_id": 1}).status_code == 200


@pytest.mark.parametrize("length", [str(10**9), "abc", "-1"])
def test_upload_content_length_is_checked_before_reading(api, monkeypatch, length) -> None:
    client, context = api
    calls = []
    monkeypatch.setattr(context.world.plans, "upload_photo", lambda *args: calls.append(args))
    headers = {"Content-Type": "multipart/form-data; boundary=b", "Content-Length": length}
    response = client.post("/api/plan/upload-photo", content=b"--b--\r\n", headers=headers)
    assert response.status_code == 413 and "10 МБ" in response.json()["detail"] and not calls


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\xff" * MAX_BYTES,
        b'<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600"><rect width="600" height="600"/></svg>',
        b"<!doctype html><html><body><img src=x onerror=alert(1)></body></html>",
    ],
)
def test_upload_rejects_non_images_up_to_size_limit(api, data) -> None:
    client, context = api
    response = upload(client, data=data, name="photo.jpg", content_type="image/jpeg")
    assert response.status_code == 422 and "JPG, PNG или WebP" in response.json()["detail"]
    assert uploads(context) == 0


def test_upload_to_unknown_group_is_not_found(api) -> None:
    client, context = api
    assert upload(client, group_id=999).status_code == 404
    assert uploads(context) == 0


def test_video_is_rebuilt_after_response_only_when_selection_changes(api) -> None:
    from app.api import plan

    client, context = api
    builds = []

    def build(business_id: int, built: dict) -> bool:
        builds.append(business_id)
        return True

    client.app.dependency_overrides[plan.get_video_builder] = lambda: build
    business_id = context.world.business.id
    images = context.world.repository.assets(business_id)
    selected = [asset.id for asset in images if asset.selected and asset.kind != "video"]
    assert client.put("/api/plan/assets", json={"asset_ids": selected}).status_code == 200
    assert builds == []
    assert client.put("/api/plan/assets", json={"asset_ids": selected[:1]}).status_code == 200
    assert builds == [business_id]
    assert upload(client).status_code == 200
    assert builds == [business_id, business_id]


def test_asset_changes_are_rate_limited(api) -> None:
    client, context = api
    from app.api.limits import assets_limit

    body = {"asset_ids": [own_asset(context)]}
    statuses = [client.put("/api/plan/assets", json=body).status_code for _ in range(assets_limit.per_user + 1)]
    assert statuses[-1] == 429 and set(statuses[:-1]) == {200}


def test_video_builder_continues_from_videos_of_previous_attempts(monkeypatch) -> None:
    # Без built каждая попытка очереди заново запускала бы ffmpeg для уже собранных роликов.
    from contextlib import nullcontext

    from app.api import plan

    calls = []

    def build_videos(repository, media_root, business_id, *, built):
        calls.append((repository.db, business_id, built))
        return False

    monkeypatch.setattr("app.db.session.SessionLocal", lambda: nullcontext("db"))
    monkeypatch.setattr(plan, "build_videos", build_videos)
    built = {"segment": ([1, 2, 3], b"video")}
    assert plan.get_video_builder()(5, built) is False
    assert calls == [("db", 5, built)] and calls[0][2] is built


@pytest.mark.parametrize("extra,by_length,by_stream", [(0, 422, 400), (1, 413, 413)])
def test_upload_limit_boundary_by_length_and_by_stream(api, monkeypatch, extra, by_length, by_stream) -> None:
    # Ровно MAX_UPLOAD доходит до разбора multipart (тело не форма — 422 и 400), на байт больше — 413.
    from app.api.plan import MAX_UPLOAD

    client, context = api
    calls = []
    monkeypatch.setattr(context.world.plans, "upload_photo", lambda *args: calls.append(args))
    headers = {"Content-Type": "multipart/form-data; boundary=b"}
    length = {"Content-Length": str(MAX_UPLOAD + extra)}
    response = client.post("/api/plan/upload-photo", content=b"--b--\r\n", headers=headers | length)
    assert response.status_code == by_length
    body = b"x" * (MAX_UPLOAD + extra)

    def chunks():
        for start in range(0, len(body), 65536):
            yield body[start : start + 65536]

    response = client.post("/api/plan/upload-photo", content=chunks(), headers=headers)
    assert response.status_code == by_stream and not calls
