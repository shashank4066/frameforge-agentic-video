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


def fit_scene_timing(scenes: list[dict[str, Any]], voice_assets: list[dict[str, Any]],
                     target_duration: float, ffmpeg_path: str = "ffmpeg") -> list[dict[str, Any]]:
    """Keep narration at its recorded pace and use remaining time for visual pauses.

    This allocates scene time rather than stretching audio. A long recording can
    expand the requested video, but the portfolio renderer remains bounded at 60s.
    """
    if not scenes or len(scenes) != len(voice_assets):
        raise ProviderError("Every scene needs a narration asset before its timing can be fitted.")
    if not math.isfinite(float(target_duration)) or not 1 <= float(target_duration) <= 60:
        raise ProviderError("Choose a video duration between 1 and 60 seconds.")
    minimum_ms = []
    planned_ms = []
    for scene, voice in zip(scenes, voice_assets, strict=True):
        planned = float(scene.get("duration_seconds", 0))
        if not math.isfinite(planned) or planned <= 0:
            raise ProviderError("Invalid scene timing. Retry scene planning.")
        path = Path(voice.get("path", ""))
        if not path.is_file():
            raise ProviderError(f"Narration is missing for {scene.get('id', 'a scene')}.")
        recorded = _duration(path, ffmpeg_path)
        if recorded is not None and (not math.isfinite(recorded) or recorded <= 0):
            raise ProviderError("A narration recording contains no usable audio.")
        # Silent placeholders do not dictate pacing. Without a measurable speech
        # duration, retain the existing plan instead of guessing word alignment.
        minimum = (recorded + 0.3 if recorded is not None else planned) if voice.get("has_speech", True) else 1.0
        minimum_ms.append(math.ceil(max(1.0, minimum) * 1000))
        planned_ms.append(round(planned * 1000))
    required_ms = sum(minimum_ms)
    if required_ms > 60000:
        raise ProviderError("The narration needs more than 60 seconds at its natural pace. "
                            "Shorten the script or upload a shorter recording, then regenerate narration.")
    total_ms = max(round(float(target_duration) * 1000), required_ms)
    extra_ms = total_ms - required_ms
    weights = [max(0, planned - minimum) for planned, minimum in zip(planned_ms, minimum_ms, strict=True)]
    if not sum(weights):
        weights = [1] * len(scenes)
    weighted = [extra_ms * weight / sum(weights) for weight in weights]
    extras = [math.floor(value) for value in weighted]
    for index in sorted(range(len(scenes)), key=lambda i: weighted[i] - extras[i], reverse=True)[:extra_ms-sum(extras)]:
        extras[index] += 1
    return [{**scene, "duration_seconds": (minimum + extra) / 1000}
            for scene, minimum, extra in zip(scenes, minimum_ms, extras, strict=True)]


def _caption_duration(scene: dict[str, Any], voice: dict[str, Any] | None, ffmpeg_path: str) -> float:
    duration = float(scene["duration_seconds"])
    if voice and voice.get("has_speech", True):
        recorded = _duration(Path(voice["path"]), ffmpeg_path)
        if recorded is not None and math.isfinite(recorded) and recorded > 0:
            return min(duration, recorded)
    return duration


def _caption_groups(words: list[str]) -> list[list[str]]:
    """Keep short phrases intact while retaining the eight-word cue limit."""
    groups = []
    offset = 0
    while offset < len(words):
        count = min(8, len(words) - offset)
        for length in range(3, count + 1):
            # Closing quotation marks should not hide a sentence boundary.
            if words[offset + length - 1].rstrip("\"'”’)]").endswith((",", ".", "!", "?")):
                count = length
                break
        groups.append(words[offset:offset + count])
        offset += count
    return groups or [[" "]]


