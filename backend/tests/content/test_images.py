import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont
from starlette.concurrency import run_in_threadpool

from app.content import images
from app.content.images import (
    DEFAULT_FONT,
    FORMATS,
    MAX_BYTES,
    MAX_SIDE,
    ImageRejected,
    accept_upload,
    check_image,
    is_duplicate,
    make_formats,
    overlay_offer,
    perceptual_hash,
)


@pytest.fixture
def photo() -> Image.Image:
    image = Image.new("RGB", (1400, 1100), "#f0d0a0")
    draw = ImageDraw.Draw(image)
    draw.ellipse((100, 150, 650, 700), fill="#486c84")
    draw.rectangle((800, 200, 1200, 950), fill="#906050")
    draw.line((0, 1000, 1300, 0), fill="white", width=30)
    return image


def encoded(image: Image.Image, fmt: str = "PNG", **kwargs: object) -> bytes:
    stream = BytesIO()
    image.save(stream, format=fmt, **kwargs)
    return stream.getvalue()


def test_generated_image_metadata(photo: Image.Image) -> None:
    result = check_image(encoded(photo), cost_kop=223)
    assert (result.width, result.height) == photo.size
    assert len(result.phash) == 16
    assert result.cost_kop == 223
    assert result.kind == "image"
    assert result.label == "иллюстрация"
    assert result.selected is False


@pytest.mark.parametrize("size", [(200, 200), (5000, 256), (256, 5000)])
def test_reject_small_and_extreme_proportions(size: tuple[int, int]) -> None:
    with pytest.raises(ImageRejected, match="small|proportions"):
        check_image(encoded(Image.new("RGB", size)))


@pytest.mark.parametrize("color", ["white", "black", "#ccaa88"])
def test_uniform_rejected(color: str) -> None:
    with pytest.raises(ImageRejected, match="uniform"):
        check_image(encoded(Image.new("RGB", (1080, 1080), color)))


def test_nearly_uniform_rejected() -> None:
    image = Image.new("RGB", (1080, 1080), "#808080")
    ImageDraw.Draw(image).rectangle((0, 0, 1000, 1000), fill="#818181")
    with pytest.raises(ImageRejected, match="uniform"):
        check_image(encoded(image))


@pytest.mark.parametrize("data", [b"", b"not an image", b"x" * (MAX_BYTES + 1)])
def test_invalid_or_oversized_upload(data: bytes) -> None:
    with pytest.raises(ImageRejected):
        accept_upload(data)


def test_custom_upload_limit(photo: Image.Image) -> None:
    data = encoded(photo)
    with pytest.raises(ImageRejected, match="byte limit"):
        accept_upload(data, max_bytes=len(data) - 1)


def test_pixel_bomb_rejected() -> None:
    data = encoded(Image.new("1", (5100, 5000)))
    with pytest.raises(ImageRejected, match="pixel limit"):
        accept_upload(data)


@pytest.mark.parametrize("size", [(4100, 4000), (5000, 256)])
def test_size_rejected_before_decoding(size: tuple[int, int], monkeypatch: pytest.MonkeyPatch) -> None:
    data = encoded(Image.new("RGB", size))

    def decode(self: Image.Image) -> None:
        raise AssertionError("Image must be rejected by its header")

    monkeypatch.setattr(Image.Image, "load", decode)
    with pytest.raises(ImageRejected, match="pixel limit|proportions"):
        accept_upload(data)


def test_webp_has_lower_pixel_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """WebP при разборе держит несколько несжатых копий: ему предел 10 млн точек, кадр 4K проходит."""
    with pytest.raises(ImageRejected, match="uniform"):
        accept_upload(encoded(Image.new("RGB", (3840, 2160)), "WEBP"))
    with pytest.raises(ImageRejected, match="uniform"):
        accept_upload(encoded(Image.new("RGB", (3200, 3200))))
    webp = encoded(Image.new("RGB", (3200, 3200)), "WEBP")

    def decode(self: Image.Image) -> None:
        raise AssertionError("Image must be rejected by its header")

    monkeypatch.setattr(Image.Image, "load", decode)
    with pytest.raises(ImageRejected, match="pixel limit"):
        accept_upload(webp)


