"""Local media validation, timed captions, and bounded FFmpeg composition."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import time
from typing import Any
import wave

from PIL import Image, ImageDraw

from .providers import ASPECT_SIZES, ProviderError, _font


def _run(command: list[str], *, timeout: float = 300, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except FileNotFoundError as exc:
        raise ProviderError("FFmpeg was not found. Install FFmpeg or set FFMPEG_PATH to its executable.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderError("Media rendering exceeded its time limit. Try a shorter video.") from exc
    if result.returncode:
        # FFmpeg stderr contains filenames, never provider request URLs or secrets.
        details = "\n".join(result.stderr.strip().splitlines()[-6:])[-1200:]
        raise ProviderError(f"Media rendering failed: {details}")
    return result


def ffmpeg_available(ffmpeg_path: str = "ffmpeg") -> bool:
    try:
        _run([str(ffmpeg_path), "-version"], timeout=8)
        return True
    except (ProviderError, OSError):
        return False


def _ffprobe_path(ffmpeg_path: str) -> str | None:
    candidate = Path(ffmpeg_path).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if candidate.is_file():
        return str(candidate)
    return shutil.which("ffprobe")


def _probe(path: Path, ffmpeg_path: str) -> dict[str, Any] | None:
    executable = _ffprobe_path(ffmpeg_path)
    if not executable:
        return None
    result = _run([executable, "-v", "error", "-protocol_whitelist", "file,pipe,crypto",
                   "-show_entries", "format=duration:stream=codec_type,width,height,duration",
                   "-of", "json", str(path.resolve())], timeout=25)
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise ProviderError("Could not inspect generated media.") from exc
    return data


def _duration(path: Path, ffmpeg_path: str) -> float | None:
    try:
        with wave.open(str(path), "rb") as audio:
            # Streaming WAV responses may leave the RIFF data size at 0xffffffff.
            # Bound header-reported frames by the actual local file size instead
            # of speeding up a short narration as though it were hours long.
            bytes_per_frame = audio.getnchannels() * audio.getsampwidth()
            frames = min(audio.getnframes(), path.stat().st_size // bytes_per_frame)
            return frames / audio.getframerate()
    except (wave.Error, EOFError):
        pass
    metadata = _probe(path, ffmpeg_path)
    if metadata:
        try:
            return float(metadata["format"]["duration"])
        except (KeyError, ValueError, TypeError):
            pass
    return None


def validate_assets(scenes: list[dict[str, Any]], visual_assets: list[dict[str, Any]],
                    voice_assets: list[dict[str, Any]], ffmpeg_path: str = "ffmpeg") -> dict[str, Any]:
    if not scenes or len(scenes) != len(visual_assets) or len(scenes) != len(voice_assets):
        raise ProviderError("Every scene must have exactly one visual and one narration asset.")
    for scene, visual, voice in zip(scenes, visual_assets, voice_assets, strict=True):
        duration = float(scene.get("duration_seconds", 0))
        if not math.isfinite(duration) or duration < 0.5 or duration > 600:
            raise ProviderError("Invalid scene timing. Retry scene planning.")
        for asset in (visual, voice):
            path = Path(asset.get("path", ""))
            if not path.is_file() or path.stat().st_size == 0:
                raise ProviderError(f"A generated asset is missing for {scene.get('id', 'a scene')}.")
        image_path = Path(visual["path"])
        if visual.get("kind") == "image":
            try:
                with Image.open(image_path) as image:
                    if image.width < 32 or image.height < 32 or image.width * image.height > 40_000_000:
                        raise ProviderError("A scene image has invalid dimensions.")
                    image.verify()
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise ProviderError("A generated scene image is corrupt.") from exc
        elif visual.get("kind") == "video":
            with image_path.open("rb") as asset:
                header = asset.read(16)
            if not (header[4:8] == b"ftyp" or header.startswith(b"\x1a\x45\xdf\xa3")):
                raise ProviderError("A generated scene must be an MP4 or WebM video file, not a playlist.")
            metadata = _probe(image_path, ffmpeg_path)
            if metadata is not None and not any(s.get("codec_type") == "video" for s in metadata.get("streams", [])):
                raise ProviderError("A generated video scene has no video stream.")
        else:
            raise ProviderError("A visual asset must be an image or a video.")
        audio_duration = _duration(Path(voice["path"]), ffmpeg_path)
        if audio_duration is not None and (not math.isfinite(audio_duration) or audio_duration <= 0):
            raise ProviderError("A generated narration asset contains no audio.")
    return {"valid": True, "issues": [], "scene_count": len(scenes),
            "duration_seconds": round(sum(float(s["duration_seconds"]) for s in scenes), 3),
            "has_speech": all(bool(v.get("has_speech")) for v in voice_assets)}


def _timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_subtitles(scenes: list[dict[str, Any]], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    cues = []
    offset = 0.0
    cue_index = 1
    for scene in scenes:
        duration = float(scene["duration_seconds"])
        # Remove SRT/ASS control markup while retaining readable content.
        narration = str(scene["narration"]).replace("\r", " ").replace("\n", " ")
        narration = narration.replace("-->", "→").replace("{", "(").replace("}", ")")
        words = narration.split()
        groups = [words[i:i+8] for i in range(0, len(words), 8)] or [[" "]]
        consumed = 0
        for group in groups:
            start = offset + duration * consumed / max(1, len(words))
            consumed += len(group)
            end = offset + duration * min(consumed, len(words)) / max(1, len(words))
            text = "\n".join(textwrap.wrap(" ".join(group), width=38))
            cues.append(f"{cue_index}\n{_timestamp(start)} --> {_timestamp(end)}\n{text}\n")
            cue_index += 1
        offset += duration
    destination.write_text("\n".join(cues), encoding="utf-8")
    return destination


def _atempo(factor: float) -> str:
    parts = []
    while factor > 2:
        parts.append("atempo=2.0")
        factor /= 2
    while factor < 0.5:
        parts.append("atempo=0.5")
        factor /= 0.5
    parts.append(f"atempo={factor:.6f}")
    return ",".join(parts)


def _caption_overlay(scene: dict[str, Any], path: Path, width: int, height: int) -> None:
    """Fallback burnt-in caption when an FFmpeg build does not include libass."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    caption = "\n".join(textwrap.wrap(" ".join(scene["narration"].split()), width=38)[:5])
    font = _font(24 if width >= 720 else 21, True)
    bbox = draw.multiline_textbbox((0, 0), caption, font=font, spacing=6, align="center")
    block_width, block_height = bbox[2]-bbox[0], bbox[3]-bbox[1]
    x, y = (width-block_width)//2, height-block_height-int(height*0.085)
    draw.rounded_rectangle((x-22, y-15, x+block_width+22, y+block_height+15), radius=12, fill=(8, 12, 24, 210))
    draw.multiline_text((x, y-bbox[1]), caption, font=font, spacing=6,
                        fill=(255, 255, 255, 255), align="center")
    image.save(path)


