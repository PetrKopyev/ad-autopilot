import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from app.content.llm import LLMClient
from app.content.yandexgpt import (
    COMPLETION_URL,
    CompletionResult,
    TokenTariff,
    YandexGPTClient,
    YandexGPTError,
)


def completion(text: str = '{"title": "Маникюр", "text": "Запись онлайн"}', **changes: object) -> dict:
    result = {
        "alternatives": [{"message": {"role": "assistant", "text": text}, "status": "ALTERNATIVE_STATUS_FINAL"}],
        "usage": {"inputTextTokens": "100", "completionTokens": "50", "totalTokens": "150"},
        "modelVersion": "test-version",
    }
    result.update(changes)
    return {"result": result}


def yandex(client: httpx.AsyncClient, **kwargs: object) -> YandexGPTClient:
    return YandexGPTClient(
        client,
        api_key="test-key",
        folder_id="test-folder",
        model="yandexgpt-lite/latest",
        tariff=TokenTariff(20, 40),
        **kwargs,
    )


@respx.mock
async def test_request_response_and_protocol_compatibility() -> None:
    post = respx.post(COMPLETION_URL).respond(200, json=completion())
    async with httpx.AsyncClient() as http:
        client: LLMClient = yandex(http, timeout=12, max_tokens=500)
        result = await client.complete_json("Системное сообщение", "Данные бизнеса")
    assert isinstance(result, str)
    assert isinstance(result, CompletionResult)
    assert json.loads(result)["title"] == "Маникюр"
    assert result.tokens_in == 100
    assert result.tokens_out == 50
    assert result.cost_kop == 4
    assert type(result.cost_kop) is int
    assert result.model == "gpt://test-folder/yandexgpt-lite/latest"
    assert result.model_version == "test-version"
    request = post.calls[0].request
    assert request.headers["Authorization"] == "Api-Key test-key"
    payload = json.loads(request.content)
    assert payload["modelUri"] == result.model
    assert payload["jsonObject"] is True
    assert payload["completionOptions"] == {"stream": False, "temperature": 0.2, "maxTokens": "500"}
    assert payload["messages"][0]["role"] == "system"
    assert "Системное сообщение" in payload["messages"][0]["text"]
    assert payload["messages"][1] == {"role": "user", "text": "Данные бизнеса"}
    assert request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 12)


@pytest.mark.parametrize("tokens_in,tokens_out,expected", [(0, 0, 0), (1, 0, 1), (0, 1, 1), (500, 500, 1)])
def test_cost_rounds_total_up_without_floats(tokens_in: int, tokens_out: int, expected: int) -> None:
    assert TokenTariff(1, 1).cost(tokens_in, tokens_out) == expected
    assert TokenTariff(123, 456).cost(10**15, 10**15) == 579 * 10**12


@pytest.mark.parametrize("rate", [-1, True, 1.5])
def test_tariff_rejects_non_integer_money(rate: object) -> None:
    with pytest.raises(ValueError):
        TokenTariff(rate, 10)
    with pytest.raises(ValueError):
        TokenTariff(10, rate)


@pytest.mark.parametrize("tokens", [-1, True, 1.5])
def test_cost_rejects_invalid_token_counts(tokens: object) -> None:
    with pytest.raises(ValueError):
        TokenTariff(20, 40).cost(tokens, 10)


@respx.mock
async def test_429_and_server_errors_are_retried_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr("app.content.yandexgpt.asyncio.sleep", sleep)
    post = respx.post(COMPLETION_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(500),
            httpx.Response(503),
            httpx.Response(200, json=completion()),
        ]
    )
    async with httpx.AsyncClient() as http:
        result = await yandex(http).complete_json("system", "user")
    assert post.call_count == 4
    assert [call.args[0] for call in sleep.await_args_list] == [2, 1, 2]
    assert result.cost_kop == 4


