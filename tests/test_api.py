from fastapi.testclient import TestClient
import pytest

from app.models import JobRequest
from app.config import Settings
from app.main import create_app
from test_workflow import run_pending


def test_input_errors_and_unconfigured_live_mode(environment):
    app, _, _ = environment
    with TestClient(app) as client:
        assert client.get("/api/health").json()["ffmpeg_available"]
        for changes in [{"brief": " " * 20}, {"duration_seconds": 100}, {"aspect_ratio": "3:2"}, {"provider_mode": "live"}, {"unexpected": True}]:
            body = {"brief": "Introduce a creative workflow", **changes}
            assert client.post("/api/jobs", json=body).status_code == 422
        assert client.get("/api/jobs/not-a-job").status_code == 404
        assert client.get("/api/jobs").json() == {"jobs": []}
        assert "frameforge_jobs" in client.get("/metrics").text


def test_requests_reuse_startup_media_readiness(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("app.media.ffmpeg_available", lambda path: calls.append(path) or True)
    app = create_app(Settings(database_path=tmp_path / "jobs.db", storage_dir=tmp_path / "media", start_worker=False))
    assert len(calls) == 1
    # A busy or blocked subprocess after startup cannot delay platform pings.
    def blocked_probe(*args):
        raise AssertionError("Request handler launched a media readiness probe")
    monkeypatch.setattr("app.media.ffmpeg_available", blocked_probe)
    with TestClient(app) as client:
        for _ in range(3):
            assert client.get("/api/health").json()["ffmpeg_available"]
            assert client.get("/api/config").json()["ffmpeg_available"]
        assert client.post("/api/jobs", json={"brief": "A warm cinematic coffee promo"}).status_code == 201


@pytest.mark.asyncio
async def test_plan_approval_rejects_invalid_timing_duplicates_and_stale_script(environment):
    app, _, _ = environment
    with TestClient(app) as client:
        job = client.post("/api/jobs", json={"brief": "Introduce a creative workflow", "duration_seconds": 12}).json()
        job = await run_pending(app)
        endpoint = f"/api/jobs/{job['id']}/approve"
        assert client.post(endpoint, json={"script": "Changed story"}).status_code == 422
        scenes = job["scenes"]
        assert client.post(endpoint, json={"script": "Different words", "scenes": scenes}).status_code == 422
        scenes[0]["duration_seconds"] = 59
        assert client.post(endpoint, json={"scenes": scenes}).status_code == 422
        scenes[0]["duration_seconds"] = 6
        scenes[1]["id"] = scenes[0]["id"]
        assert client.post(endpoint, json={"scenes": scenes}).status_code == 422
        assert app.state.store.get(job["id"])["status"] == "awaiting_review"


def test_artifact_paths_cannot_escape_job_directory(environment):
    app, _, settings = environment
    job = app.state.store.create(JobRequest(brief="Introduce a creative workflow"))
    directory = settings.storage_dir / job["id"]
    directory.mkdir()
    (directory / "script.txt").write_text("Public script")
    (directory / "private.env").write_text("Do not expose")
    (settings.storage_dir / "private.txt").write_text("Do not expose")
    with TestClient(app) as client:
        prefix = f"/api/jobs/{job['id']}/artifacts"
        assert [a["name"] for a in client.get(prefix).json()["artifacts"]] == ["script.txt"]
        assert client.get(prefix + "/private.env").status_code == 404
        assert client.get(prefix + "/%2E%2E/private.txt").status_code == 404
        assert "_state" not in client.get(f"/api/jobs/{job['id']}").json()
