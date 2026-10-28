from dataclasses import dataclass, field
from typing import Protocol


class LLMClient(Protocol):
    async def complete_json(self, system: str, user: str, temperature: float = 0.2) -> str: ...


@dataclass
class FakeLLM:
    response: str = '{"services": [], "address": {"value": "", "quote": ""}, "advantages": []}'
    calls: list[tuple[str, str]] = field(default_factory=list)
    temperatures: list[float] = field(default_factory=list)

    async def complete_json(self, system: str, user: str, temperature: float = 0.2) -> str:
        self.calls.append((system, user))
        self.temperatures.append(temperature)
        return self.response