@pytest.mark.parametrize(("size", "fmt"), [((4000, 2500), "WEBP"), ((4000, 4000), "PNG")])
def test_image_of_exactly_pixel_limit_passes(size: tuple[int, int], fmt: str) -> None:
    assert size[0] * size[1] == (images.MAX_PIXELS_WEBP if fmt == "WEBP" else images.MAX_PIXELS)
    with pytest.raises(ImageRejected, match="uniform"):
        accept_upload(encoded(Image.new("RGB", size), fmt))


@pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
def test_large_photo_is_stored_reduced(photo: Image.Image, fmt: str) -> None:
    result = accept_upload(encoded(photo.resize((4032, 3024)), fmt))
    assert (result.width, result.height) == (MAX_SIDE, MAX_SIDE * 3 // 4)


def test_transparency_is_composited_on_white(photo: Image.Image) -> None:
    image = photo.convert("RGBA")
    image.putalpha(Image.new("L", photo.size, 255))
    image.paste((0, 0, 0, 0), (0, 0, 400, 400))
    result = accept_upload(encoded(image))
    with Image.open(BytesIO(result.data)) as decoded:
        assert all(channel > 245 for channel in decoded.getpixel((10, 10)))


def test_decoding_is_limited_to_two_at_once(photo: Image.Image, monkeypatch: pytest.MonkeyPatch) -> None:
    data = encoded(photo)
    active, peak = 0, 0
    guard = threading.Lock()
    transpose = images.ImageOps.exif_transpose

    def slow_transpose(image: Image.Image, **kwargs: object) -> None:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return transpose(image, **kwargs)

    monkeypatch.setattr(images.ImageOps, "exif_transpose", slow_transpose)
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda _: accept_upload(data), range(6)))
    assert len(results) == 6
    assert peak == 2


async def test_waiting_for_decode_slot_does_not_hold_pool_threads(photo: Image.Image) -> None:
    """Пока слоты держит сборка ролика, в потоках пула слот ждут не больше двух разборов: остальные ждут в цикле
    событий, и пул обслуживает другие запросы."""
    data = encoded(photo)
    started = []

    def upload() -> object:
        started.append(threading.get_ident())
        return accept_upload(data)

    images.DECODE_SLOTS.acquire()
    images.DECODE_SLOTS.acquire()
    try:
        uploads = [asyncio.create_task(images.run_decoding(upload)) for _ in range(6)]
        await asyncio.sleep(0.2)
        assert len(started) == 2
        assert await asyncio.wait_for(run_in_threadpool(lambda: "other request"), 1) == "other request"
    finally:
        images.DECODE_SLOTS.release()
        images.DECODE_SLOTS.release()
    assert len(await asyncio.gather(*uploads)) == 6
    assert len(started) == 6


def test_each_event_loop_has_its_own_decode_queue() -> None:
    """Перемотка демо и шаги демо-воркера идут каждый в своём asyncio.run: очередь прошлого цикла в новом упала бы с
    RuntimeError у третьего разбора, которому пришлось ждать слот."""

    def work() -> str:
        time.sleep(0.05)
        return "decoded"

    async def decode_three() -> list[str]:
        return await asyncio.gather(*(images.run_decoding(work) for _ in range(3)))

    assert asyncio.run(decode_three()) == ["decoded"] * 3
    assert asyncio.run(decode_three()) == ["decoded"] * 3


async def blocked_uploads(count: int, release: threading.Event, started: list) -> list[asyncio.Task]:
    def work() -> str:
        started.append(threading.get_ident())
        release.wait(5)
        return "decoded"

    tasks = [asyncio.create_task(images.run_upload_decoding(work)) for _ in range(count)]
    await asyncio.sleep(0.1)
    return tasks


