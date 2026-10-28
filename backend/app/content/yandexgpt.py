import asyncio
import math
from dataclasses import dataclass

import httpx

COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"


@dataclass(frozen=True)
class TokenTariff:
    input_kop_per_1000: int
    output_kop_per_1000: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (
                self.input_kop_per_1000,
                self.output_kop_per_1000,
            )
        ):
            raise ValueError("Tariff must use non-negative integer kopecks")

    def cost(self, tokens_in: int, tokens_out: int) -> int:
        tokens_in = _token_count(tokens_in)
        tokens_out = _token_count(tokens_out)
        numerator = tokens_in * self.input_kop_per_1000 + tokens_out * self.output_kop_per_1000
        return (numerator + 999) // 1000


class CompletionResult(str):
    """A Protocol-compatible string carrying accounting for this individual call."""

    model: str
    model_version: str
    tokens_in: int
    tokens_out: int
    cost_kop: int
    status: str

    def __new__(
        cls,
        text: str,
        *,
        model: str,
        model_version: str,
        tokens_in: int,
        tokens_out: int,
        cost_kop: int,
        status: str,
    ) -> "CompletionResult":
        result = super().__new__(cls, text)
        result.model = model
        result.model_version = model_version
        result.tokens_in = tokens_in
        result.tokens_out = tokens_out
        result.cost_kop = cost_kop
        result.status = status
        return result


class YandexGPTError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        result: CompletionResult | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.result = result


def _token_count(value: object) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("Invalid token count")
    return value


class YandexGPTClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        folder_id: str,
        model: str,
        tariff: TokenTariff,
        timeout: float = 30,
        max_tokens: int = 1000,
        max_retries: int = 3,
        retry_delay: float = 0.5,
    ) -> None:
        if not api_key or not folder_id or not model:
            raise ValueError("YC_API_KEY, YC_FOLDER_ID and YANDEXGPT_MODEL are required")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be positive and finite")
        if type(max_tokens) is not int or max_tokens < 1:
            raise ValueError("Max tokens must be a positive integer")
        if type(max_retries) is not int or not 0 <= max_retries <= 3:
            raise ValueError("At most three retries are allowed")
        if not math.isfinite(retry_delay) or retry_delay <= 0:
            raise ValueError("Retry delay must be positive and finite")
        self.client = client
        self._headers = {"Authorization": f"Api-Key {api_key}"}
        self.model_uri = f"gpt://{folder_id}/{model}"
        self.tariff = tariff
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    async def complete_json(self, system: str, user: str, temperature: float = 0.2) -> CompletionResult:
        if not system.strip() or not user.strip():
            raise ValueError("System and user messages must not be empty")
        payload = {
            "modelUri": self.model_uri,
            "completionOptions": {"stream": False, "temperature": temperature, "maxTokens": str(self.max_tokens)},
            "messages": [
                {"role": "system", "text": f"{system}\nВерни только JSON-объект."},
                {"role": "user", "text": user},
            ],
            "jsonObject": True,
        }
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.client.post(
                    COMPLETION_URL,
                    headers=self._headers,
                    json=payload,
                    timeout=self.timeout,
                    follow_redirects=False,
                )
            except httpx.HTTPError:
                raise YandexGPTError("YandexGPT transport failure") from None
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < self.max_retries:
                    delay = min(self.retry_delay * 2**attempt, 30)
                    retry_after = response.headers.get("Retry-After", "")
                    if retry_after.isascii() and retry_after.isdecimal():
                        delay = min(max(delay, int(retry_after)), 30)
                    await asyncio.sleep(delay)
                    continue
            if not response.is_success:
                raise YandexGPTError("YandexGPT HTTP failure", status_code=response.status_code)
            return self._parse(response)
        raise AssertionError("Unreachable retry state")

    def _parse(self, response: httpx.Response) -> CompletionResult:
        try:
            data = response.json()["result"]
            usage = data["usage"]
            tokens_in = _token_count(usage["inputTextTokens"])
            tokens_out = _token_count(usage["completionTokens"])
            if _token_count(usage["totalTokens"]) != tokens_in + tokens_out:
                raise ValueError("Inconsistent token usage")
            alternative = data["alternatives"][0]
            text = alternative["message"].get("text", "")
            status = alternative["status"]
            version = data.get("modelVersion", "")
            if not all(isinstance(value, str) for value in (text, status, version)):
                raise ValueError("Invalid completion")
            result = CompletionResult(
                text,
                model=self.model_uri,
                model_version=version,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_kop=self.tariff.cost(tokens_in, tokens_out),
                status=status,
            )
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise YandexGPTError("Malformed YandexGPT response") from None
        if status != "ALTERNATIVE_STATUS_FINAL" or not text.strip():
            raise YandexGPTError("YandexGPT did not return a complete answer", result=result)
        return result
