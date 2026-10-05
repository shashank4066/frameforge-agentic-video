"""Bounded, inspected local media uploads; original filenames never become paths."""
import asyncio
import math
from pathlib import Path
import uuid

from fastapi import HTTPException, UploadFile
from PIL import Image

from . import media
from .providers import ProviderError

VISUAL_LIMIT = 25 * 1024 * 1024
AUDIO_LIMIT = 15 * 1024 * 1024


def _inspect_upload(source: Path, directory: Path, kind: str, ffmpeg_path: str) -> dict:
    stem = uuid.uuid4().hex
    header = source.read_bytes()[:16] if source.stat().st_size < 16 else b""
    if not header:
        with source.open("rb") as stream:
            header = stream.read(16)
    if kind == "visual" and (header.startswith(b"\x89PNG") or header.startswith(b"\xff\xd8") or (header[:4] == b"RIFF" and header[8:12] == b"WEBP")):
        try:
            with Image.open(source) as image:
                if not 32 <= image.width <= 8000 or not 32 <= image.height <= 8000 or image.width * image.height > 40_000_000:
                    raise ProviderError("Use an image between 32 pixels and 40 megapixels.")
                suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(image.format)
                if not suffix:
                    raise ProviderError("Use a PNG, JPEG, or WebP image.")
                image.verify()
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ProviderError("This image cannot be read. Upload a PNG, JPEG, or WebP file.") from exc
        destination = directory / "images" / f"upload-{stem}{suffix}"
        destination.parent.mkdir(exist_ok=True)
        source.replace(destination)
        return {"path": str(destination), "kind": "image", "provider": "uploaded-media", "draft": False}

    is_video = header[4:8] == b"ftyp" or header.startswith(b"\x1a\x45\xdf\xa3")
    is_audio = (header[:4] == b"RIFF" and header[8:12] == b"WAVE") or header.startswith((b"ID3", b"OggS")) or (len(header) >= 2 and header[0] == 255 and header[1] & 224 == 224) or header[4:8] == b"ftyp"
    if (kind == "visual" and not is_video) or (kind != "visual" and not is_audio):
        raise ProviderError("Use an MP4/WebM video or PNG/JPEG/WebP image." if kind == "visual" else "Use WAV, MP3, M4A, or OGG audio.")
    metadata = media._probe(source, ffmpeg_path)
    if not metadata:
        raise ProviderError("Media inspection is unavailable. Install FFprobe alongside FFmpeg.")
    streams = metadata.get("streams", [])
    wanted = "video" if kind == "visual" else "audio"
    if not any(stream.get("codec_type") == wanted for stream in streams):
        raise ProviderError(f"The uploaded file has no {wanted} stream.")
    try:
        duration = float(metadata["format"]["duration"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ProviderError("The uploaded file has invalid timing.") from exc
    maximum = 180 if kind == "visual" else 300 if kind == "music" else 60
    if not math.isfinite(duration) or not 0 < duration <= maximum:
        raise ProviderError(f"Upload a {wanted} file shorter than {maximum} seconds.")
    if kind == "visual":
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        if not 32 <= video.get("width", 0) <= 3840 or not 32 <= video.get("height", 0) <= 3840:
            raise ProviderError("Use a video up to 3840 pixels on each side.")
        suffix = ".mp4" if header[4:8] == b"ftyp" else ".webm"
        destination = directory / "images" / f"upload-{stem}{suffix}"
        destination.parent.mkdir(exist_ok=True)
        source.replace(destination)
        return {"path": str(destination), "kind": "video", "provider": "uploaded-media", "draft": False,
                "duration_seconds": round(duration, 3)}
    destination = directory / "audio" / f"{kind}-upload-{stem}.wav"
    destination.parent.mkdir(exist_ok=True)
    try:
        media._run([str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y",
                    "-protocol_whitelist", "file,pipe,crypto", "-i", str(source), "-map", "0:a:0", "-vn",
                    "-t", str(maximum), "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(destination)], timeout=40)
        measured = media._duration(destination, ffmpeg_path)
        if not measured or measured <= 0:
            raise ProviderError("The uploaded recording contains no audio.")
        return {"path": str(destination), "provider": "uploaded-recording" if kind == "voice" else "uploaded-music",
                "has_speech": kind == "voice", "duration_seconds": round(measured, 3)}
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


async def prepare_upload(file: UploadFile, directory: Path, kind: str, ffmpeg_path: str) -> dict:
    folder = directory / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / f"pending-{uuid.uuid4().hex}.bin"
    limit = VISUAL_LIMIT if kind == "visual" else AUDIO_LIMIT
    count = 0
    try:
        with source.open("xb") as stream:
            while chunk := await file.read(1024 * 1024):
                count += len(chunk)
                if count > limit:
                    raise HTTPException(413, f"Upload limit is {limit // (1024 * 1024)} MB.")
                stream.write(chunk)
        if not count:
            raise HTTPException(422, "Choose a non-empty media file.")
        try:
            asset = await asyncio.to_thread(_inspect_upload, source, directory, kind, ffmpeg_path)
        except ProviderError as exc:
            raise HTTPException(422, str(exc)) from exc
        asset["filename"] = (file.filename or "Uploaded media").replace("\\", "/").split("/")[-1][:120]
        return asset
    finally:
        source.unlink(missing_ok=True)
        await file.close()
