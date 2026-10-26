import json
import math
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from PIL import Image, ImageOps

from app.content.images import DEFAULT_FONT, _decode, overlay_offer

SIZES = {"1:1": (1080, 1080), "16:9": (1920, 1080), "9:16": (1080, 1920)}
FPS = 30
FADE_FRAMES = 15
TIMEOUT = 180


class VideoError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuiltVideo:
    data: bytes
    preview: bytes


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    codec: str
    width: int
    height: int


def _run(command: list[str], directory: Path) -> str:
    try:
        result = subprocess.run(
            command, cwd=directory, capture_output=True, text=True, check=True, timeout=TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise VideoError(f"{command[0]} is not installed or not available in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoError(f"{command[0]} exceeded the {TIMEOUT} second timeout") from exc
    except subprocess.CalledProcessError as exc:
        raise VideoError(f"{command[0]} failed: {exc.stderr[-2000:]}") from exc
    return result.stdout


def _build_command(
    count: int, size: tuple[int, int], seconds: float, with_silent_audio: bool,
) -> list[str]:
    width, height = size
    total_frames = round(seconds * FPS)
    frames, extra = divmod(total_frames + FADE_FRAMES * (count - 1), count)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-filter_complex_threads", "1"]
    filters = []
    elapsed = 0
    previous = "v0"
    for index in range(count):
        command.extend(["-i", f"frame-{index}.png"])
        length = frames + (index < extra)
        progress = f"on/{length - 1}"
        # Keep the offer inside overlay_offer's 4% horizontal margins throughout the pan.
        zoom = f"1.03-0.03*{progress}" if index % 2 else f"1+0.03*{progress}"
        x = f"(iw-iw/zoom)*{progress}"
        filters.append(
            f"[{index}:v]scale={width * 2}:{height * 2},"
            f"zoompan=z='{zoom}':x='{x}':y='(ih-ih/zoom)/2':d={length}:s={width}x{height}:fps={FPS},"
            f"setsar=1,format=yuv420p,settb=AVTB[v{index}]"
        )
        if index:
            offset = (elapsed - FADE_FRAMES) / FPS
            label = f"fade{index}"
            filters.append(
                f"[{previous}][v{index}]xfade=transition=fade:duration={FADE_FRAMES / FPS}:"
                f"offset={offset:.9f}[{label}]"
            )
            previous = label
            elapsed -= FADE_FRAMES
        elapsed += length
    if with_silent_audio:
        command.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
    command.extend(["-filter_complex", ";".join(filters), "-map", f"[{previous}]"])
    if with_silent_audio:
        command.extend(["-map", f"{count}:a:0", "-c:a", "aac", "-b:a", "64k"])
    else:
        command.append("-an")
    command.extend([
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-threads", "2", "-r", str(FPS), "-t", f"{total_frames / FPS:.9f}",
        "-movflags", "+faststart", "video.mp4",
    ])
    return command


def build_video(
    images: Sequence[str | Path],
    offer_text: str,
    aspect: Literal["1:1", "16:9", "9:16"] = "1:1",
    seconds: float = 6,
    *,
    with_silent_audio: bool = False,
    font_path: str = DEFAULT_FONT,
) -> BuiltVideo:
    """Return MP4 and JPEG bytes; all intermediate files are removed, including on failure."""
    if len(images) not in (3, 4):
        raise ValueError("Video requires 3 or 4 images")
    if aspect not in SIZES:
        raise ValueError("Unsupported video aspect ratio")
    if isinstance(seconds, bool) or not math.isfinite(seconds) or not 6 <= seconds <= 10:
        raise ValueError("Video duration must be between 6 and 10 seconds")
    size = SIZES[aspect]
    with TemporaryDirectory(prefix="ad-video-") as temporary:
        directory = Path(temporary)
        for index, path in enumerate(images):
            image = _decode(Path(path).read_bytes())
            cropped = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
            frame = overlay_offer(cropped, offer_text, font_path=font_path)
            frame.save(directory / f"frame-{index}.png")
        _run(_build_command(len(images), size, seconds, with_silent_audio), directory)
        _run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", "video.mp4",
            "-frames:v", "1", "-q:v", "2", "preview.jpg",
        ], directory)
        return BuiltVideo((directory / "video.mp4").read_bytes(), (directory / "preview.jpg").read_bytes())


def probe(path: str | Path) -> VideoInfo:
    """Inspect the first video stream with ffprobe."""
    source = Path(path).resolve()
    with TemporaryDirectory(prefix="ad-probe-") as temporary:
        output = _run([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=codec_name,width,height:format=duration", "-of", "json", str(source),
        ], Path(temporary))
    try:
        metadata = json.loads(output)
        stream = metadata["streams"][0]
        duration = float(metadata["format"]["duration"])
        width, height = int(stream["width"]), int(stream["height"])
        codec = stream["codec_name"]
        if not math.isfinite(duration) or duration <= 0 or min(width, height) <= 0 or not isinstance(codec, str):
            raise ValueError("Invalid video metadata")
        return VideoInfo(duration, codec, width, height)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise VideoError("ffprobe returned invalid video metadata") from exc
