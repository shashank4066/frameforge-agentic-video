from pathlib import Path
import wave

import pytest
from PIL import Image

from app.config import Settings
from app.main import create_app


class FakeProvider:
    def __init__(self):
        self.calls = {}
        self.fail_concept_once = False
        self.fail_visual_once = False

    def count(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1
        return self.calls[name]

    async def choose_action(self, state, allowed_tools):
        return {"tool": allowed_tools[0], "reason": "Test dependency selection"}

    async def generate_concept(self, brief, style):
        count = self.count("concept")
        if self.fail_concept_once and count == 1:
            return {}
        return {"concept": "An explainer about creative workflows", "title": "Story"}

    async def generate_script(self, brief, concept, duration):
        self.count("script")
        return "Create a clear story. Bring the idea to life."

    async def plan_scenes(self, script, duration, aspect, style):
        self.count("scenes")
        return [{"id": "scene-01", "narration": "Create a clear story.", "visual_prompt": "A calm blue geometric landscape", "duration_seconds": duration / 2},
                {"id": "scene-02", "narration": "Bring the idea to life.", "visual_prompt": "A bright green geometric landscape", "duration_seconds": duration / 2}]

    async def generate_visual(self, scene, destination, aspect):
        count = self.count(scene["id"])
        if self.fail_visual_once and scene["id"] == "scene-02" and count == 1:
            raise TimeoutError("Transient image provider timeout")
        Image.new("RGB", (64, 64), (40, 80, 140)).save(destination)
        return {"path": str(destination), "kind": "image", "provider": "test"}

    async def generate_voice(self, text, destination):
        self.count("voice")
        with wave.open(str(destination), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\x00\x00" * 8000)
        return {"path": str(destination), "has_speech": False, "provider": "test"}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    provider = FakeProvider()
    settings = Settings(database_path=tmp_path / "jobs.db", storage_dir=tmp_path / "artifacts", start_worker=False, max_stage_attempts=2)
    monkeypatch.setattr("app.media.ffmpeg_available", lambda *args: True)
    def compose(**kwargs):
        path = kwargs["output_path"]
        path.write_bytes(b"test-video" * 200)
        return {"path": str(path), "duration_seconds": 12, "size_bytes": path.stat().st_size, "has_speech": False}
    monkeypatch.setattr("app.media.compose_video", compose)
    app = create_app(settings, lambda mode, config: provider)
    return app, provider, settings