async def test_upload_queue_overflow_is_rejected_at_once() -> None:
    release, started = threading.Event(), []
    try:
        tasks = await blocked_uploads(2 + images.UPLOAD_QUEUE, release, started)
        assert len(started) == 2
        with pytest.raises(images.DecodeBusy):
            await asyncio.wait_for(images.run_upload_decoding(lambda: "late"), 0.5)
    finally:
        release.set()
    assert await asyncio.gather(*tasks) == ["decoded"] * (2 + images.UPLOAD_QUEUE)
    assert await images.run_upload_decoding(lambda: "after") == "after"


async def test_upload_waiting_too_long_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(images, "UPLOAD_WAIT", 0.2)
    release, started = threading.Event(), []
    try:
        tasks = await blocked_uploads(2, release, started)
        with pytest.raises(images.DecodeBusy):
            await images.run_upload_decoding(lambda: "late")
        assert images._queue().uploads == 0
    finally:
        release.set()
    await asyncio.gather(*tasks)
    assert len(started) == 2


async def test_upload_waits_for_thread_decode_slot_only_until_its_deadline(monkeypatch, photo: Image.Image) -> None:
    monkeypatch.setattr(images, "UPLOAD_WAIT", 0.3)
    data = encoded(photo)
    images.DECODE_SLOTS.acquire()
    images.DECODE_SLOTS.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(images.DecodeBusy):
            await images.run_upload_decoding(accept_upload, data)
        assert time.monotonic() - started < 2
        # Разбор без срока (пересборка роликов в фоне того же запроса) по-прежнему ждёт слот.
        waiting = asyncio.create_task(images.run_decoding(accept_upload, data))
        await asyncio.sleep(0.5)
        assert not waiting.done()
    finally:
        images.DECODE_SLOTS.release()
        images.DECODE_SLOTS.release()
    assert (await waiting).width == photo.width
    assert images._queue().slots._value == 2


async def test_cancelled_upload_keeps_slot_until_its_thread_ends() -> None:
    """Поток отменённого запроса доделывает разбор: слот его до тех пор не отдаёт другому разбору и после не
    теряется."""
    release, started = threading.Event(), []
    try:
        running = await blocked_uploads(2, release, started)
        waiting = await blocked_uploads(2, release, started)
        for task in running + waiting[:1]:
            task.cancel()
        await asyncio.sleep(0.2)
        assert len(started) == 2
        assert images._queue().uploads == 1
    finally:
        release.set()
    assert await asyncio.gather(waiting[1]) == ["decoded"]
    assert len(started) == 3
    assert all(task.cancelled() for task in running + waiting[:1])
    assert images._queue().uploads == 0
    again = threading.Event()
    try:
        tasks = await blocked_uploads(2, again, started)
        assert len(started) == 5
    finally:
        again.set()
    await asyncio.gather(*tasks)


def test_gif_and_animated_png_rejected(photo: Image.Image) -> None:
    with pytest.raises(ImageRejected, match="Invalid or unsafe"):
        accept_upload(encoded(photo, "GIF"))
    frames = [photo, Image.new("RGB", photo.size, "red")]
    data = encoded(frames[0], save_all=True, append_images=frames[1:], duration=100)
    with pytest.raises(ImageRejected, match="single-frame"):
        accept_upload(data)
    animated = encoded(frames[0], "WEBP", save_all=True, append_images=frames[1:], duration=100)
    with pytest.raises(ImageRejected, match="single-frame"):
        accept_upload(animated)


def test_webp_is_accepted_and_reencoded_to_jpeg(photo: Image.Image) -> None:
    result = accept_upload(encoded(photo, "WEBP", quality=90))
    with Image.open(BytesIO(result.data)) as decoded:
        assert decoded.format == "JPEG"
    assert (result.width, result.height) == photo.size


def test_transparent_empty_image_rejected() -> None:
    with pytest.raises(ImageRejected, match="uniform"):
        accept_upload(encoded(Image.new("RGBA", (1080, 1080), (0, 0, 0, 0))))


