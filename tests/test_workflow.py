import asyncio
import time

from fastapi.testclient import TestClient
import pytest

from app.models import JobRequest
from app.store import Store


async def run_pending(app, owner="test-worker"):
    job = app.state.store.claim(owner)
    assert job is not None
    await app.state.pipeline.run(job["id"], owner)
    return app.state.store.get(job["id"])


@pytest.mark.asyncio
async def test_review_gates_and_export(environment):
    app, provider, settings = environment
    with TestClient(app) as client:
        job = client.post("/api/jobs", json={"brief": "Introduce a creative workflow", "duration_seconds": 12}).json()
        job_id = job["id"]
        plan = await run_pending(app)
        assert plan["status"] == "awaiting_review"
        assert plan["review_checkpoint"] == "plan"
        assert "scene-01" not in provider.calls
        scenes = plan["scenes"]
        scenes[0]["narration"] = "Create a thoughtful story."
        approved = client.post(f"/api/jobs/{job_id}/approve", json={"scenes": scenes, "notes": "Edited opening"})
        assert approved.status_code == 200
        media = await run_pending(app)
        assert media["review_checkpoint"] == "media"
        assert "compose" not in media["_state"]["done"]
        assert not (settings.storage_dir / job_id / "final.mp4").exists()
        assert client.post(f"/api/jobs/{job_id}/approve", json={"script": "Unexpected edit"}).status_code == 422
        assert client.post(f"/api/jobs/{job_id}/approve", json={}).status_code == 200
        complete = await run_pending(app)
        assert complete["status"] == "completed"
        assert complete["progress"] == 100
        assert complete["script"].startswith("Create a thoughtful story.")
        assert client.get(complete["output_url"]).status_code == 200
        artifacts = client.get(f"/api/jobs/{job_id}/artifacts").json()["artifacts"]
        assert {"final.mp4", "manifest.json", "subtitles.srt"}.issubset({a["name"] for a in artifacts})
        assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409
        assert len(client.get(f"/api/jobs/{job_id}/events").json()["events"]) > 15


@pytest.mark.asyncio
async def test_transient_errors_retry_and_reuse_successful_assets(environment):
    app, provider, _ = environment
    provider.fail_concept_once = True
    provider.fail_visual_once = True
    job = app.state.store.create(JobRequest(brief="Explain an asynchronous creative workflow", duration_seconds=12, review_required=False))
    result = await run_pending(app)
    assert result["status"] == "completed"
    assert provider.calls["concept"] == 2
    assert provider.calls["scene-01"] == 1
    assert provider.calls["scene-02"] == 2
    assert result["_state"]["attempts"]["visuals"] == 2


@pytest.mark.asyncio
async def test_cancellation_cannot_be_overwritten_by_provider_completion(environment):
    app, provider, _ = environment
    entered, resume = asyncio.Event(), asyncio.Event()
    async def slow_concept(*args):
        entered.set()
        await resume.wait()
        return {"concept": "Late result"}
    provider.generate_concept = slow_concept
    app.state.store.create(JobRequest(brief="Explain an asynchronous creative workflow", duration_seconds=12))
    job = app.state.store.claim("slow")
    task = asyncio.create_task(app.state.pipeline.run(job["id"], "slow"))
    await entered.wait()
    with TestClient(app) as client:
        assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
    resume.set()
    await task
    cancelled = app.state.store.get(job["id"])
    assert cancelled["status"] == "cancelled"
    assert "concept" not in cancelled


def test_worker_leases_prevent_double_claim_and_recover_expired_jobs(environment):
    app, _, settings = environment
    store = app.state.store
    job = store.create(JobRequest(brief="Introduce a creative workflow", duration_seconds=12))
    assert store.claim("worker-a")["id"] == job["id"]
    assert store.claim("worker-b") is None
    with store.connection() as conn:
        conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job["id"]))
    assert not store.renew(job["id"], "worker-a")
    assert store.claim("worker-b")["id"] == job["id"]
    recovered = Store(settings.database_path).get(job["id"])
    assert recovered["status"] == "running"
    assert any("recovered" in event["message"] for event in store.events(job["id"]))


@pytest.mark.asyncio
async def test_corrupt_media_is_repaired_with_a_new_review(environment):
    app, provider, settings = environment
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", json={"brief": "Introduce a creative workflow", "duration_seconds": 12}).json()["id"]
        await run_pending(app)
        client.post(f"/api/jobs/{job_id}/approve", json={})
        await run_pending(app)
        from pathlib import Path
        Path(app.state.store.get(job_id)["_state"]["visual_assets"]["scene-01"]["path"]).write_bytes(b"corrupt")
        client.post(f"/api/jobs/{job_id}/approve", json={})
        repaired = await run_pending(app)
        assert repaired["status"] == "awaiting_review"
        assert repaired["review_checkpoint"] == "media"
        assert provider.calls["scene-01"] == 2
        assert provider.calls["scene-02"] == 1
        assert repaired["_state"]["repair_count"] == 1


@pytest.mark.asyncio
async def test_supervisor_cannot_bypass_safe_candidates(environment):
    app, provider, _ = environment
    async def unsafe(*args):
        return {"tool": "compose_video", "reason": "Skip the review"}
    provider.choose_action = unsafe
    app.state.store.create(JobRequest(brief="Introduce a creative workflow", duration_seconds=12))
    job = await run_pending(app)
    assert job["review_checkpoint"] == "plan"
    assert job["_state"]["done"] == ["concept", "script", "scenes"]
    assert "scene-01" not in provider.calls


@pytest.mark.asyncio
async def test_immediate_retry_cannot_publish_stale_script(environment):
    app, provider, settings = environment
    entered, resume = asyncio.Event(), asyncio.Event()
    calls = 0
    async def script(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await resume.wait()
            return "Stale script from the cancelled worker."
        return "Create a clear story. Bring the idea to life."
    provider.generate_script = script
    created = app.state.store.create(JobRequest(brief="Introduce a creative workflow", duration_seconds=12))
    app.state.store.claim("old-owner")
    old = asyncio.create_task(app.state.pipeline.run(created["id"], "old-owner"))
    await entered.wait()
    with TestClient(app) as client:
        client.post(f"/api/jobs/{created['id']}/cancel")
        client.post(f"/api/jobs/{created['id']}/retry")
    newer = await run_pending(app, "new-owner")
    resume.set()
    await old
    assert newer["review_checkpoint"] == "plan"
    current = app.state.store.get(created["id"])
    assert current["script"] == "Create a clear story. Bring the idea to life."
    assert (settings.storage_dir / created["id"] / "script.txt").read_text() == current["script"]
