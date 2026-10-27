import json

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY = 1024 * 1024
TOO_LARGE = "Запрос слишком большой."


class BodyLimit:
    """Предел тела запроса до проверки входа: иначе FastAPI читает JSON целиком, и гигабайт без входа съедает память.

    Длинное тело отклоняется сразу по Content-Length, а без него — при чтении потока, как только счёт превысит предел.
    Обычным ручкам хватает MAX_BODY, загрузке фото предел и текст отказа заданы отдельно по пути."""

    def __init__(self, app: ASGIApp, paths: dict[str, tuple[int, str]], default: int = MAX_BODY) -> None:
        self.app = app
        self.paths = paths
        self.default = default

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit, text = self.paths.get(scope["path"], (self.default, TOO_LARGE))
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > limit):
            await too_large(send, text)
            return
        received = 0

        async def limited() -> Message:
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(413, text)
            return message

        await self.app(scope, limited, send)


async def too_large(send: Send, text: str) -> None:
    body = json.dumps({"detail": text}, ensure_ascii=False).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": 413, "headers": headers})
    await send({"type": "http.response.body", "body": body})
