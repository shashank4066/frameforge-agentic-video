from contextlib import asynccontextmanager
import asyncio
import json
from pathlib import Path
import re
import shutil
import time
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
import httpx

from .config import Settings
from . import media
from .models import ApprovalRequest, JobRequest, validate_scenes
from .pipeline import Pipeline
from .providers import create_provider
from .queue import Coordinator, QueueSignal
from .store import Conflict, Store, public_job
from .uploads import prepare_upload

ROOT = Path(__file__).resolve().parent.parent


def create_app(settings=None, provider_factory=create_provider):
    settings = settings or Settings.from_env()
    store = Store(settings.database_path)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    signal = QueueSignal(settings)
    pipeline = Pipeline(store, settings, provider_factory)
    coordinator = Coordinator(store, pipeline, settings, signal)
    gemini_probe_cache = {}
    gemini_probe_lock = asyncio.Lock()

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
                "pexels_ready": settings.pexels_ready, "gemini_ready": settings.gemini_ready,
                "gemini_model": settings.gemini_model, "gemini_tts_model": settings.gemini_tts_model,
                "uploads_available": True, "sample_available": (ROOT / "app/samples/coffee/dawn.png").is_file(),
                "queue_backend": settings.queue_backend,
                "ffmpeg_available": media.ffmpeg_available(settings.ffmpeg_path)}

    @app.get("/api/jobs")
    async def list_jobs():
        return {"jobs": [public_job(job) for job in store.list()]}

    @app.get("/api/providers/gemini/status")
    async def gemini_status():
        """Check supported model access without exposing keys or generating tokens."""
        if not settings.gemini_ready:
            return {"configured": False, "connected": False, "reason": "key_missing"}
        async with gemini_probe_lock:
            if gemini_probe_cache.get("expires", 0) > time.monotonic():
                return gemini_probe_cache["result"]
            from .gemini import TEXT_MODELS, TTS_MODELS
            result = {"configured": True, "connected": False}
            try:
                async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
                    response = await client.get("https://generativelanguage.googleapis.com/v1beta/models",
                                                headers={"x-goog-api-key": settings.gemini_api_key})
                if response.status_code != 200:
                    result.update(reason="key_or_access_rejected" if response.status_code in {400, 401, 403} else "provider_unavailable",
                                  http_status=response.status_code)
                else:
                    models = response.json().get("models", [])
                    names = {model.get("name", "").removeprefix("models/") for model in models if isinstance(model, dict) and "generateContent" in model.get("supportedGenerationMethods", [])}
                    result.update(connected=True, available_text_models=sorted(names & TEXT_MODELS),
                                  available_tts_models=sorted(names & TTS_MODELS),
                                  configured_text_available=settings.gemini_model in names,
                                  configured_tts_available=settings.gemini_tts_model in names)
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                result.update(reason="provider_unavailable")
            gemini_probe_cache.update(result=result, expires=time.monotonic() + 60)
            return result

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
        original = get_job(job_id)
        adjusted_scenes = None
        if original["status"] == "awaiting_review" and original["review_checkpoint"] == "media":
            drafts = [scene["id"] for scene in original["scenes"] if original["_state"]["visual_assets"].get(scene["id"], {}).get("draft")]
            if drafts and original["provider_mode"] == "free":
                raise HTTPException(422, "Replace the draft visuals with photos or clips before exporting: " + ", ".join(drafts))
            try:
                adjusted_scenes = await asyncio.to_thread(
                    media.fit_scene_timing, original["scenes"],
                    [original["_state"]["voice_assets"][scene["id"]] for scene in original["scenes"]],
                    original["duration_seconds"], settings.ffmpeg_path)
            except (media.ProviderError, KeyError) as exc:
                raise HTTPException(422, str(exc)) from exc
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
                if job["updated_at"] != original["updated_at"]:
                    raise Conflict("Media changed during timing inspection. Review the updated assets and try again.")
                if request.script is not None or request.scenes is not None:
                    raise HTTPException(422, "Media approval does not accept script or scene edits. Create a revised job for new content.")
                if adjusted_scenes is not None:
                    job["scenes"] = adjusted_scenes
                    job["duration_seconds"] = round(sum(scene["duration_seconds"] for scene in adjusted_scenes), 3)
                    (settings.storage_dir.resolve() / job_id / "scenes.json").write_text(json.dumps(adjusted_scenes, indent=2), encoding="utf-8")
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

    @app.post("/api/jobs/{job_id}/media")
    async def upload_media(job_id: str, file: UploadFile = File(...), kind: str = Form(...), scene_id: str | None = Form(default=None)):
        job = get_job(job_id)
        if kind not in {"visual", "voice", "music"}:
            raise HTTPException(422, "Choose visual, voice, or music.")
        if job["status"] != "awaiting_review" or job["review_checkpoint"] != "media":
            raise Conflict("Upload media while this job is awaiting media review.")
        if kind != "music" and scene_id not in {scene["id"] for scene in job["scenes"]}:
            raise HTTPException(422, "Choose a scene in this production.")
        directory = settings.storage_dir.resolve() / job_id
        asset = await prepare_upload(file, directory, kind, settings.ffmpeg_path)
        try:
            def save(doc):
                if doc["status"] != "awaiting_review" or doc["review_checkpoint"] != "media":
                    raise Conflict("The review has already resumed. This upload was not applied.")
                if kind == "music":
                    doc["_state"]["music_asset"] = asset
                else:
                    if scene_id not in {scene["id"] for scene in doc["scenes"]}:
                        raise Conflict("This scene has changed. Upload again to the updated plan.")
                    key = "visual_assets" if kind == "visual" else "voice_assets"
                    doc["_state"][key][scene_id] = asset
                doc["_state"]["done"] = [stage for stage in doc["_state"]["done"] if stage not in {"subtitles", "validate", "compose"}]
                doc["_state"]["media_approved"] = False
            updated = store.mutate(job_id, save)
        except BaseException:
            Path(asset["path"]).unlink(missing_ok=True)
            raise
        store.event(job_id, "review", "info", f"Uploaded {kind}" + (f" for {scene_id}." if kind != "music" else "."))
        return public_job(updated)

    @app.post("/api/jobs/{job_id}/music/remove")
    async def remove_music(job_id: str):
        get_job(job_id)
        def apply(job):
            if job["status"] != "awaiting_review" or job["review_checkpoint"] != "media":
                raise Conflict("Change music while awaiting media review.")
            job["_state"].pop("music_asset", None)
        updated = store.mutate(job_id, apply)
        store.event(job_id, "review", "info", "Background music removed from the export.")
        return public_job(updated)

    @app.post("/api/samples/coffee", status_code=201)
    async def coffee_sample():
        source = ROOT / "app/samples/coffee"
        if not (source / "dawn.png").is_file():
            raise HTTPException(503, "The prepared coffee sample is unavailable.")
        request = JobRequest(brief="A warm cinematic coffee promo for the fictional brand Ember & Bean.",
                             title="Ember & Bean · Coffee example", duration_seconds=16, provider_mode="free", review_required=True)
        def prepare(doc):
            directory = settings.storage_dir.resolve() / doc["id"]
            (directory / "images").mkdir(parents=True)
            (directory / "audio").mkdir()
            scenes = json.loads((source / "scenes.json").read_text(encoding="utf-8"))
            for scene in scenes:
                visual = directory / "images" / f"{scene['id']}.png"
                audio = directory / "audio" / f"{scene['id']}.wav"
                shutil.copyfile(source / f"{scene['id']}.png", visual)
                shutil.copyfile(source / f"{scene['id']}.wav", audio)
                doc["_state"]["visual_assets"][scene["id"]] = {"path": str(visual), "kind": "image", "provider": "prepared-ai-image", "draft": False}
                doc["_state"]["voice_assets"][scene["id"]] = {"path": str(audio), "provider": "prepared-zira-narration", "has_speech": True}
            music = directory / "audio/music.mp3"
            shutil.copyfile(source / "music.mp3", music)
            doc["_state"].update(done=["concept", "script", "scenes", "visuals", "voice"], plan_approved=True,
                                 music_asset={"path": str(music), "provider": "original-sample-score"})
            doc.update(status="awaiting_review", review_checkpoint="media", current_stage="voice", progress=62,
                       scenes=scenes, script=" ".join(scene["narration"] for scene in scenes), prepared_sample=True)
            (directory / "scenes.json").write_text(json.dumps(scenes, indent=2), encoding="utf-8")
            (directory / "script.txt").write_text(doc["script"], encoding="utf-8")
        job = store.create(request, initializer=prepare)
        store.event(job["id"], "review", "info", "Prepared coffee example loaded: existing AI stills, local narration, and an original score. Review or replace assets before exporting.")
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
                elif relative in {"final.mp4", "manifest.json", "poster.jpg"} and job["status"] == "completed":
                    allowed.append(path)
                elif relative.startswith(("images/", "audio/")) and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".wav", ".mp3", ".m4a", ".ogg"}:
                    # Only expose provider outputs committed to workflow state.
                    registered = [Path(asset["path"]).resolve() for key in ["visual_assets", "voice_assets"] for asset in job["_state"][key].values()]
                    if job["_state"].get("music_asset"):
                        registered.append(Path(job["_state"]["music_asset"]["path"]).resolve())
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
            kind = "image" if suffix in {".png", ".jpg", ".jpeg", ".webp"} else "audio" if suffix in {".wav", ".mp3", ".m4a", ".ogg"} else "video" if suffix in {".mp4", ".webm"} else "subtitle" if suffix == ".srt" else "document"
            entry = {"name": name, "kind": kind, "url": f"/api/jobs/{job_id}/artifacts/{quote(name, safe='/')}", "size_bytes": path.stat().st_size}
            for key, role in [("visual_assets", "visual"), ("voice_assets", "voice")]:
                for scene_id, asset in job["_state"][key].items():
                    if Path(asset["path"]).resolve() == path.resolve():
                        entry.update(scene_id=scene_id, role=role)
                        entry.update({field: asset[field] for field in ["provider", "source_url", "creator", "creator_url", "license_url", "duration_seconds", "draft", "filename", "placeholder_reason"] if field in asset})
            music = job["_state"].get("music_asset")
            if music and Path(music["path"]).resolve() == path.resolve():
                entry.update(role="music", provider=music.get("provider", "uploaded-music"), filename=music.get("filename", "Background music"))
            entries.append(entry)
        return {"artifacts": entries}

    @app.get("/api/jobs/{job_id}/artifacts/{name:path}")
    async def artifact(job_id: str, name: str):
        job = get_job(job_id)
        directory = settings.storage_dir.resolve() / job_id
        candidate = (directory / name).resolve()
        if not candidate.is_relative_to(directory) or candidate not in [path.resolve() for path in artifact_paths(job)]:
            raise HTTPException(404, "Artifact not found")
        return FileResponse(candidate, filename=candidate.name,
                            content_disposition_type="inline" if candidate.suffix in {".mp4", ".webm", ".wav", ".mp3", ".png", ".jpg", ".webp"} else "attachment")

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
