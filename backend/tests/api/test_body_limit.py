import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.body import MAX_BODY, BodyLimit


class Item(BaseModel):
    text: str


def make_client() -> tuple[TestClient, list[str]]:
    calls = []
    app = FastAPI()
    app.add_middleware(BodyLimit, paths={"/upload": (4 * MAX_BODY, "Файл слишком большой.")})

    @app.post("/json")
    def json_route(item: Item) -> dict:
        calls.append(item.text)
        return {"length": len(item.text)}

    @app.post("/upload")
    async def upload_route(item: Item) -> dict:
        calls.append(item.text)
        return {"length": len(item.text)}

    return TestClient(app), calls


def body(size: int) -> bytes:
    return b'{"text": "' + b"x" * size + b'"}'


def chunks(data: bytes):
    for start in range(0, len(data), 65536):
        yield data[start : start + 65536]


def test_json_body_over_limit_is_rejected_by_length_before_handler() -> None:
    client, calls = make_client()
    response = client.post("/json", content=body(MAX_BODY), headers={"content-type": "application/json"})
    assert response.status_code == 413 and response.json() == {"detail": "Запрос слишком большой."}
    assert not calls


def test_json_body_without_length_is_counted_while_reading() -> None:
    client, calls = make_client()
    response = client.post("/json", content=chunks(body(2 * MAX_BODY)), headers={"content-type": "application/json"})
    assert response.status_code == 413 and not calls


def test_small_json_and_large_upload_pass() -> None:
    client, calls = make_client()
    assert client.post("/json", json={"text": "привет"}).json() == {"length": 6}
    json = {"content-type": "application/json"}
    assert client.post("/upload", content=body(2 * MAX_BODY), headers=json).status_code == 200
    response = client.post("/upload", content=body(5 * MAX_BODY), headers=json)
    assert response.status_code == 413 and response.json() == {"detail": "Файл слишком большой."}
    response = client.post("/upload", content=chunks(body(5 * MAX_BODY)), headers=json)
    assert response.status_code == 413 and response.json() == {"detail": "Файл слишком большой."}
    assert calls == ["привет", "x" * 2 * MAX_BODY]


def test_application_limits_every_route() -> None:
    from app.api.plan import MAX_UPLOAD, TOO_LARGE
    from app.main import app

    (limit,) = [item for item in app.user_middleware if item.cls is BodyLimit]
    assert limit.kwargs == {"paths": {"/api/plan/upload-photo": (MAX_UPLOAD, TOO_LARGE)}}
    assert "10 МБ" in TOO_LARGE


async def call(limit: BodyLimit, headers: list, body: bytes) -> tuple[int, list]:
    messages = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "path": "/json", "headers": headers}
    await limit(scope, receive, send)
    return messages[0]["status"], messages


@pytest.mark.parametrize("length", [b"abc", b"", b"1e3", b"-1", b" 5", "²".encode()])
async def test_unreadable_content_length_is_rejected(length) -> None:
    reached = []

    async def app(scope, receive, send):
        reached.append(scope)

    status, messages = await call(BodyLimit(app, paths={}), [(b"content-length", length)], b"{}")
    assert status == 413 and not reached
    assert json.loads(messages[1]["body"]) == {"detail": "Запрос слишком большой."}


def test_body_of_exactly_the_limit_passes_and_one_byte_more_does_not() -> None:
    client, calls = make_client()
    exact = body(MAX_BODY - len(body(0)))
    json_header = {"content-type": "application/json"}
    assert len(exact) == MAX_BODY
    assert client.post("/json", content=exact, headers=json_header).status_code == 200
    assert client.post("/json", content=exact + b" ", headers=json_header).status_code == 413
    assert client.post("/json", content=chunks(exact), headers=json_header).status_code == 200
    assert client.post("/json", content=chunks(exact + b" "), headers=json_header).status_code == 413


def test_large_photo_streamed_without_length_gets_photo_limit_text() -> None:
    # Ревью: 12 МБ фото без Content-Length получали 400 «Invalid multipart data» вместо понятного 413.
    from app.api.plan import MAX_UPLOAD, TOO_LARGE
    from app.main import app as application

    (limit,) = [item for item in application.user_middleware if item.cls is BodyLimit]
    calls = []
    app = FastAPI()
    app.add_middleware(BodyLimit, **limit.kwargs)

    @app.post("/api/plan/upload-photo")
    async def upload_route(request: Request) -> dict:
        calls.append(len((await request.form())["file"].file.read()))
        return {}

    boundary = "upload-boundary"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="photo.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode()
    data = head + b"\xff" * (12 * 1024 * 1024) + f"\r\n--{boundary}--\r\n".encode()
    headers = {"content-type": f"multipart/form-data; boundary={boundary}"}
    response = TestClient(app).post("/api/plan/upload-photo", content=chunks(data), headers=headers)
    assert response.status_code == 413 and response.json() == {"detail": TOO_LARGE} and not calls
    small = head + b"\xff" * (MAX_UPLOAD - len(head) - 64) + f"\r\n--{boundary}--\r\n".encode()
    assert TestClient(app).post("/api/plan/upload-photo", content=chunks(small), headers=headers).status_code == 200
