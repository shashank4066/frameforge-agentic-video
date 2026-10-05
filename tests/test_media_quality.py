"""Checks for natural narration pacing, caption estimates, and free finishing."""

import array
import math
from pathlib import Path
import shutil
import subprocess
import wave

from PIL import Image
import pytest

from app import media
from app.providers import ProviderError


def recording(path: Path, seconds: float, *, tone: bool = True) -> Path:
    samples = array.array("h", (round(9000 * math.sin(2 * math.pi * 440 * frame / 16000)) if tone else 0
                                for frame in range(round(seconds * 16000))))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.tobytes())
    return path


def scene(name: str, seconds: float) -> dict:
    return {"id": name, "duration_seconds": seconds,
            "narration": "One two three four five six seven eight nine ten.", "visual_prompt": "A bright scene."}


def test_timing_reallocates_pauses_and_preserves_the_requested_duration(tmp_path):
    scenes = [scene("one", 4), scene("two", 4)]
    voices = [{"path": str(recording(tmp_path / "one.wav", 5)), "has_speech": True},
              {"path": str(recording(tmp_path / "two.wav", 1)), "has_speech": True}]
    fitted = media.fit_scene_timing(scenes, voices, 8)
    assert sum(item["duration_seconds"] for item in fitted) == pytest.approx(8)
    assert fitted[0]["duration_seconds"] >= 5.3
    assert fitted[1]["duration_seconds"] >= 1.3
    assert scenes[0]["duration_seconds"] == 4


def test_timing_expands_for_natural_speech_and_rejects_overlong_narration(tmp_path):
    voices = [{"path": str(recording(tmp_path / "long.wav", 3)), "has_speech": True}]
    fitted = media.fit_scene_timing([scene("one", 2)], voices, 2)
    assert fitted[0]["duration_seconds"] == pytest.approx(3.3)
    voices[0]["path"] = str(recording(tmp_path / "too-long.wav", 60))
    with pytest.raises(ProviderError, match="Shorten the script"):
        media.fit_scene_timing([scene("one", 60)], voices, 60)


def test_silent_placeholder_does_not_expand_video(tmp_path):
    voice = {"path": str(recording(tmp_path / "silence.wav", 10, tone=False)), "has_speech": False}
    fitted = media.fit_scene_timing([scene("one", 2)], [voice], 2)
    assert fitted[0]["duration_seconds"] == 2


def test_caption_estimates_stop_at_recorded_speech_and_keep_scene_offsets(tmp_path):
    voices = [{"path": str(recording(tmp_path / "one.wav", 1)), "has_speech": True},
              {"path": str(recording(tmp_path / "two.wav", 2)), "has_speech": True}]
    text = media.build_subtitles([scene("one", 4), scene("two", 4)], tmp_path / "captions.srt", voices).read_text()
    assert "00:00:00,000 --> 00:00:00,800" in text
    assert "00:00:00,800 --> 00:00:01,000" in text
    assert "00:00:04,000 --> 00:00:05,600" in text
    assert "00:00:05,600 --> 00:00:06,000" in text
    with pytest.raises(ProviderError, match="one narration"):
        media.build_subtitles([scene("one", 4)], tmp_path / "bad.srt", voices)


def test_caption_phrases_break_at_punctuation_and_balance_orphan_lines(tmp_path):
    coffee = {**scene("coffee", 4.4),
              "narration": "Before the day begins, take a moment that belongs to you."}
    long_phrase = {**scene("closing", 4), "narration": "Take another quiet moment and make it yours."}
    cues = media.build_subtitles([coffee, long_phrase], tmp_path / "phrases.srt").read_text().strip().split("\n\n")
    assert len(cues) == 3
    assert cues[0].splitlines() == ["1", "00:00:00,000 --> 00:00:01,600", "Before the day begins,"]
    assert cues[1].splitlines() == ["2", "00:00:01,600 --> 00:00:04,400", "take a moment that belongs to you."]
    closing_lines = cues[2].splitlines()[2:]
    assert len(closing_lines) == 2
    assert all(10 <= len(line) <= 38 for line in closing_lines)
    assert " ".join(closing_lines) == long_phrase["narration"]


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg tools unavailable")
def test_real_render_fades_loops_ducked_music_and_preserves_narration(tmp_path, monkeypatch):
    # Small frames keep this a real integration test without exercising 720p CPU
    # throughput. Production keeps its existing portrait/landscape resolutions.
    monkeypatch.setattr(media, "ASPECT_SIZES", {"16:9": (160, 90)})
    image = tmp_path / "visual.png"
    Image.new("RGB", (240, 160), (240, 80, 40)).save(image)
    voice = {"path": str(recording(tmp_path / "voice.wav", 1.2)), "has_speech": True}
    scenes = media.fit_scene_timing([scene("one", 1), scene("two", 1)], [voice, voice], 2)
    assert sum(item["duration_seconds"] for item in scenes) == pytest.approx(3)
    subtitles = media.build_subtitles(scenes, tmp_path / "captions.srt", [voice, voice])
    commands = []
    original_run = media._run
    def tracked(command, **kwargs):
        commands.append(command)
        return original_run(command, **kwargs)
    monkeypatch.setattr(media, "_run", tracked)
    result = media.compose_video(scenes, [{"path": str(image), "kind": "image"}] * 2,
                                 [voice, voice], subtitles, tmp_path / "final.mp4", "16:9",
                                 music_asset={"path": str(recording(tmp_path / "music.wav", 0.4))})
    assert result["duration_seconds"] == pytest.approx(3, abs=0.1)
    assert result["music_used"] is True
    assert result["narration_pacing"] == "natural"
    rendered_commands = " ".join(" ".join(command) for command in commands)
    assert "atempo" not in rendered_commands
    assert "sidechaincompress" in rendered_commands
    assert "-stream_loop -1" in rendered_commands
    assert "fade=t=in" in rendered_commands
    metadata = media._probe(Path(result["path"]), "ffmpeg")
    assert {item["codec_type"] for item in metadata["streams"]} >= {"audio", "video", "subtitle"}
    def corner(timestamp):
        command = [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-ss", str(timestamp),
                   "-i", result["path"], "-frames:v", "1", "-vf", "crop=16:16:0:0,scale=1:1",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        return subprocess.run(command, check=True, capture_output=True).stdout
    opening, visible = corner(0), corner(0.4)
    assert len(opening) == len(visible) == 3
    assert sum(opening) < sum(visible) / 4
    decoded = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-i", result["path"],
                              "-f", "null", "-"], capture_output=True, check=True)
    assert not decoded.stderr
    with pytest.raises(ProviderError, match="Fit scene timing"):
        media.compose_video([scene("one", 0.6)], [{"path": str(image), "kind": "image"}],
                            [voice], subtitles, tmp_path / "too-short.mp4", "16:9")