def _wrap_caption(words: list[str], width: int = 38) -> str:
    lines = textwrap.wrap(" ".join(words), width=width)
    if len(lines) == 2 and len(lines[-1]) < 10:
        candidates = [(" ".join(words[:split]), " ".join(words[split:]))
                      for split in range(1, len(words))]
        candidates = [(first, second) for first, second in candidates
                      if len(first) <= width and len(second) <= width]
        if candidates:
            lines = list(min(candidates, key=lambda pair: abs(len(pair[0]) - len(pair[1]))))
    return "\n".join(lines)


def build_subtitles(scenes: list[dict[str, Any]], destination: Path,
                    voice_assets: list[dict[str, Any]] | None = None,
                    ffmpeg_path: str = "ffmpeg") -> Path:
    """Estimate cue timing over recorded speech; this is not word alignment."""
    if voice_assets is not None and len(voice_assets) != len(scenes):
        raise ProviderError("Caption timing needs one narration asset per scene.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    cues = []
    offset = 0.0
    cue_index = 1
    for index, scene in enumerate(scenes):
        duration = float(scene["duration_seconds"])
        span = _caption_duration(scene, voice_assets[index] if voice_assets else None, ffmpeg_path)
        # Remove SRT/ASS control markup while retaining readable content.
        narration = str(scene["narration"]).replace("\r", " ").replace("\n", " ")
        narration = narration.replace("-->", "→").replace("{", "(").replace("}", ")")
        words = narration.split()
        groups = _caption_groups(words)
        consumed = 0
        for group in groups:
            start = offset + span * consumed / max(1, len(words))
            consumed += len(group)
            end = offset + span * min(consumed, len(words)) / max(1, len(words))
            text = _wrap_caption(group)
            cues.append(f"{cue_index}\n{_timestamp(start)} --> {_timestamp(end)}\n{text}\n")
            cue_index += 1
        offset += duration
    destination.write_text("\n".join(cues), encoding="utf-8")
    return destination


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
                  aspect_ratio: str, ffmpeg_path: str = "ffmpeg", *,
                  music_asset: dict[str, Any] | None = None, transition: str = "fade") -> dict[str, Any]:
    started = time.monotonic()
    validation = validate_assets(scenes, visual_assets, voice_assets, ffmpeg_path)
    if aspect_ratio not in ASPECT_SIZES:
        raise ProviderError("Aspect ratio must be 16:9, 9:16, or 1:1.")
    if transition not in {"fade", "cut"}:
        raise ProviderError("Transition must be fade or cut.")
    music_path = Path(music_asset.get("path", "")) if music_asset else None
    if music_path is not None:
        if not music_path.is_file() or not music_path.stat().st_size:
            raise ProviderError("The background music is missing. Upload it again or remove it.")
        metadata = _probe(music_path, ffmpeg_path)
        if metadata is not None and not any(stream.get("codec_type") == "audio" for stream in metadata.get("streams", [])):
            raise ProviderError("The background music file has no audio stream. Upload an audio recording.")
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
                audio_duration = _duration(Path(voice["path"]), ffmpeg_path)
                if voice.get("has_speech", True) and audio_duration and audio_duration > duration + 0.05:
                    raise ProviderError("A scene is shorter than its narration. Fit scene timing before rendering "
                                        "or shorten the recording; speech will not be sped up.")
                frames = math.ceil(duration * 30)
                render_duration = frames / 30
                segment = work / f"scene-{index:02d}.mp4"
                segments.append(segment)
                command = [resolved_ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                           "-filter_threads", "1", "-filter_complex_threads", "1",
                           "-protocol_whitelist", "file,pipe,crypto"]
                if visual["kind"] == "image":
                    command += ["-loop", "1", "-framerate", "30"]
                else:
                    command += ["-stream_loop", "-1"]
                # Input codec options must precede each -i. Output -threads does
                # not limit decoder pools, which otherwise use host CPU count
                # even when the container has only a small CPU/memory quota.
                command += ["-threads", "1", "-i", str(Path(visual["path"]).resolve()),
                            "-protocol_whitelist", "file,pipe,crypto", "-threads", "1",
                            "-i", str(Path(voice["path"]).resolve())]
                video_filter = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
                if visual["kind"] == "image":
                    progress = f"min(on/{max(1, frames-1)},1)"
                    zoom = f"1.035+0.035*({progress})" if index % 2 == 0 else f"1.07-0.035*({progress})"
                    pan = f"0.2+0.6*({progress})" if index % 3 == 0 else f"0.8-0.6*({progress})"
                    video_filter += (f",zoompan=z='{zoom}':x='(iw-iw/zoom)*({pan})':"
                                     f"y='(ih-ih/zoom)/2':d=1:s={width}x{height}:fps=30")
                else:
                    video_filter += ",fps=30"
                if transition == "fade":
                    fade = min(0.2, duration / 5)
                    video_filter += f",fade=t=in:st=0:d={fade:.6f},fade=t=out:st={duration-fade:.6f}:d={fade:.6f}"
                if has_subtitles_filter:
                    scene_caption = work / f"scene-{index:02d}.srt"
                    build_subtitles([scene], scene_caption, [voice], ffmpeg_path)
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
                    command += ["-loop", "1", "-threads", "1", "-i", str(overlay)]
                    caption_span = _caption_duration(scene, voice, ffmpeg_path)
                    filter_complex = (f"[0:v]{video_filter}[base];[base][2:v]"
                                      f"overlay=0:0:enable='lt(t,{caption_span:.6f})'[v];")
                audio_filter = "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000"
                audio_filter += f",apad,atrim=duration={render_duration:.6f},asetpts=PTS-STARTPTS"
                filter_complex += f"[1:a]{audio_filter}[a]"
                command += ["-filter_complex", filter_complex, "-map", "[v]", "-map", "[a]",
                            "-t", f"{render_duration:.6f}", "-r", "30", "-c:v", "libx264",
                            "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                            "-threads", "2", "-movflags", "+faststart", str(segment)]
                _run(command, cwd=work, timeout=remaining_timeout(max(120, duration*10)))
            concat_path = work / "segments.txt"
            # Filenames are internally generated; no user strings enter this list.
            concat_path.write_text("\n".join(f"file '{segment.name}'" for segment in segments), encoding="utf-8")
            shutil.copyfile(subtitle_path, work / "captions.srt")
            command = [resolved_ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                       "-filter_threads", "1", "-filter_complex_threads", "1", "-f", "concat", "-safe", "1",
                       "-protocol_whitelist", "file,pipe,crypto", "-threads", "1", "-i", str(concat_path),
                       "-protocol_whitelist", "file,pipe,crypto", "-threads", "1", "-i", "captions.srt"]
            if music_path is not None:
                fade_out = max(0, total_duration - 0.7)
                command += ["-stream_loop", "-1", "-protocol_whitelist", "file,pipe,crypto", "-threads", "1",
                            "-i", str(music_path.resolve()),
                            "-filter_complex",
                            "[0:a]asplit=2[voice][side];"
                            "[2:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,volume=0.18,"
                            f"afade=t=in:d=0.5,afade=t=out:st={fade_out:.6f}:d=0.7[music];"
                            "[music][side]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=350[ducked];"
                            "[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[a]",
                            "-map", "0:v:0", "-map", "[a]"]
            else:
                command += ["-map", "0:v:0", "-map", "0:a:0"]
            command += ["-map", "1:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                       "-c:s", "mov_text", "-metadata:s:s:0", "language=eng", "-t", str(total_duration),
                       "-threads", "2", "-movflags", "+faststart", str(temporary_output)]
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
                "captions_burned": True, "width": width, "height": height,
                "transition": transition, "music_used": music_path is not None,
                "narration_pacing": "natural", "caption_timing": "estimated_from_narration"}
    finally:
        temporary_output.unlink(missing_ok=True)
