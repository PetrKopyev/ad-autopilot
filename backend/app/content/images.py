import math
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat, UnidentifiedImageError

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 25_000_000
DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FORMATS = {
    "direct_1_1": (1080, 1080),
    "direct_4_3": (1440, 1080),
    "direct_16_9": (1080, 607),
    "vk_icon": (256, 256),
    "vk_square": (600, 600),
    "vk_wide": (1080, 607),
}


class ImageRejected(ValueError):
    pass


@dataclass(frozen=True)
class CheckedImage:
    data: bytes
    width: int
    height: int
    phash: str
    kind: str
    label: str | None
    cost_kop: int = 0
    selected: bool = False


def _decode(data: bytes, max_bytes: int = MAX_BYTES) -> Image.Image:
    if not data or len(data) > max_bytes:
        raise ImageRejected("Image is empty or exceeds the byte limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP"} or getattr(source, "n_frames", 1) != 1:
                    raise ImageRejected("Only single-frame JPG, PNG and WebP images are supported")
                if source.width * source.height > MAX_PIXELS:
                    raise ImageRejected("Image exceeds the pixel limit")
                source.load()
                oriented = ImageOps.exif_transpose(source).convert("RGBA")
                background = Image.new("RGBA", oriented.size, "white")
                background.alpha_composite(oriented)
                clean = background.convert("RGB")
                clean.info.clear()
                return clean
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageRejected("Invalid or unsafe image") from exc


def perceptual_hash(image: Image.Image) -> str:
    sample = image.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
    pixels = list(sample.tobytes())
    cosines = [[math.cos((2 * x + 1) * frequency * math.pi / 64) for x in range(32)] for frequency in range(8)]
    rows = [[sum(pixels[y * 32 + x] * cosines[u][x] for x in range(32)) for u in range(8)] for y in range(32)]
    coefficients = [sum(rows[y][u] * cosines[v][y] for y in range(32)) for v in range(8) for u in range(8)]
    median = sorted(coefficients[1:])[31]
    value = 0
    for coefficient in coefficients:
        value = (value << 1) | (coefficient > median)
    return f"{value:016x}"


def is_duplicate(phash: str, existing: Iterable[str], *, max_distance: int = 6) -> bool:
    if not 0 <= max_distance <= 64:
        raise ValueError("Invalid hash distance")
    return any((int(phash, 16) ^ int(other, 16)).bit_count() <= max_distance for other in existing)


def _validate(image: Image.Image) -> None:
    width, height = image.size
    if min(width, height) < 256 or not 1 / 4 <= width / height <= 4:
        raise ImageRejected("Image is too small or has unsupported proportions")
    sample = image.resize((128, 128), Image.Resampling.LANCZOS)
    if max(ImageStat.Stat(sample).stddev) < 3:
        raise ImageRejected("Image is blank or nearly uniform")


def _encode(image: Image.Image) -> bytes:
    for quality in (92, 85, 75, 60):
        output = BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True, exif=b"")
        if output.tell() <= MAX_BYTES:
            return output.getvalue()
    raise ImageRejected("Encoded image exceeds the byte limit")


def check_image(
    data: bytes,
    *,
    existing_hashes: Iterable[str] = (),
    uploaded: bool = False,
    cost_kop: int = 0,
    max_bytes: int = MAX_BYTES,
) -> CheckedImage:
    if type(cost_kop) is not int or cost_kop < 0:
        raise ValueError("Cost must use non-negative integer kopecks")
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES:
        raise ValueError("Invalid upload byte limit")
    image = _decode(data, max_bytes)
    _validate(image)
    phash = perceptual_hash(image)
    if is_duplicate(phash, existing_hashes):
        raise ImageRejected("Duplicate image")
    return CheckedImage(
        _encode(image), image.width, image.height, phash,
        "upload" if uploaded else "image", None if uploaded else "иллюстрация", cost_kop,
    )


def accept_upload(data: bytes, *, existing_hashes: Iterable[str] = (), max_bytes: int = MAX_BYTES) -> CheckedImage:
    return check_image(data, uploaded=True, existing_hashes=existing_hashes, max_bytes=max_bytes)


def _wrap(text: str, draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines = []
    for paragraph in text.splitlines():
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if draw.textbbox((0, 0), candidate, font=font)[2] <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            line = ""
            for character in word:
                if draw.textbbox((0, 0), line + character, font=font)[2] > width:
                    if not line:
                        raise ImageRejected("Offer character does not fit")
                    lines.append(line)
                    line = ""
                line += character
        lines.append(line)
    return lines


def overlay_offer(image: Image.Image, text: str, *, font_path: str = DEFAULT_FONT) -> Image.Image:
    if not text.strip() or len(text) > 1000 or any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ImageRejected("Invalid offer text")
    result = image.convert("RGB").copy()
    draw = ImageDraw.Draw(result)
    margin = max(8, image.width // 25)
    padding = max(6, image.width // 50)
    available_width = image.width - 2 * (margin + padding)
    for size in range(max(12, min(image.size) // 14), 11, -1):
        font = ImageFont.truetype(font_path, size=size)
        lines = _wrap(text, draw, font, available_width)
        wrapped = "\n".join(lines)
        spacing = max(2, size // 5)
        bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=spacing)
        text_height = bbox[3] - bbox[1]
        if text_height + 2 * padding <= image.height // 2 and bbox[2] - bbox[0] <= available_width:
            top = image.height - margin - text_height - 2 * padding
            draw.rectangle((margin, top, image.width - margin, image.height - margin), fill=(20, 20, 20))
            draw.multiline_text(
                (margin + padding - bbox[0], top + padding - bbox[1]),
                wrapped, font=font, fill="white", spacing=spacing,
            )
            return result
    raise ImageRejected("Offer does not fit; shorten the text")


def make_formats(
    data: bytes, *, offer: str | None = None, font_path: str = DEFAULT_FONT,
) -> dict[str, bytes]:
    image = _decode(data)
    _validate(image)
    formats = {}
    for name, size in FORMATS.items():
        cropped = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
        if offer is not None:
            cropped = overlay_offer(cropped, offer, font_path=font_path)
        formats[name] = _encode(cropped)
    return formats
