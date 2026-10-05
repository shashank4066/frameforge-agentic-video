from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.queue import Coordinator

BRIEF = {"brief": "Introduce a creative workflow", "duration_seconds": 12}


def test_visitors_only_see_and_control_their_own_jobs(environment):
    app, _, _ = environment
    with TestClient(app) as alice, TestClient(app) as bob:
        job = alice.post("/api/jobs", json=BRIEF).json()
        assert alice.cookies.get("ff_visitor")
        assert [j["id"] for j in alice.get("/api/jobs").json()["jobs"]] == [job["id"]]
        assert bob.get("/api/jobs").json() == {"jobs": []}
        for path in ["", "/events", "/artifacts"]:
            assert bob.get(f"/api/jobs/{job['id']}{path}").status_code == 404
        assert bob.post(f"/api/jobs/{job['id']}/cancel").status_code == 404
        assert alice.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
        assert "_owner" not in alice.get(f"/api/jobs/{job['id']}").json()


def test_active_job_quotas(environment):
    app, _, settings = environment
    settings.max_active_jobs_per_visitor, settings.max_queued_jobs = 2, 3
    with TestClient(app) as alice, TestClient(app) as bob:
        assert [alice.post("/api/jobs", json=BRIEF).status_code for _ in range(3)] == [201, 201, 429]
        assert bob.post("/api/jobs", json=BRIEF).status_code == 201
        busy = bob.post("/api/jobs", json=BRIEF)
        assert busy.status_code == 429 and "busy" in busy.json()["detail"]
        # Cancelled jobs stop counting, but retrying one is quota-checked again.
        first = alice.get("/api/jobs").json()["jobs"][0]["id"]
        alice.post(f"/api/jobs/{first}/cancel")
        assert alice.post("/api/jobs", json=BRIEF).status_code == 201
        assert alice.post(f"/api/jobs/{first}/retry").status_code == 429


def test_retention_purges_old_jobs_and_files(environment):
    app, _, settings = environment
    store = app.state.store
    with TestClient(app) as client:
        old, fresh = (client.post("/api/jobs", json=BRIEF).json()["id"] for _ in range(2))
    for job_id in (old, fresh):
        (settings.storage_dir / job_id).mkdir(parents=True, exist_ok=True)
    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    with store.connection() as conn:
        conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (stale, old))
    settings.job_retention_hours = 24
    Coordinator(store, None, settings, None).purge_expired()
    assert store.get(old) is None and store.events(old) == []
    assert not (settings.storage_dir / old).exists()
    assert store.get(fresh) is not None and (settings.storage_dir / fresh).exists()
