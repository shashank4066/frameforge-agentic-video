import io
import wave

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.models import JobRequest


async def media_review(app):
    job = app.state.store.create(JobRequest(brief="A warm cinematic coffee product promo", provider_mode="free", duration_seconds=12))
    for gate in ["plan", "media"]:
        owner = f"upload-test-{gate}"
        assert app.state.store.claim(owner)["id"] == job["id"]
        await app.state.pipeline.run(job["id"], owner)
        current = app.state.store.get(job["id"])
        assert current["review_checkpoint"] == gate
        if gate == "plan":
            with TestClient(app) as client:
                assert client.post(f"/api/jobs/{job['id']}/approve", json={}).status_code == 200
    return job["id"]


def image_file():
    output = io.BytesIO()
    Image.new("RGB", (80, 64), "blue").save(output, format="PNG")
    return output.getvalue()


def audio_file(seconds=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * int(8000 * seconds))
    return output.getvalue()


@pytest.mark.asyncio
async def test_visual_upload_replaces_only_registered_scene_asset(environment):
    app, _, settings = environment
    job_id = await media_review(app)
    previous = app.state.store.get(job_id)["_state"]["visual_assets"]["scene-01"]["path"]
    with TestClient(app) as client:
        response = client.post(f"/api/jobs/{job_id}/media", data={"kind": "visual", "scene_id": "scene-01"},
                               files={"file": ("../../.env", image_file(), "image/png")})
        assert response.status_code == 200
        assets = client.get(f"/api/jobs/{job_id}/artifacts").json()["artifacts"]
        visual = next(asset for asset in assets if asset.get("role") == "visual" and asset.get("scene_id") == "scene-01")
        assert visual["provider"] == "uploaded-media" and visual["draft"] is False
        assert visual["name"].startswith("images/upload-") and visual["name"].endswith(".png")
        assert client.get(visual["url"]).status_code == 200
        assert client.get(f"/api/jobs/{job_id}/artifacts/images/{previous.split('/')[-1]}").status_code == 404
        assert not (settings.storage_dir / ".env").exists()


@pytest.mark.asyncio
async def test_corrupt_upload_and_wrong_scene_leave_media_unchanged(environment):
    app, _, _ = environment
    job_id = await media_review(app)
    before = app.state.store.get(job_id)["_state"]["visual_assets"]
    with TestClient(app) as client:
        for data, body in [({"kind": "visual", "scene_id": "scene-01"}, b"#EXTM3U\nhttps://example.com/clip"),
                           ({"kind": "visual", "scene_id": "no-scene"}, image_file())]:
            result = client.post(f"/api/jobs/{job_id}/media", data=data, files={"file": ("bad.mp4", body)})
            assert result.status_code == 422
    assert app.state.store.get(job_id)["_state"]["visual_assets"] == before


@pytest.mark.asyncio
async def test_voice_upload_and_music_removal_are_reviewable(environment):
    app, _, _ = environment
    job_id = await media_review(app)
    with TestClient(app) as client:
        voice = client.post(f"/api/jobs/{job_id}/media", data={"kind": "voice", "scene_id": "scene-01"},
                            files={"file": ("narration.wav", audio_file(8), "audio/wav")})
        assert voice.status_code == 200
        music = client.post(f"/api/jobs/{job_id}/media", data={"kind": "music"}, files={"file": ("score.wav", audio_file(2), "audio/wav")})
        assert music.status_code == 200
        items = client.get(f"/api/jobs/{job_id}/artifacts").json()["artifacts"]
        track = next(item for item in items if item.get("role") == "music")
        assert client.get(track["url"]).status_code == 200
        assert client.post(f"/api/jobs/{job_id}/music/remove").status_code == 200
        assert client.get(track["url"]).status_code == 404
        approved = client.post(f"/api/jobs/{job_id}/approve", json={})
        assert approved.status_code == 200
        assert approved.json()["scenes"][0]["duration_seconds"] >= 8.3
        assert approved.json()["duration_seconds"] == 12
        denied = client.post(f"/api/jobs/{job_id}/media", data={"kind": "visual", "scene_id": "scene-01"}, files={"file": ("photo.png", image_file())})
        assert denied.status_code == 409


@pytest.mark.asyncio
async def test_overlong_narration_cannot_resume_export(environment):
    app, _, _ = environment
    job_id = await media_review(app)
    with TestClient(app) as client:
        for scene_id in ["scene-01", "scene-02"]:
            response = client.post(f"/api/jobs/{job_id}/media", data={"kind": "voice", "scene_id": scene_id}, files={"file": ("long.wav", audio_file(31))})
            assert response.status_code == 200
        result = client.post(f"/api/jobs/{job_id}/approve", json={})
        assert result.status_code == 422 and "60" in result.json()["detail"]
    assert app.state.store.get(job_id)["status"] == "awaiting_review"


def test_prepared_coffee_sample_works_without_provider_keys(environment):
    app, _, _ = environment
    with TestClient(app) as client:
        config = client.get("/api/config").json()
        assert config["uploads_available"] and config["sample_available"]
        assert config["gemini_ready"] is False and config["pexels_ready"] is False
        sample = client.post("/api/samples/coffee", json={})
        assert sample.status_code == 201
        job = sample.json()
        assert job["prepared_sample"] and job["review_checkpoint"] == "media" and job["duration_seconds"] == 16
        assets = client.get(f"/api/jobs/{job['id']}/artifacts").json()["artifacts"]
        assert len([item for item in assets if item.get("role") == "visual"]) == 3
        assert len([item for item in assets if item.get("role") == "voice"]) == 3
        assert all(item["provider"] == "prepared-gemini-narration" for item in assets if item.get("role") == "voice")
        assert len([item for item in assets if item.get("role") == "music"]) == 1
        assert client.post(f"/api/jobs/{job['id']}/approve", json={}).status_code == 200


@pytest.mark.asyncio
async def test_free_drafts_cannot_be_exported_through_api(environment):
    app, _, _ = environment
    job_id = await media_review(app)
    app.state.store.mutate(job_id, lambda job: job["_state"]["visual_assets"]["scene-01"].update(draft=True))
    with TestClient(app) as client:
        result = client.post(f"/api/jobs/{job_id}/approve", json={})
        assert result.status_code == 422 and "Replace the draft visuals" in result.json()["detail"]
    assert app.state.store.get(job_id)["status"] == "awaiting_review"
