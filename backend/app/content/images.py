import asyncio
import math
import threading
import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from io import BytesIO
from weakref import WeakKeyDictionary

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

MAX_BYTES = 10 * 1024 * 1024
# 16 млн точек — снимок телефона 4032x3024 (12 млн) и камеры 16 Мп проходят. Пик памяти одного разбора в 16 млн точек
# (замер в образе api): JPEG около 130 МБ, PNG с прозрачностью около 190 МБ. WebP libwebp держит при разборе несколько
# несжатых копий — 250–315 МБ на 16 млн точек, поэтому ему предел 10 млн (212–213 МБ; кадр 4K 3840x2160 проходит).
# Одновременно идут не больше DECODE_SLOTS разборов.
MAX_PIXELS = 16_000_000
MAX_PIXELS_WEBP = 10_000_000
# Pillow пробует только эти разборщики: заголовки остальных форматов чужого файла не читаются вовсе.
DECODE_FORMATS = ("JPEG", "PNG", "WEBP")
# Самый крупный формат площадок — 1920 точек по длинной стороне (ролик 16:9): больше хранить незачем.
MAX_SIDE = 2560
DECODE_SLOTS = threading.BoundedSemaphore(2)
# Загрузка фото ждёт слот разбора не дольше UPLOAD_WAIT секунд, и ждут не больше UPLOAD_QUEUE загрузок цикла. Каждая
# держит тело до 10 МБ: шесть ждущих — 60 МБ рядом с двумя разборами по 213 МБ, и при самых медленных картинках
# (1,9 с на разбор, два слота) последняя дождётся слота секунд за шесть. Обычно разбор фото короче секунды: шести мест
# хватает и владельцу, который выбрал сразу несколько фото, и нескольким владельцам вместе.
UPLOAD_QUEUE = 6
UPLOAD_WAIT = 20
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


def _check_size(width: int, height: int) -> None:
    if min(width, height) < 256 or not 1 / 4 <= width / height <= 4:
        raise ImageRejected("Image is too small or has unsupported proportions")


class DecodeBusy(Exception):
    """Очередь загрузок к разбору полна или слот не освободился за UPLOAD_WAIT секунд."""


class _Queue:
    def __init__(self) -> None:
        self.slots = asyncio.Semaphore(2)
        self.uploads = 0


_queues: WeakKeyDictionary[asyncio.AbstractEventLoop, _Queue] = WeakKeyDictionary()


def _queue() -> _Queue:
    loop = asyncio.get_running_loop()
    return _queues.get(loop) or _queues.setdefault(loop, _Queue())


async def run_decoding[T](func: Callable[..., T], *args: object, **kwargs: object) -> T:
    """Запускает в пуле потоков работу с разбором картинки. Очередь к слотам ждут в цикле событий, а не в потоке пула:
    поток и соединение с базой ожиданием не заняты. В потоке слот DECODE_SLOTS ждут не больше двух разборов каждого
    цикла событий — пока слоты держит сборка ролика. Очередь своя у каждого цикла: у каждого и свой пул потоков, а
    перемотка демо и шаги демо-воркера идут в отдельных циклах."""
    queue = _queue()
    await queue.slots.acquire()
    return await _run(queue, func, *args, **kwargs)


async def run_upload_decoding[T](func: Callable[..., T], *args: object, **kwargs: object) -> T:
    """run_decoding для загрузки фото: очередь загрузок ограничена UPLOAD_QUEUE, ожидание — UPLOAD_WAIT секундами,
    иначе DecodeBusy. Генерация и нарезка при запуске ограничены своими лимитами и ждут без срока."""
    queue = _queue()
    if queue.uploads >= UPLOAD_QUEUE:
        raise DecodeBusy
    queue.uploads += 1
    try:
        async with asyncio.timeout(UPLOAD_WAIT):
            await queue.slots.acquire()
    except TimeoutError:
        raise DecodeBusy from None
    finally:
        queue.uploads -= 1
    return await _run(queue, func, *args, **kwargs)


async def _run[T](queue: _Queue, func: Callable[..., T], *args: object, **kwargs: object) -> T:
    """Слот отпускает конец потока, а не запроса: поток отменённого запроса доделывает разбор, и отпусти слот сам
    запрос — в потоках пула слот DECODE_SLOTS ждали бы больше двух разборов цикла."""
    task = asyncio.ensure_future(run_in_threadpool(func, *args, **kwargs))
    task.add_done_callback(lambda _: queue.slots.release())
    return await asyncio.shield(task)


def _decode(data: bytes, max_bytes: int = MAX_BYTES) -> Image.Image:
    """Размеры проверяются по заголовку до декодирования. Сразу после декодирования картинка уменьшается до MAX_SIDE,
    поворот и перевод в RGB идут уже с ней. Всё это занимает слот DECODE_SLOTS: загрузка фото, генерация, нарезка при
    запуске и сборка ролика вместе разбирают не больше двух картинок сразу. Уменьшенный результат (проверка, хэш,
    нарезка, кадры ролика) слот уже не держит."""
    if not data or len(data) > max_bytes:
        raise ImageRejected("Image is empty or exceeds the byte limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=DECODE_FORMATS) as source:
                if getattr(source, "n_frames", 1) != 1:
                    raise ImageRejected("Only single-frame JPG, PNG and WebP images are supported")
                if source.width * source.height > (MAX_PIXELS_WEBP if source.format == "WEBP" else MAX_PIXELS):
                    raise ImageRejected("Image exceeds the pixel limit")
                _check_size(source.width, source.height)
                with DECODE_SLOTS:
                    source.thumbnail((MAX_SIDE, MAX_SIDE))
                    ImageOps.exif_transpose(source, in_place=True)
                    if not source.has_transparency_data:
                        clean = source.convert("RGB")
                        clean.info.clear()
                        return clean
                    rgba = source.convert("RGBA")
                    clean = Image.new("RGB", rgba.size, "white")
                    clean.paste(rgba, mask=rgba)
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
    _check_size(*image.size)
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
