from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.body import MAX_BODY, BodyLimit


class Item(BaseModel):
    text: str


def make_client() -> tuple[TestClient, list[str]]:
    calls = []
    app = FastAPI()
    app.add_middleware(BodyLimit, paths={"/upload": 4 * MAX_BODY})

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
    assert client.post("/upload", content=body(5 * MAX_BODY), headers=json).status_code == 413
    assert calls == ["привет", "x" * 2 * MAX_BODY]


def test_application_limits_every_route() -> None:
    from app.api.plan import MAX_UPLOAD
    from app.main import app

    (limit,) = [item for item in app.user_middleware if item.cls is BodyLimit]
    assert limit.kwargs == {"paths": {"/api/plan/upload-photo": MAX_UPLOAD}}
