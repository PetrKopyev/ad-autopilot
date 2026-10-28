import asyncio
import base64
import json
from dataclasses import replace

import httpx
import pytest
import respx

from app.content.art import (
    ART_COST_KOP,
    ART_SIZE,
    DEFAULT_ART_MODEL,
    IMAGES_URL,
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
        record_call=record, retry_delay=0, **kwargs,
    )


def generated(image: bytes = b"test image") -> dict:
    return {"created": 1, "data": [{"b64_json": base64.b64encode(image).decode()}], "output_format": "jpeg"}


def transport(*responses: httpx.Response | BaseException) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        answer = responses[min(len(requests), len(responses)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), requests


async def test_generation_saves_image_and_spends_budget(records: list[ArtCall]) -> None:
    client, requests = transport(httpx.Response(200, json=generated()))
    async with client:
        generator = art(client, records)
        result = await generator.generate("Иллюстрация", seed=42, business_id=7)
    assert result.image == b"test image"
    assert result.seed == 42 and result.operation_id is None
    assert result.label == "иллюстрация"
    assert result.selected is False
    assert result.cost_kop == generator.budget.spent_kop == ART_COST_KOP
    assert len(requests) == 1 and str(requests[0].url) == IMAGES_URL
    assert json.loads(requests[0].content) == {
        "model": f"art://test-folder/{DEFAULT_ART_MODEL}", "prompt": "Иллюстрация", "size": ART_SIZE,
    }
    assert requests[0].headers["Authorization"] == "Bearer test-key"
    assert requests[0].headers["OpenAI-Project"] == "test-folder"
    assert records[0].status == "pending"
    assert records[-1].status == "succeeded"
    assert records[-1].business_id == 7
    assert records[-1].operation_id is None
    assert records[-1].seed == 42
    assert records[-1].cost_kop == ART_COST_KOP


async def test_model_from_settings_goes_into_request(records: list[ArtCall]) -> None:
    from app.services.registry import build_art_factory

    client, requests = transport(httpx.Response(200, json=generated()))
    config = type("Config", (), {
        "platform_mode": "live", "yc_folder_id": "folder", "yandexart_model": "aliceai-image-art-9.9/latest",
        "yc_api_key": type("Secret", (), {"get_secret_value": lambda self: "key"})(),
    })()

    async def record(call: ArtCall) -> None:
        pass

    async with client:
        await build_art_factory(config, client)(ArtBudget(6000), record).generate("Idea", seed=1, business_id=1)
    assert json.loads(requests[0].content)["model"] == "art://folder/aliceai-image-art-9.9/latest"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_client_errors_are_not_retried_and_not_paid(status: int, records: list[ArtCall]) -> None:
    client, requests = transport(httpx.Response(status, json={"error": {"message": "Access to model denied"}}))
    async with client:
        generator = art(client, records)
        with pytest.raises(ArtError):
            await generator.generate("Idea", seed=1, business_id=1)
    assert len(requests) == 1
    assert generator.budget.spent_kop == 0
    assert records[-1].status == "rejected"
    assert records[-1].rejection_reason == f"http_{status}"
    assert records[-1].cost_kop == 0


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_busy_and_server_errors_are_retried(status: int, records: list[ArtCall]) -> None:
    client, requests = transport(httpx.Response(status), httpx.Response(200, json=generated()))
    async with client:
        generator = art(client, records)
        result = await generator.generate("Idea", seed=1, business_id=1)
    assert result.image == b"test image" and len(requests) == 2
    assert generator.budget.spent_kop == ART_COST_KOP


@pytest.mark.parametrize("status", [429, 500])
async def test_retries_are_bounded_and_failure_is_not_paid(status: int, records: list[ArtCall]) -> None:
    client, requests = transport(httpx.Response(status))
    async with client:
        generator = art(client, records, max_retries=2)
        with pytest.raises(ArtError):
            await generator.generate("Idea", seed=1, business_id=1)
    assert len(requests) == 3
    assert generator.budget.spent_kop == 0 and records[-1].cost_kop == 0


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ReadError, httpx.WriteError, httpx.WriteTimeout])
async def test_ambiguous_network_failure_is_not_retried(error: type, records: list[ArtCall]) -> None:
    client, requests = transport(error("ambiguous"))
    async with client:
        generator = art(client, records)
        with pytest.raises(ArtError):
            await generator.generate("Idea", seed=1, business_id=1)
    assert len(requests) == 1
    assert generator.budget.spent_kop == ART_COST_KOP
    assert records[-1].status == "unknown" and records[-1].cost_kop == ART_COST_KOP


async def test_connect_failure_can_be_retried(records: list[ArtCall]) -> None:
    client, requests = transport(httpx.ConnectError("not sent"), httpx.Response(200, json=generated()))
    async with client:
        generator = art(client, records)
        await generator.generate("Idea", seed=1, business_id=1)
    assert len(requests) == 2
    assert generator.budget.spent_kop == ART_COST_KOP


async def test_connect_retries_are_bounded_and_not_paid(records: list[ArtCall]) -> None:
    client, requests = transport(httpx.ConnectTimeout("not sent"))
    async with client:
        generator = art(client, records, max_retries=1)
        with pytest.raises(ArtError):
            await generator.generate("Idea", seed=1, business_id=1)
    assert len(requests) == 2
    assert generator.budget.spent_kop == 0 and records[-1].rejection_reason == "connect_error"


@pytest.mark.parametrize("body", [[], {}, {"data": []}, {"data": [{"b64_json": "?!"}]}, {"data": [{"b64_json": ""}]}])
async def test_malformed_response_is_not_replayed(body: object, records: list[ArtCall]) -> None:
    client, requests = transport(httpx.Response(200, json=body))
    async with client:
        generator = art(client, records)
        with pytest.raises(ArtError):
            await generator.generate("Idea", seed=1, business_id=7)
    assert len(requests) == 1
    assert records[-1].status == "unknown"
    assert generator.budget.spent_kop == ART_COST_KOP


async def test_concurrent_generations_share_budget(records: list[ArtCall]) -> None:
    client, requests = transport(httpx.Response(200, json=generated()))
    async with client:
        generator = art(client, records, limit=223)
        results = await asyncio.gather(
            *(generator.generate("Idea", seed=seed, business_id=1) for seed in range(8)),
            return_exceptions=True,
        )
    assert sum(isinstance(result, ArtBudgetExceeded) for result in results) == 7
    assert len(requests) == 1
    assert generator.budget.spent_kop == 223


async def test_cancellation_keeps_reservation(records: list[ArtCall]) -> None:
    client, _ = transport(asyncio.CancelledError())
    async with client:
        generator = art(client, records)
        with pytest.raises(asyncio.CancelledError):
            await generator.generate("Idea", seed=1, business_id=7)
    assert generator.budget.spent_kop == 223
    assert records[-1].status == "unknown"
    assert records[-1].rejection_reason == "cancelled"


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