def compose_video(scenes: list[dict[str, Any]], visual_assets: list[dict[str, Any]],
                  voice_assets: list[dict[str, Any]], subtitle_path: Path, output_path: Path,
                  aspect_ratio: str, ffmpeg_path: str = "ffmpeg") -> dict[str, Any]:
    started = time.monotonic()
    validation = validate_assets(scenes, visual_assets, voice_assets, ffmpeg_path)
    if aspect_ratio not in ASPECT_SIZES:
        raise ProviderError("Aspect ratio must be 16:9, 9:16, or 1:1.")
    if not subtitle_path.is_file():
        raise ProviderError("Subtitles are missing. Build captions before composition.")
    if not ffmpeg_available(ffmpeg_path):
        raise ProviderError("FFmpeg was not found. Install FFmpeg or set FFMPEG_PATH to its executable.")
    # Resolve executable before changing working directories for safe subtitle paths.
    resolved_ffmpeg = shutil.which(str(ffmpeg_path)) or str(Path(ffmpeg_path).resolve())
    width, height = ASPECT_SIZES[aspect_ratio]
    output_path = output_path.resolve()
    subtitle_path = subtitle_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filters = _run([resolved_ffmpeg, "-hide_banner", "-filters"], timeout=10).stdout
    has_subtitles_filter = any(line.split()[1:2] == ["subtitles"] for line in filters.splitlines())
    total_duration = validation["duration_seconds"]
    def remaining_timeout(requested: float) -> float:
        remaining = 270 - (time.monotonic() - started)
        if remaining <= 0:
            raise ProviderError("Media rendering exceeded its time limit. Try a shorter video.")
        return min(requested, remaining)

    temporary_output = output_path.with_name(output_path.stem + ".rendering.mp4")
    try:
        with tempfile.TemporaryDirectory(prefix="render-", dir=output_path.parent) as temporary:
            work = Path(temporary)
            segments = []
            for index, (scene, visual, voice) in enumerate(zip(scenes, visual_assets, voice_assets, strict=True)):
                duration = float(scene["duration_seconds"])
                frames = math.ceil(duration * 30)
                render_duration = frames / 30
                segment = work / f"scene-{index:02d}.mp4"
                segments.append(segment)
                command = [resolved_ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                           "-protocol_whitelist", "file,pipe,crypto"]
                if visual["kind"] == "image":
                    command += ["-loop", "1", "-framerate", "30"]
                else:
                    command += ["-stream_loop", "-1"]
                command += ["-i", str(Path(visual["path"]).resolve()),
                            "-protocol_whitelist", "file,pipe,crypto", "-i", str(Path(voice["path"]).resolve())]
                video_filter = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
                if visual["kind"] == "image":
                    video_filter += (f",zoompan=z='min(pzoom+0.0003,1.06)':"
                                     f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={width}x{height}:fps=30")
                else:
                    video_filter += ",fps=30"
                if has_subtitles_filter:
                    scene_caption = work / f"scene-{index:02d}.srt"
                    build_subtitles([scene], scene_caption)
                    # SRT/libass uses a 384x288 script canvas; these sizes scale
                    # to about 35-45 actual output pixels across the three ratios.
                    font_size = 14 if width >= height else 10
                    video_filter += (f",subtitles=filename='{scene_caption.name}':"
                                     f"force_style='FontName=Arial,FontSize={font_size},PrimaryColour=&HFFFFFF,"
                                     "OutlineColour=&H80101018,BorderStyle=3,Outline=2,Shadow=0,"
                                     "Alignment=2,MarginV=30'")
                    filter_complex = f"[0:v]{video_filter}[v];"
                else:
                    overlay = work / f"caption-{index:02d}.png"
                    _caption_overlay(scene, overlay, width, height)
                    command += ["-loop", "1", "-i", str(overlay)]
                    filter_complex = f"[0:v]{video_filter}[base];[base][2:v]overlay=0:0[v];"
                audio_duration = _duration(Path(voice["path"]), ffmpeg_path)
                audio_filter = "aresample=48000"
                if audio_duration and audio_duration > duration:
                    audio_filter += "," + _atempo(audio_duration / duration)
                audio_filter += f",apad,atrim=duration={render_duration:.6f},asetpts=PTS-STARTPTS"
                filter_complex += f"[1:a]{audio_filter}[a]"
                command += ["-filter_complex", filter_complex, "-map", "[v]", "-map", "[a]",
                            "-t", f"{render_duration:.6f}", "-r", "30", "-c:v", "libx264",
                            "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                            "-threads", "2", "-movflags", "+faststart", str(segment)]
                _run(command, cwd=work, timeout=remaining_timeout(max(120, duration*10)))
            concat_path = work / "segments.txt"
            # Filenames are internally generated; no user strings enter this list.
            concat_path.write_text("\n".join(f"file '{segment.name}'" for segment in segments), encoding="utf-8")
            shutil.copyfile(subtitle_path, work / "captions.srt")
            command = [resolved_ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                       "-f", "concat", "-safe", "1", "-protocol_whitelist", "file,pipe,crypto",
                       "-i", str(concat_path), "-i", "captions.srt", "-map", "0:v:0", "-map", "0:a:0",
                       "-map", "1:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                       "-c:s", "mov_text", "-metadata:s:s:0", "language=eng", "-t", str(total_duration),
                       "-movflags", "+faststart", str(temporary_output)]
            _run(command, cwd=work, timeout=remaining_timeout(max(120, total_duration*5)))
        if not temporary_output.is_file() or temporary_output.stat().st_size < 1024:
            raise ProviderError("Rendering did not produce a playable video.")
        metadata = _probe(temporary_output, ffmpeg_path)
        actual_duration = total_duration
        if metadata:
            streams = metadata.get("streams", [])
            if not any(stream.get("codec_type") == "video" for stream in streams) or not any(stream.get("codec_type") == "audio" for stream in streams):
                raise ProviderError("Rendered output is missing its video or audio stream.")
            try:
                actual_duration = float(metadata["format"]["duration"])
            except (ValueError, TypeError, KeyError) as exc:
                raise ProviderError("Rendered output has invalid timing.") from exc
            if abs(actual_duration-total_duration) > max(0.35, len(scenes)/30+0.1):
                raise ProviderError("Rendered output duration does not match the scene plan.")
        temporary_output.replace(output_path)
        return {"path": str(output_path), "duration_seconds": round(actual_duration, 3),
                "size_bytes": output_path.stat().st_size, "has_speech": validation["has_speech"],
                "captions_burned": True, "width": width, "height": height}
    finally:
        temporary_output.unlink(missing_ok=True)
