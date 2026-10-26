import asyncio
import base64
import json
from dataclasses import replace

import httpx
import pytest
import respx

from app.content.art import (
    ART_COST_KOP,
    GENERATE_URL,
    OPERATIONS_URL,
    ArtBudget,
    ArtBudgetExceeded,
    ArtCall,
    ArtError,
    FakeArt,
    YandexArt,
)


@pytest.fixture
def records() -> list[ArtCall]:
    return []


def art(client: httpx.AsyncClient, records: list[ArtCall], limit: int = 6000, **kwargs: object) -> YandexArt:
    async def record(call: ArtCall) -> None:
        records.append(replace(call))

    return YandexArt(
        client, api_key="test-key", folder_id="test-folder", budget=ArtBudget(limit),
        record_call=record, poll_interval=0, max_polls=2, **kwargs,
    )


def completed(image: bytes = b"test image") -> dict:
    return {"id": "op-1", "done": True, "response": {"image": base64.b64encode(image).decode()}}


@respx.mock
async def test_async_generation_and_accounting(records: list[ArtCall]) -> None:
    post = respx.post(GENERATE_URL).respond(200, json={"id": "op-1", "done": False})
    poll = respx.get(f"{OPERATIONS_URL}/op-1").mock(side_effect=[
        httpx.Response(200, json={"id": "op-1", "done": False}),
        httpx.Response(200, json=completed()),
    ])
    async with httpx.AsyncClient() as client:
        generator = art(client, records)
        result = await generator.generate("Иллюстрация", seed=42, business_id=7)
    assert result.image == b"test image"
    assert result.seed == 42
    assert result.label == "иллюстрация"
    assert result.selected is False
    assert result.cost_kop == generator.budget.spent_kop == ART_COST_KOP
    assert poll.call_count == 2
    payload = json.loads(post.calls[0].request.content)
    assert payload["modelUri"] == "art://test-folder/yandex-art/latest"
    assert payload["generationOptions"] == {"mimeType": "image/jpeg", "seed": "42"}
    assert post.calls[0].request.headers["Authorization"] == "Api-Key test-key"
    assert records[0].status == "pending"
    assert records[-1].status == "succeeded"
    assert records[-1].business_id == 7
    assert records[-1].operation_id == "op-1"
    assert records[-1].seed == 42


@respx.mock
async def test_budget_blocks_before_http(records: list[ArtCall]) -> None:
    async with httpx.AsyncClient() as client:
        generator = art(client, records, limit=222)
        with pytest.raises(ArtBudgetExceeded):
            await generator.generate("Idea", seed=1, business_id=1)
    assert not respx.calls
    assert not records


@pytest.mark.parametrize("limit", [True, 223.0, -1])
def test_budget_requires_integer_kopecks(limit: object) -> None:
    with pytest.raises(ValueError):
        ArtBudget(limit)


@respx.mock
async def test_concurrent_generations_share_budget(records: list[ArtCall]) -> None:
    post = respx.post(GENERATE_URL).respond(200, json=completed())
    async with httpx.AsyncClient() as client:
        generator = art(client, records, limit=223)
        results = await asyncio.gather(
            *(generator.generate("Idea", seed=seed, business_id=1) for seed in range(8)),
            return_exceptions=True,
        )
    assert sum(isinstance(result, ArtBudgetExceeded) for result in results) == 7
    assert post.call_count == 1
    assert generator.budget.spent_kop == 223


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
@respx.mock
async def test_server_responses_are_never_retried(status: int, records: list[ArtCall]) -> None:
    post = respx.post(GENERATE_URL).respond(status)
    async with httpx.AsyncClient() as client:
        generator = art(client, records)
        with pytest.raises(ArtError):
            await generator.generate("Idea", seed=1, business_id=1)
    assert post.call_count == 1
    assert generator.budget.spent_kop == 223
    assert records[-1].status == "unknown"


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ReadError, httpx.WriteError, httpx.WriteTimeout])
@respx.mock
async def test_ambiguous_network_failure_is_not_retried(error: type, records: list[ArtCall]) -> None:
    post = respx.post(GENERATE_URL).mock(side_effect=error("ambiguous"))
    async with httpx.AsyncClient() as client:
        generator = art(client, records)
        with pytest.raises(ArtError):
            await generator.generate("Idea", seed=1, business_id=1)
    assert post.call_count == 1
    assert generator.budget.spent_kop == 223


