import asyncio
import logging

import httpx
import uvicorn


class Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


async def test_access_log_keeps_method_path_and_status_without_query() -> None:
    # Сервер запускается так же, как в контейнере: журнал настраивает сам uvicorn, приложение загружается по имени.
    server = uvicorn.Server(uvicorn.Config("app.main:app", host="127.0.0.1", port=0, lifespan="off"))
    lines = Lines()
    access = logging.getLogger("uvicorn.access")
    access.addHandler(lines)
    serving = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{port}/api/auth/yandex/callback?code=secret-code&state=")
    finally:
        server.should_exit = True
        await serving
        access.removeHandler(lines)
    assert response.status_code == 422
    assert len(lines.lines) == 1
    assert lines.lines[0].endswith('"GET /api/auth/yandex/callback HTTP/1.1" 422')
    assert "secret-code" not in lines.lines[0]