@pytest.mark.parametrize("status", [429, 500, 599])
@respx.mock
async def test_retries_are_bounded(status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr("app.content.yandexgpt.asyncio.sleep", sleep)
    post = respx.post(COMPLETION_URL).respond(status)
    async with httpx.AsyncClient() as http:
        with pytest.raises(YandexGPTError) as error:
            await yandex(http).complete_json("system", "user")
    assert post.call_count == 4
    assert sleep.await_count == 3
    assert error.value.status_code == status


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404])
@respx.mock
async def test_other_statuses_are_not_retried(status: int) -> None:
    post = respx.post(COMPLETION_URL).respond(status, headers={"Location": "https://other.example"})
    async with httpx.AsyncClient(follow_redirects=True) as http:
        with pytest.raises(YandexGPTError) as error:
            await yandex(http).complete_json("system", "user")
    assert post.call_count == 1
    assert error.value.status_code == status


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError])
@respx.mock
async def test_transport_failures_are_not_replayed(error: type[httpx.HTTPError]) -> None:
    post = respx.post(COMPLETION_URL).mock(side_effect=error("transport failed"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(YandexGPTError, match="transport failure"):
            await yandex(http).complete_json("system", "user")
    assert post.call_count == 1


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"result": []}, completion(alternatives=[]), completion(usage={}), completion(alternatives=[None])],
)
@respx.mock
async def test_malformed_response(payload: object) -> None:
    post = respx.post(COMPLETION_URL).respond(200, json=payload)
    async with httpx.AsyncClient() as http:
        with pytest.raises(YandexGPTError, match="Malformed"):
            await yandex(http).complete_json("system", "user")
    assert post.call_count == 1


@pytest.mark.parametrize("token", [True, -1, 1.5, "1.5", "-1", None, "999999999999999999999999"])
@respx.mock
async def test_invalid_usage_is_not_silently_free(token: object) -> None:
    respx.post(COMPLETION_URL).respond(
        200,
        json=completion(
            usage={
                "inputTextTokens": token,
                "completionTokens": "50",
                "totalTokens": "150",
            }
        ),
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(YandexGPTError, match="Malformed"):
            await yandex(http).complete_json("system", "user")


@pytest.mark.parametrize("status", ["ALTERNATIVE_STATUS_TRUNCATED_FINAL", "ALTERNATIVE_STATUS_CONTENT_FILTER"])
@respx.mock
async def test_unusable_completion_preserves_paid_usage(status: str) -> None:
    post = respx.post(COMPLETION_URL).respond(
        200,
        json=completion(
            alternatives=[
                {
                    "message": {"role": "assistant", "text": ""},
                    "status": status,
                }
            ]
        ),
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(YandexGPTError) as error:
            await yandex(http).complete_json("system", "user")
    assert post.call_count == 1
    assert error.value.result is not None
    assert error.value.result.cost_kop == 4
    assert error.value.result.status == status


@respx.mock
async def test_concurrent_results_have_independent_usage() -> None:
    respx.post(COMPLETION_URL).mock(
        side_effect=[
            httpx.Response(200, json=completion()),
            httpx.Response(
                200,
                json=completion(
                    usage={
                        "inputTextTokens": "200",
                        "completionTokens": "100",
                        "totalTokens": "300",
                    }
                ),
            ),
        ]
    )
    async with httpx.AsyncClient() as http:
        client = yandex(http)
        results = await asyncio.gather(client.complete_json("s", "u"), client.complete_json("s", "u"))
    assert sorted(result.cost_kop for result in results) == [4, 8]
    assert sorted(result.tokens_in for result in results) == [100, 200]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_retries": 4},
        {"max_retries": True},
        {"max_tokens": 0},
        {"timeout": 0},
        {"timeout": float("inf")},
        {"retry_delay": 0},
    ],
)
async def test_configuration_limits(kwargs: dict) -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError):
            yandex(http, **kwargs)