def test_perceptual_duplicates_survive_resize_and_jpeg(photo: Image.Image) -> None:
    first = check_image(encoded(photo))
    resized = photo.resize((1000, 800))
    with pytest.raises(ImageRejected, match="Duplicate"):
        check_image(encoded(resized, "JPEG", quality=80), existing_hashes=[first.phash])
    assert is_duplicate(first.phash, [first.phash])
    assert not is_duplicate("0000000000000000", ["ffffffffffffffff"])


def test_different_content_not_duplicate(photo: Image.Image) -> None:
    other = Image.new("RGB", photo.size, "white")
    draw = ImageDraw.Draw(other)
    for x in range(0, other.width, 100):
        draw.rectangle((x, 0, x + 45, other.height), fill="black")
    assert not is_duplicate(perceptual_hash(photo), [perceptual_hash(other)])


@pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
def test_owner_photo_exif_removed_and_orientation_applied(photo: Image.Image, fmt: str) -> None:
    exif = Image.Exif()
    exif[274] = 6
    exif[271] = "Private camera metadata"
    result = accept_upload(encoded(photo, fmt, exif=exif))
    assert result.kind == "upload"
    assert result.label is None
    assert result.cost_kop == 0
    assert result.selected is False
    assert (result.width, result.height) == (1100, 1400)
    with Image.open(BytesIO(result.data)) as clean:
        assert not clean.getexif()
        assert b"Private camera metadata" not in result.data


def test_platform_formats(photo: Image.Image) -> None:
    outputs = make_formats(encoded(photo))
    assert outputs.keys() == FORMATS.keys()
    for name, data in outputs.items():
        assert 0 < len(data) <= MAX_BYTES
        with Image.open(BytesIO(data)) as image:
            assert image.size == FORMATS[name]
            assert image.format == "JPEG"
            assert not image.getexif()
    assert FORMATS["direct_4_3"][0] * 3 == FORMATS["direct_4_3"][1] * 4


@pytest.fixture
def font_path() -> str:
    if not Path(DEFAULT_FONT).is_file():
        pytest.skip("DejaVu Sans is supplied by fonts-dejavu-core in the container")
    return DEFAULT_FONT


def test_dejavu_cyrillic_offer_on_all_formats(photo: Image.Image, font_path: str) -> None:
    outputs = make_formats(encoded(photo), offer="Маникюр к празднику\nЗапись рядом с домом", font_path=font_path)
    assert outputs.keys() == FORMATS.keys()


def test_offer_bounds_and_wrapping(photo: Image.Image, monkeypatch: pytest.MonkeyPatch) -> None:
    # A bundled Pillow font exercises layout even on hosts without container fonts.
    bundled_font = ImageFont.load_default(size=18)
    monkeypatch.setattr(ImageFont, "truetype", lambda path, size: bundled_font.font_variant(size=size))
    source = photo.resize((256, 256))
    result = overlay_offer(source, "LongOfferWithoutSpacesToWrap\nA second line")
    changed = Image.new("L", source.size)
    pixels = changed.load()
    for y in range(256):
        for x in range(256):
            if source.getpixel((x, y)) != result.getpixel((x, y)):
                pixels[x, y] = 255
    left, top, right, bottom = changed.getbbox()
    assert 0 < left < right < 256
    assert 0 < top < bottom < 256
    assert result.getpixel((128, 240)) == (20, 20, 20)


def test_excessive_offer_is_rejected(photo: Image.Image, monkeypatch: pytest.MonkeyPatch) -> None:
    bundled_font = ImageFont.load_default(size=18)
    monkeypatch.setattr(ImageFont, "truetype", lambda path, size: bundled_font.font_variant(size=size))
    with pytest.raises(ImageRejected, match="shorten"):
        overlay_offer(photo.resize((256, 256)), "Very long offer " * 60)


def test_missing_font_is_explicit(photo: Image.Image) -> None:
    # Имя несуществующее и нигде не ищется: PIL по пути без файла отвечает OSError,
    # но по известному имени (DejaVuSans.ttf) подставит системный шрифт из поискового пути.
    with pytest.raises(OSError):
        overlay_offer(photo, "Оффер", font_path="/nonexistent/no-such-font-autopilot-404.ttf")
