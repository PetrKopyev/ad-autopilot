from dataclasses import dataclass, field
from typing import Protocol


class LLMClient(Protocol):
    async def complete_json(self, system: str, user: str) -> str: ...


@dataclass
class FakeLLM:
    response: str = '{"services": [], "address": {"value": "", "quote": ""}, "advantages": []}'
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def complete_json(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.response
