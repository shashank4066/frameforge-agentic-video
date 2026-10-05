from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from . import media
from .models import ApprovalRequest, JobRequest, validate_scenes
from .pipeline import Pipeline
from .providers import create_provider
from .queue import Coordinator, QueueSignal
from .store import Conflict, Store, public_job

ROOT = Path(__file__).resolve().parent.parent


def create_app(settings=None, provider_factory=create_provider):
    settings = settings or Settings.from_env()
    store = Store(settings.database_path)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    signal = QueueSignal(settings)
    pipeline = Pipeline(store, settings, provider_factory)
    coordinator = Coordinator(store, pipeline, settings, signal)

    @asynccontextmanager
    async def lifespan(app):
        if settings.start_worker and settings.queue_backend == "local":
            await coordinator.start()
        yield
        await coordinator.stop()
        await signal.close()

    app = FastAPI(title="FrameForge - Agentic AI Video Pipeline", version="1.0.0", lifespan=lifespan,
                  description="Persistent, reviewable media generation with agent tool selection, retries, and FFmpeg exports.")
    app.state.store, app.state.pipeline, app.state.settings = store, pipeline, settings

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    def get_job(job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise HTTPException(404, "Job not found")
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "mode": settings.queue_backend,
                "ffmpeg_available": media.ffmpeg_available(settings.ffmpeg_path), "live_ready": settings.live_ready}

    @app.get("/api/config")
    async def configuration():
        return {"demo_available": True, "live_ready": settings.live_ready,
                "llm_configured": settings.live_ready, "image_configured": settings.live_ready,
                "tts_configured": settings.live_ready, "video_configured": bool(settings.video_api_url),
                "queue_backend": settings.queue_backend,
                "ffmpeg_available": media.ffmpeg_available(settings.ffmpeg_path)}

    @app.get("/api/jobs")
    async def list_jobs():
        return {"jobs": [public_job(job) for job in store.list()]}

    @app.post("/api/jobs", status_code=201)
    async def create_job(request: JobRequest):
        if request.provider_mode == "live" and not settings.live_ready:
            raise HTTPException(422, "Live generation needs OPENAI_API_KEY in your .env file. Demo works without a key.")
        if not media.ffmpeg_available(settings.ffmpeg_path):
            raise HTTPException(503, "FFmpeg is missing. Install FFmpeg and set FFMPEG_PATH before generating videos.")
        job = store.create(request)
        await signal.notify(job["id"])
        return public_job(job)

    @app.get("/api/jobs/{job_id}")
    async def job_details(job_id: str):
        return public_job(get_job(job_id))

    @app.post("/api/jobs/{job_id}/approve")
    async def approve(job_id: str, request: ApprovalRequest):
        get_job(job_id)
        checkpoint = None
        def apply(job):
            nonlocal checkpoint
            if job["status"] != "awaiting_review":
                raise Conflict("This job is not awaiting review")
            checkpoint = job["review_checkpoint"]
            if checkpoint == "plan":
                if request.script is not None and request.script != job["script"] and request.scenes is None:
                    raise HTTPException(422, "When changing the script, submit the matching scene narrations too.")
                if request.scenes is not None:
                    try:
                        job["scenes"] = validate_scenes([scene.model_dump() for scene in request.scenes], job["duration_seconds"])
                    except ValueError as exc:
                        raise HTTPException(422, str(exc)) from exc
                    joined = " ".join(scene["narration"] for scene in job["scenes"])
                    if request.script is not None and " ".join(request.script.split()) != " ".join(joined.split()):
                        raise HTTPException(422, "The script must match the scene narrations, which are used for voice and captions.")
                    job["script"] = request.script or " ".join(scene["narration"] for scene in job["scenes"])
                if not job["script"].strip():
                    raise HTTPException(422, "Script cannot be blank")
                directory = settings.storage_dir.resolve() / job_id
                (directory / "script.txt").write_text(job["script"], encoding="utf-8")
                (directory / "scenes.json").write_text(json.dumps(job["scenes"], indent=2), encoding="utf-8")
                job["_state"]["plan_approved"] = True
            elif checkpoint == "media":
                if request.script is not None or request.scenes is not None:
                    raise HTTPException(422, "Media approval does not accept script or scene edits. Create a revised job for new content.")
                job["_state"]["media_approved"] = True
            else:
                raise Conflict("Unknown review checkpoint")
            if request.notes:
                job.setdefault("review_notes", []).append({"checkpoint": checkpoint, "notes": request.notes})
            job.update(status="queued", review_checkpoint=None)
        job = store.mutate(job_id, apply)
        store.event(job_id, "review", "info", f"Human approved {checkpoint}. Pipeline resumed.")
        await signal.notify(job_id)
        return public_job(job)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: str):
        get_job(job_id)
        def apply(job):
            if job["status"] not in {"queued", "running", "awaiting_review"}:
                raise Conflict("Only pending or running jobs can be cancelled")
            job.update(status="cancelled", review_checkpoint=None)
        job = store.mutate(job_id, apply)
        store.event(job_id, "queue", "warning", "Job cancelled. In-flight provider calls may finish; further stages are stopped.")
        return public_job(job)

    @app.post("/api/jobs/{job_id}/retry")
    async def retry(job_id: str):
        get_job(job_id)
        def apply(job):
            if job["status"] not in {"failed", "cancelled"}:
                raise Conflict("Retry is available for failed or cancelled jobs")
            job.update(status="queued", error=None, review_checkpoint=None)
        job = store.mutate(job_id, apply)
        store.event(job_id, "queue", "info", "Job requeued. Valid persisted stages and assets will be reused.")
        await signal.notify(job_id)
        return public_job(job)

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str):
        get_job(job_id)
        return {"events": store.events(job_id)}

    def artifact_paths(job):
        directory = settings.storage_dir.resolve() / job["id"]
        allowed = []
        if directory.exists():
            for path in directory.rglob("*"):
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(directory):
                    continue
                relative = path.relative_to(directory).as_posix()
                if relative in {"script.txt", "scenes.json", "subtitles.srt"}:
                    allowed.append(path)
                elif relative in {"final.mp4", "manifest.json"} and job["status"] == "completed":
                    allowed.append(path)
                elif relative.startswith(("images/", "audio/")) and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".wav", ".mp3", ".m4a"}:
                    # Only expose provider outputs committed to workflow state.
                    registered = [Path(asset["path"]).resolve() for key in ["visual_assets", "voice_assets"] for asset in job["_state"][key].values()]
                    if path.resolve() in registered:
                        allowed.append(path)
        return allowed

    @app.get("/api/jobs/{job_id}/artifacts")
    async def artifacts(job_id: str):
        job = get_job(job_id)
        directory = settings.storage_dir.resolve() / job_id
        entries = []
        for path in artifact_paths(job):
            name = path.relative_to(directory).as_posix()
            suffix = path.suffix.lower()
            kind = "image" if suffix in {".png", ".jpg", ".jpeg", ".webp"} else "audio" if suffix in {".wav", ".mp3", ".m4a"} else "video" if suffix == ".mp4" else "subtitle" if suffix == ".srt" else "document"
            entries.append({"name": name, "kind": kind, "url": f"/api/jobs/{job_id}/artifacts/{quote(name, safe='/')}",
                            "size_bytes": path.stat().st_size})
        return {"artifacts": entries}

    @app.get("/api/jobs/{job_id}/artifacts/{name:path}")
    async def artifact(job_id: str, name: str):
        job = get_job(job_id)
        directory = settings.storage_dir.resolve() / job_id
        candidate = (directory / name).resolve()
        if not candidate.is_relative_to(directory) or candidate not in [path.resolve() for path in artifact_paths(job)]:
            raise HTTPException(404, "Artifact not found")
        return FileResponse(candidate, filename=candidate.name,
                            content_disposition_type="inline" if candidate.suffix in {".mp4", ".wav", ".mp3", ".png", ".jpg"} else "attachment")

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics():
        return PlainTextResponse(store.metrics(), media_type="text/plain; version=0.0.4")

    if (ROOT / "web").exists():
        app.mount("/web", StaticFiles(directory=ROOT / "web"), name="web")

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(ROOT / "web" / "index.html")

    return app


app = create_app()