@respx.mock
async def test_connect_failure_can_be_retried(records: list[ArtCall]) -> None:
    post = respx.post(GENERATE_URL).mock(side_effect=[
        httpx.ConnectError("not sent"), httpx.Response(200, json=completed()),
    ])
    async with httpx.AsyncClient() as client:
        generator = art(client, records)
        await generator.generate("Idea", seed=1, business_id=1)
    assert post.call_count == 2
    assert generator.budget.spent_kop == 223


@respx.mock
async def test_connect_retries_are_bounded(records: list[ArtCall]) -> None:
    post = respx.post(GENERATE_URL).mock(side_effect=httpx.ConnectTimeout("not sent"))
    async with httpx.AsyncClient() as client:
        generator = art(client, records)
        with pytest.raises(ArtError):
            await generator.generate("Idea", seed=1, business_id=1)
    assert post.call_count == 2


@respx.mock
async def test_rejection_recorded_without_replaying(records: list[ArtCall]) -> None:
    post = respx.post(GENERATE_URL).respond(200, json={"id": "op-1"})
    respx.get(f"{OPERATIONS_URL}/op-1").respond(200, json={
        "done": True, "error": {"code": 3, "message": "content rejected"},
    })
    async with httpx.AsyncClient() as client:
        with pytest.raises(ArtError):
            await art(client, records).generate("Idea", seed=1, business_id=7)
    assert post.call_count == 1
    assert records[-1].status == "rejected"
    assert records[-1].rejection_reason == "operation_error:3"
    assert records[-1].cost_kop == 223


@respx.mock
async def test_polling_limit_keeps_reservation(records: list[ArtCall]) -> None:
    post = respx.post(GENERATE_URL).respond(200, json={"id": "op-1"})
    poll = respx.get(f"{OPERATIONS_URL}/op-1").respond(200, json={"done": False})
    async with httpx.AsyncClient() as client:
        generator = art(client, records)
        with pytest.raises(ArtError, match="polling limit"):
            await generator.generate("Idea", seed=1, business_id=7)
    assert post.call_count == 1
    assert poll.call_count == 2
    assert generator.budget.spent_kop == 223
    assert records[-1].status == "unknown"


@pytest.mark.parametrize("operation", [[], {}, {"id": "op-1", "done": True, "response": {"image": "?!"}}])
@respx.mock
async def test_malformed_response_is_not_replayed(operation: object, records: list[ArtCall]) -> None:
    post = respx.post(GENERATE_URL).respond(200, json=operation)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ArtError):
            await art(client, records).generate("Idea", seed=1, business_id=7)
    assert post.call_count == 1
    assert records[-1].status == "unknown"


@respx.mock
async def test_failed_reservation_storage_prevents_request() -> None:
    async def record(call: ArtCall) -> None:
        raise RuntimeError("storage unavailable")

    async with httpx.AsyncClient() as client:
        generator = YandexArt(
            client, api_key="test", folder_id="test", budget=ArtBudget(223), record_call=record,
        )
        with pytest.raises(RuntimeError):
            await generator.generate("Idea", seed=1, business_id=7)
    assert not respx.calls


async def test_fake_budget_and_rejections() -> None:
    fake = FakeArt(b"image", ArtBudget(446))
    assert (await fake.generate("Idea", seed=1, business_id=1)).image == b"image"
    fake.rejection_reason = "moderation"
    with pytest.raises(ArtError):
        await fake.generate("Idea", seed=2, business_id=1)
    assert fake.calls[-1].rejection_reason == "moderation"
    with pytest.raises(ArtBudgetExceeded):
        await fake.generate("Idea", seed=3, business_id=1)
    assert fake.budget.spent_kop == 446


@pytest.mark.parametrize("prompt,seed", [("", 1), ("Idea", -1), ("Idea", 2**63), ("Idea", True)])
async def test_invalid_input_does_not_reserve(prompt: str, seed: int) -> None:
    fake = FakeArt(b"image", ArtBudget(223))
    with pytest.raises(ValueError):
        await fake.generate(prompt, seed=seed, business_id=1)
    assert fake.budget.spent_kop == 0
    assert not fake.calls


@respx.mock
async def test_cancellation_keeps_reservation_and_operation(records: list[ArtCall]) -> None:
    async def cancel(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    post = respx.post(GENERATE_URL).respond(200, json={"id": "op-1"})
    respx.get(f"{OPERATIONS_URL}/op-1").mock(side_effect=cancel)
    async with httpx.AsyncClient() as client:
        generator = art(client, records)
        with pytest.raises(asyncio.CancelledError):
            await generator.generate("Idea", seed=1, business_id=7)
    assert post.call_count == 1
    assert generator.budget.spent_kop == 223
    assert records[-1].operation_id == "op-1"
    assert records[-1].status == "unknown"
    assert records[-1].rejection_reason == "cancelled"
