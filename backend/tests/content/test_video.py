import json
import shutil
import subprocess
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.content import video
from app.content.images import DEFAULT_FONT


@pytest.fixture
def pictures(tmp_path: Path) -> list[Path]:
    paths = []
    for index, color in enumerate(("#486c84", "#906050", "#608040", "#805080")):
        image = Image.new("RGB", (800, 600), "#f0d0a0")
        ImageDraw.Draw(image).ellipse((80 + index * 40, 90, 650, 500), fill=color)
        path = tmp_path / f"photo {index}.png"
        image.save(path)
        paths.append(path)
    return paths


@pytest.fixture
def font_path() -> str:
    for candidate in (DEFAULT_FONT, "/System/Library/Fonts/Supplemental/Arial.ttf", "DejaVuSans.ttf"):
        try:
            return str(ImageFont.truetype(candidate, size=24).path)
        except OSError:
            continue
    pytest.fail("A Cyrillic TrueType font is required for offer rendering")


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Video integration tests require ffmpeg and ffprobe in PATH",
)
@pytest.mark.parametrize(
    ("aspect", "size", "seconds", "count", "audio"),
    [
        ("1:1", (1080, 1080), 6, 3, False),
        ("16:9", (1920, 1080), 8, 3, False),
        ("9:16", (1080, 1920), 10, 3, False),
        ("1:1", (1080, 1080), 6.25, 4, True),
    ],
)
def test_build_video(
    pictures: list[Path], font_path: str, tmp_path: Path,
    aspect: str, size: tuple[int, int], seconds: float, count: int, audio: bool,
) -> None:
    result = video.build_video(
        pictures[:count], "Маникюр к празднику\nЗапись рядом с домом", aspect, seconds,
        with_silent_audio=audio, font_path=font_path,
    )
    path = tmp_path / "result.mp4"
    path.write_bytes(result.data)
    info = video.probe(path)
    assert info.duration == pytest.approx(seconds, abs=0.3)
    assert info.codec == "h264"
    assert (info.width, info.height) == size
    with Image.open(BytesIO(result.preview)) as preview:
        assert preview.format == "JPEG"
        assert preview.size == size
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    streams = json.loads(output.stdout)["streams"]
    picture = next(stream for stream in streams if stream["codec_type"] == "video")
    assert picture["pix_fmt"] == "yuv420p"
    assert picture["r_frame_rate"] == "30/1"
    sound = [stream for stream in streams if stream["codec_type"] == "audio"]
    assert [stream["codec_name"] for stream in sound] == (["aac"] if audio else [])


@pytest.mark.parametrize("count", [3, 4])
@pytest.mark.parametrize("audio", [False, True])
def test_command_without_ffmpeg(count: int, audio: bool) -> None:
    command = video._build_command(count, (1920, 1080), 8, audio)
    graph = command[command.index("-filter_complex") + 1]
    assert command[0] == "ffmpeg"
    assert command.count("-i") == count + audio
    for index in range(count):
        assert f"frame-{index}.png" in command
    assert graph.count("zoompan=") == count
    assert graph.count("xfade=transition=fade:duration=0.5") == count - 1
    assert "s=1920x1080:fps=30" in graph
    assert "drawtext" not in graph
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-r") + 1] == "30"
    assert float(command[command.index("-t") + 1]) == 8
    if audio:
        assert "anullsrc=r=48000:cl=stereo" in command
        assert command[command.index("-c:a") + 1] == "aac"
    else:
        assert "-an" in command


@pytest.mark.parametrize(
    ("count", "aspect", "seconds"),
    [(2, "1:1", 6), (5, "1:1", 6), (3, "4:3", 6), (3, "1:1", 5.9),
     (3, "1:1", 10.1), (3, "1:1", float("nan")), (3, "1:1", float("inf")), (3, "1:1", True)],
)
def test_invalid_input(count: int, aspect: str, seconds: float) -> None:
    with pytest.raises(ValueError):
        video.build_video([Path("unused.png")] * count, "Оффер", aspect, seconds)


def test_subprocess_and_cleanup(
    pictures: list[Path], font_path: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        directory = kwargs["cwd"]
        assert isinstance(directory, Path)
        assert directory.is_dir()
        assert kwargs == {
            "cwd": directory, "capture_output": True, "text": True, "check": True, "timeout": video.TIMEOUT,
        }
        calls.append((command, directory))
        if command[-1] == "video.mp4":
            with Image.open(directory / "frame-0.png") as frame:
                assert frame.size == (1080, 1080)
                assert frame.getpixel((540, 1030)) == (20, 20, 20)
            (directory / "video.mp4").write_bytes(b"mp4")
        else:
            (directory / "preview.jpg").write_bytes(b"jpeg")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(video.subprocess, "run", run)
    result = video.build_video(pictures[:3], "Оффер", font_path=font_path)
    assert result == video.BuiltVideo(b"mp4", b"jpeg")
    assert len(calls) == 2
    assert all(not directory.exists() for _, directory in calls)


@pytest.mark.parametrize(
    ("failure", "message"),
    [(FileNotFoundError(), "ffmpeg.*PATH"),
     (subprocess.TimeoutExpired("ffmpeg", video.TIMEOUT), "timeout"),
     (subprocess.CalledProcessError(1, "ffmpeg", stderr="encoder failed"), "encoder failed")],
)
def test_build_failure_cleanup(
    pictures: list[Path], font_path: str, monkeypatch: pytest.MonkeyPatch,
    failure: Exception, message: str,
) -> None:
    directories = []

    def run(command: list[str], **kwargs: object) -> None:
        directories.append(kwargs["cwd"])
        raise failure

    monkeypatch.setattr(video.subprocess, "run", run)
    with pytest.raises(video.VideoError, match=message):
        video.build_video(pictures[:3], "Оффер", font_path=font_path)
    assert directories
    assert all(not directory.exists() for directory in directories)


@pytest.mark.parametrize("metadata", ["not json", "{}", '{"streams": []}',
                                      '{"streams": [{}], "format": {"duration": "NaN"}}'])
def test_probe_invalid_metadata(metadata: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(video, "_run", lambda command, directory: metadata)
    with pytest.raises(video.VideoError, match="invalid video metadata"):
        video.probe(tmp_path / "invalid.mp4")


def test_missing_ffprobe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(video.subprocess, "run", run)
    with pytest.raises(video.VideoError, match="ffprobe.*PATH"):
        video.probe(tmp_path / "video.mp4")
