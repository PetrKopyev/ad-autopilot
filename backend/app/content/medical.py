import re
from dataclasses import dataclass
from typing import Literal

MedicalLevel = Literal["none", "maybe", "required"]
REQUIRED = re.compile(
    r"\b(?:косметолог\w*|ботокс\w*|ботул\w*|филлер\w*|мезотерап\w*|биоревитал\w*|"
    r"пилинг\w*|инъекц\w*|стоматолог\w*|медицинск\w*\s+массаж\w*|лечебн\w*\s+массаж\w*|"
    r"массаж\w*\s+(?:лечебн|медицинск)\w*)\b",
    re.IGNORECASE,
)
MAYBE = re.compile(r"\b(?:лазерн\w*|эпиляц\w*|татуаж\w*|перманентн\w*\s+макияж\w*)\b", re.IGNORECASE)


@dataclass
class MedicalResult:
    level: MedicalLevel
    words: list[str]


def classify(text: str) -> MedicalResult:
    required = list(REQUIRED.finditer(text))
    possible = list(MAYBE.finditer(text))
    matches = sorted(required + possible, key=lambda match: match.start())
    words = list(dict.fromkeys(match.group().lower() for match in matches))
    return MedicalResult("required" if required else "maybe" if possible else "none", words)
