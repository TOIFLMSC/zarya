"""Local-only, bounded FFmpeg preparation; no network inputs or shell."""

import asyncio
import json
import math
import os
import shutil
import wave
from pathlib import Path
from typing import Any

MAX_MEDIA_BYTES = 20 * 1024 * 1024


def frame_times(duration: float, maximum: int, frame_rate: float = 25) -> list[float]:
    """Two-second coverage, including endpoints; spread a capped budget over the whole clip."""
    count = min(maximum, max(2, math.ceil(duration / 2) + 1))
    if count == 1:
        return [round(duration / 2, 3)]
    end = max(0.0, duration - max(0.1, 1 / max(frame_rate, 0.01)))
    return sorted({round(end * index / (count - 1), 3) for index in range(count)})


class MediaProcessor:
    def __init__(self, ffmpeg: str = "", ffprobe: str = ""):
        root = Path(__file__).resolve().parents[2] / ".tools" / "ffmpeg"
        self.ffmpeg = (
            ffmpeg
            or shutil.which("ffmpeg")
            or next((str(p) for p in root.glob("*/bin/ffmpeg.exe")), "")
        )
        self.ffprobe = (
            ffprobe
            or shutil.which("ffprobe")
            or next((str(p) for p in root.glob("*/bin/ffprobe.exe")), "")
        )

    @property
    def available(self) -> bool:
        return bool(
            self.ffmpeg
            and self.ffprobe
            and Path(self.ffmpeg).is_file()
            and Path(self.ffprobe).is_file()
        )

    async def run(self, executable: str, *args: str) -> bytes:
        options: dict[str, Any] = {"creationflags": 0x08000000} if os.name == "nt" else {}
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **options,
        )
        try:
            async with asyncio.timeout(20):
                assert process.stdout
                chunks = bytearray()
                while block := await process.stdout.read(4096):
                    chunks.extend(block)
                    if len(chunks) > 65536:
                        raise ValueError("probe_output_limit")
                await process.wait()
                if process.returncode:
                    raise ValueError("decoder_rejected")
                return bytes(chunks)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def prepare(
        self, data: bytes, directory: Path, kind: str, limits: dict[str, int]
    ) -> dict[str, Any]:
        if not self.available:
            raise ValueError("tools_unavailable")
        if not data or len(data) > MAX_MEDIA_BYTES:
            raise ValueError("file_size")
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        source = directory / "input.bin"
        await asyncio.to_thread(source.write_bytes, data)
        try:
            async with asyncio.timeout(90):
                raw = await self.run(
                    self.ffprobe,
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file",
                    "-format_whitelist",
                    "ogg,mov,matroska,webm,gif,wav,mp3",
                    "-show_entries",
                    "format=duration,format_name:stream=codec_type,width,height,duration,avg_frame_rate",
                    "-of",
                    "json",
                    str(source),
                )
                probe = json.loads(raw)
                formats = set(probe.get("format", {}).get("format_name", "").split(","))
                if not formats.intersection(
                    {"ogg", "mov", "mp4", "matroska", "webm", "gif", "wav", "mp3"}
                ):
                    raise ValueError("media_format")
                duration = float(probe.get("format", {}).get("duration", 0))
                maximum = limits["voice"] if kind == "voice" else limits["video"]
                if not math.isfinite(duration) or duration <= 0 or duration > maximum + 0.1:
                    raise ValueError("duration_limit")
                streams = probe.get("streams", [])
                video = next((s for s in streams if s.get("codec_type") == "video"), None)
                audio = any(s.get("codec_type") == "audio" for s in streams)
                if kind != "voice" and not video:
                    raise ValueError("missing_video")
                if video and (
                    int(video.get("width", 0)) * int(video.get("height", 0)) > 16_000_000
                ):
                    raise ValueError("video_pixels")
                base = (
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-threads",
                    "1",
                    "-protocol_whitelist",
                    "file",
                    "-format_whitelist",
                    "ogg,mov,matroska,webm,gif,wav,mp3",
                    "-i",
                    str(source),
                )
                frames = []
                if video and kind != "voice":
                    numerator, denominator = video.get("avg_frame_rate", "25/1").split("/")
                    rate = float(numerator) / float(denominator) if float(denominator) else 25
                    # The container may include an audio tail beyond the last video frame.
                    # Keep its duration for limits/audio, but sample within the video stream.
                    try:
                        video_duration = float(video.get("duration", duration))
                    except (TypeError, ValueError):
                        video_duration = duration
                    if not math.isfinite(video_duration) or video_duration <= 0:
                        video_duration = duration
                    times = frame_times(min(duration, video_duration), limits["frames"], rate)
                    for index, timestamp in enumerate(times):
                        name = f"frame-{index}.jpg"
                        await self.run(
                            self.ffmpeg,
                            *base[:-2],
                            "-ss",
                            str(timestamp),
                            *base[-2:],
                            "-map",
                            "0:v:0",
                            "-an",
                            "-frames:v",
                            "1",
                            "-vf",
                            "scale=1280:1280:force_original_aspect_ratio=decrease",
                            "-q:v",
                            "3",
                            str(directory / name),
                        )
                        path = directory / name
                        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                            raise ValueError("frame_unavailable")
                        frames.append(
                            {"path": f"{directory.name}/{name}", "seconds": round(timestamp, 3)}
                        )
                audio_bytes = None
                audio_duration = None
                signal = "missing"
                if audio:
                    destination = directory / "audio.wav"
                    await self.run(
                        self.ffmpeg,
                        *base,
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-t",
                        str(maximum),
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-c:a",
                        "pcm_s16le",
                        str(destination),
                    )
                    if destination.stat().st_size > 10 * 1024 * 1024:
                        raise ValueError("audio_size")
                    audio_bytes = await asyncio.to_thread(destination.read_bytes)
                    with wave.open(str(destination), "rb") as extracted:
                        audio_duration = extracted.getnframes() / extracted.getframerate()
                    signal = await asyncio.to_thread(self.signal, destination)
                    if signal == "low_signal":
                        audio_bytes = None
                return {
                    "duration": duration,
                    "frames": frames,
                    "audio": audio_bytes,
                    "audio_duration": audio_duration,
                    "coverage": {
                        "frame_times": [f["seconds"] for f in frames],
                        "sampling": "uniform_2s_capped_endpoints",
                        "audio_signal": signal,
                        "audio_intervals": [],
                        "note": "Выбранные кадры в хронологическом порядке; "
                        "отметки приблизительны. "
                        "Промежутки между кадрами не просмотрены. "
                        "Распознавание речи может содержать ошибки.",
                    },
                }
        finally:
            source.unlink(missing_ok=True)

    @staticmethod
    def signal(path: Path) -> str:
        import array

        with wave.open(str(path), "rb") as audio:
            samples = array.array("h", audio.readframes(audio.getnframes()))
        rms = math.sqrt(sum(float(s) * s for s in samples) / max(1, len(samples)))
        return "low_signal" if rms < 40 else "present"
