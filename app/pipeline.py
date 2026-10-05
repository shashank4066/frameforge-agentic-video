"""A supervisor chooses registered tools from dependency-safe candidates.

Every completed stage and individual media asset is persisted. Human gates
are enforced here, independently of the model's chosen tool.
"""
import asyncio
import json
import logging
from pathlib import Path
import time
import uuid

from . import media
from .models import validate_scenes
from .providers import create_provider
from .store import Conflict, now

logger = logging.getLogger("frameforge.pipeline")
STAGES = ["concept", "script", "scenes", "visuals", "voice", "subtitles", "validate", "compose"]
TOOLS = {"generate_concept": "concept", "generate_script": "script", "plan_scenes": "scenes",
         "generate_visuals": "visuals", "generate_voice": "voice", "build_subtitles": "subtitles",
         "validate_assets": "validate", "compose_video": "compose"}


class RepairNeeded(Exception):
    """A bounded repair routes the supervisor back to a damaged asset stage."""


class Pipeline:
    def __init__(self, store, settings, provider_factory=create_provider):
        self.store, self.settings, self.provider_factory = store, settings, provider_factory

    def candidates(self, job):
        done = set(job["_state"]["done"])
        for tool in ["generate_concept", "generate_script", "plan_scenes"]:
            if TOOLS[tool] not in done:
                return [tool]
        candidates = [tool for tool in ["generate_visuals", "generate_voice"] if TOOLS[tool] not in done]
        if candidates:
            return candidates
        for tool in ["build_subtitles", "validate_assets", "compose_video"]:
            if TOOLS[tool] not in done:
                return [tool]
        return []

    def checkpoint(self, job):
        state = job["_state"]
        if not job["review_required"]:
            return None
        if "scenes" in state["done"] and not state["plan_approved"]:
            return "plan"
        if {"visuals", "voice"}.issubset(state["done"]) and not state["media_approved"]:
            return "media"
        return None

    def _patch(self, job_id, owner, **changes):
        def apply(job):
            job.update(changes)
        return self.store.mutate(job_id, apply, owner)

    async def _heartbeat(self, job_id, owner, task):
        while True:
            await asyncio.sleep(10)
            if not self.store.renew(job_id, owner):
                task.cancel()
                return

    async def run(self, job_id, owner):
        task = asyncio.current_task()
        heartbeat = asyncio.create_task(self._heartbeat(job_id, owner, task))
        try:
            job = self.store.get(job_id)
            provider = self.provider_factory(job["provider_mode"], self.settings)
            directory = self.settings.storage_dir.resolve() / job_id
            directory.mkdir(parents=True, exist_ok=True)
            while True:
                self.store.assert_owner(job_id, owner)
                job = self.store.get(job_id)
                if job["status"] != "running":
                    return
                checkpoint = self.checkpoint(job)
                if checkpoint:
                    self._patch(job_id, owner, status="awaiting_review", review_checkpoint=checkpoint)
                    self.store.event(job_id, "review", "info",
                                     "Review the script and scene plan before media generation." if checkpoint == "plan"
                                     else "Review generated visual and voice assets before composition.")
                    return
                candidates = self.candidates(job)
                if not candidates:
                    self._patch(job_id, owner, status="completed", progress=100, review_checkpoint=None,
                                completed_at=now(), error=None)
                    self.store.event(job_id, "compose", "info", "Video ready. Download MP4, captions, and generation manifest.")
                    return
                tool = await self._select_tool(provider, job, candidates)
                stage = TOOLS[tool]
                self._patch(job_id, owner, current_stage=stage, review_checkpoint=None)
                self.store.event(job_id, stage, "info", f"Executing {tool}.")
                started = time.monotonic()
                try:
                    await self._retry_stage(stage, provider, job_id, owner, directory)
                except RepairNeeded:
                    continue
                elapsed = time.monotonic() - started
                def finish(doc):
                    state = doc["_state"]
                    if stage not in state["done"]:
                        state["done"].append(stage)
                    state["stage_seconds"][stage] = state["stage_seconds"].get(stage, 0) + elapsed
                    doc["progress"] = round(len(state["done"]) / len(STAGES) * 100)
                self.store.mutate(job_id, finish, owner)
                self.store.event(job_id, stage, "info", f"{stage.capitalize()} finished in {elapsed:.1f}s.")
        except asyncio.CancelledError:
            # A clean shutdown requeues work. An API cancellation stays cancelled.
            try:
                self._patch(job_id, owner, status="queued")
            except (Conflict, KeyError):
                pass
            raise
        except Conflict:
            logger.info("Job lease lost or job cancelled: %s", job_id)
        except Exception as exc:
            message = self._safe_error(exc)
            try:
                self._patch(job_id, owner, status="failed", error=message)
                self.store.event(job_id, self.store.get(job_id)["current_stage"], "error", message)
            except Conflict:
                pass
            logger.error("Job %s failed: %s", job_id, message)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self.store.release(job_id, owner)

    def _safe_error(self, exc):
        message = str(exc) or type(exc).__name__
        for secret in [self.settings.openai_api_key, self.settings.video_api_key, self.settings.pexels_api_key, self.settings.gemini_api_key]:
            if secret:
                message = message.replace(secret, "[redacted]")
        return message[:1000]

    async def _select_tool(self, provider, job, candidates):
        if not hasattr(provider, "choose_action"):
            return candidates[0]
        try:
            snapshot = {"brief": job["brief"], "completed_stages": job["_state"]["done"],
                        "aspect_ratio": job["aspect_ratio"], "duration_seconds": job["duration_seconds"],
                        "scene_count": len(job["scenes"]), "provider_mode": job["provider_mode"]}
            async with asyncio.timeout(self.settings.provider_timeout_seconds):
                decision = await provider.choose_action(snapshot, candidates)
            if decision.get("tool") not in candidates:
                raise ValueError("Supervisor selected a tool outside the allowed dependencies")
            self.store.event(job["id"], "supervisor", "info",
                             f"Selected {decision['tool']}: {str(decision.get('reason', 'Dependencies satisfied'))[:250]}")
            return decision["tool"]
        except Exception:
            self.store.event(job["id"], "supervisor", "warning",
                             "Supervisor response unavailable. Using the next dependency-safe tool.")
            return candidates[0]

    async def _retry_stage(self, stage, provider, job_id, owner, directory):
        for attempt in range(1, self.settings.max_stage_attempts + 1):
            def count(doc):
                counts = doc["_state"]["attempts"]
                counts[stage] = counts.get(stage, 0) + 1
            self.store.mutate(job_id, count, owner)
            try:
                timeout = max(300, self.settings.provider_timeout_seconds) if stage == "compose" else self.settings.provider_timeout_seconds
                if stage == "voice" and self.settings.gemini_ready:
                    timeout = max(timeout, len(self.store.get(job_id)["scenes"]) * 35 + 60)
                async with asyncio.timeout(timeout):
                    await getattr(self, f"stage_{stage}")(provider, job_id, owner, directory)
                return
            except (Conflict, RepairNeeded):
                raise
            except Exception as exc:
                self.store.event(job_id, stage, "warning",
                                 f"Attempt {attempt}/{self.settings.max_stage_attempts} failed: {self._safe_error(exc)}")
                if attempt == self.settings.max_stage_attempts:
                    raise
                await asyncio.sleep(min(2 ** (attempt - 1), 8))

    async def stage_concept(self, provider, job_id, owner, directory):
        job = self.store.get(job_id)
        concept = await provider.generate_concept(job["brief"], job["style"])
        if not isinstance(concept, dict) or not isinstance(concept.get("concept"), str) or not concept["concept"].strip():
            raise ValueError("Concept agent returned incomplete output")
        self._patch(job_id, owner, concept=concept)

    async def stage_script(self, provider, job_id, owner, directory):
        job = self.store.get(job_id)
        script = await provider.generate_script(job["brief"], job["concept"], job["duration_seconds"])
        if not isinstance(script, str) or not 10 <= len(script.strip()) <= 12000:
            raise ValueError("Script agent returned an empty or oversized script")
        def save(doc):
            (directory / "script.txt").write_text(script, encoding="utf-8")
            doc["script"] = script
        self.store.mutate(job_id, save, owner)

    async def stage_scenes(self, provider, job_id, owner, directory):
        job = self.store.get(job_id)
        scenes = validate_scenes(await provider.plan_scenes(job["script"], job["duration_seconds"],
                                                           job["aspect_ratio"], job["style"]), job["duration_seconds"])
        def save(doc):
            (directory / "scenes.json").write_text(json.dumps(scenes, indent=2), encoding="utf-8")
            doc["scenes"] = scenes
        self.store.mutate(job_id, save, owner)

    async def _assets(self, provider, job_id, owner, directory, kind):
        job = self.store.get(job_id)
        key = "visual_assets" if kind == "visual" else "voice_assets"
        folder = directory / ("images" if kind == "visual" else "audio")
        folder.mkdir(exist_ok=True)
        semaphore = asyncio.Semaphore(2)

        async def generate(scene):
            async with semaphore:
                self.store.assert_owner(job_id, owner)
                latest = self.store.get(job_id)
                if latest["status"] != "running":
                    raise Conflict("Job cancelled")
                cached = latest["_state"][key].get(scene["id"])
                if cached and Path(cached["path"]).is_file() and Path(cached["path"]).stat().st_size > 0:
                    return
                asset_name = f"{scene['id']}-{uuid.uuid4().hex[:12]}"
                if kind == "visual":
                    asset = await provider.generate_visual(scene, folder / f"{asset_name}.png", job["aspect_ratio"])
                else:
                    asset = await provider.generate_voice(scene["narration"], folder / f"{asset_name}.wav")
                path = Path(asset["path"]).resolve()
                if not path.is_relative_to(directory) or not path.is_file() or not path.stat().st_size:
                    raise ValueError("Provider returned a missing or invalid asset")
                def save(doc):
                    doc["_state"][key][scene["id"]] = asset
                self.store.mutate(job_id, save, owner)
                self.store.event(job_id, "visuals" if kind == "visual" else "voice", "info",
                                 f"Saved {kind} for {scene['id']} ({asset.get('provider', job['provider_mode'])}).")
                if kind == "voice" and not asset.get("has_speech", True):
                    self.store.event(job_id, "voice", "warning", "Offline speech engine unavailable; this demo scene has silent audio.")

        # Wait for every child before retrying; no overlapping duplicate generation.
        results = await asyncio.gather(*(generate(scene) for scene in job["scenes"]), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def stage_visuals(self, provider, job_id, owner, directory):
        await self._assets(provider, job_id, owner, directory, "visual")

    async def stage_voice(self, provider, job_id, owner, directory):
        await self._assets(provider, job_id, owner, directory, "voice")

    async def stage_subtitles(self, provider, job_id, owner, directory):
        job = self.store.get(job_id)
        voices = [job["_state"]["voice_assets"][scene["id"]] for scene in job["scenes"]]
        scenes = await asyncio.to_thread(media.fit_scene_timing, job["scenes"], voices,
                                        job["duration_seconds"], self.settings.ffmpeg_path)
        pending = directory / f"subtitles-{uuid.uuid4().hex}.srt"
        await asyncio.to_thread(media.build_subtitles, scenes, pending, voices, self.settings.ffmpeg_path)
        try:
            def save(doc):
                pending.replace(directory / "subtitles.srt")
                doc["scenes"] = scenes
                doc["duration_seconds"] = round(sum(scene["duration_seconds"] for scene in scenes), 3)
                (directory / "scenes.json").write_text(json.dumps(scenes, indent=2), encoding="utf-8")
            self.store.mutate(job_id, save, owner)
        finally:
            pending.unlink(missing_ok=True)

    async def stage_validate(self, provider, job_id, owner, directory):
        job = self.store.get(job_id)
        scenes = validate_scenes(job["scenes"], job["duration_seconds"])
        state = job["_state"]
        missing = []
        for key in ["visual_assets", "voice_assets"]:
            for scene in scenes:
                asset = state[key].get(scene["id"])
                if not asset or not Path(asset["path"]).is_file() or not Path(asset["path"]).stat().st_size:
                    missing.append(f"{key}:{scene['id']}")
        if not missing and hasattr(media, "validate_assets"):
            for scene in scenes:
                try:
                    await asyncio.to_thread(media.validate_assets, [scene], [state["visual_assets"][scene["id"]]],
                                            [state["voice_assets"][scene["id"]]], self.settings.ffmpeg_path)
                except Exception:
                    # A corrupt pair is regenerated together; verified pairs stay cached.
                    missing.extend([f"visual_assets:{scene['id']}", f"voice_assets:{scene['id']}"])
        if missing:
            if state["repair_count"] >= 2:
                raise ValueError("Media validation still failed after two repair rounds. Check the configured provider.")
            # Invalidate only damaged assets; the supervisor can revisit their tools.
            def invalidate(doc):
                doc["_state"]["repair_count"] += 1
                for item in missing:
                    key, scene_id = item.split(":")
                    doc["_state"][key].pop(scene_id, None)
                    stage = "visuals" if key == "visual_assets" else "voice"
                    doc["_state"]["done"] = [done for done in doc["_state"]["done"] if done not in {stage, "validate", "compose"}]
                doc["_state"]["media_approved"] = False
            self.store.mutate(job_id, invalidate, owner)
            self.store.event(job_id, "validate", "warning", "Damaged media detected. Supervisor will regenerate affected scenes and request review again.")
            raise RepairNeeded("Regenerate missing or corrupt assets")
        if not (directory / "subtitles.srt").is_file():
            raise ValueError("Missing subtitles")
        self.store.event(job_id, "validate", "info", "Scene timing, visual assets, audio, and subtitles validated.")

    async def stage_compose(self, provider, job_id, owner, directory):
        job = self.store.get(job_id)
        state = job["_state"]
        render_dir = directory / "renders"
        render_dir.mkdir(exist_ok=True)
        render_path = render_dir / f"final-{uuid.uuid4().hex}.mp4"
        result = await asyncio.to_thread(media.compose_video,
                                        scenes=job["scenes"],
                                        visual_assets=[state["visual_assets"][s["id"]] for s in job["scenes"]],
                                        voice_assets=[state["voice_assets"][s["id"]] for s in job["scenes"]],
                                        subtitle_path=directory / "subtitles.srt", output_path=render_path,
                                        aspect_ratio=job["aspect_ratio"], ffmpeg_path=self.settings.ffmpeg_path,
                                        music_asset=state.get("music_asset"), transition=job.get("transition", "fade"))
        if not Path(result["path"]).is_file() or Path(result["path"]).stat().st_size < 1000:
            raise ValueError("Composition produced an empty video")
        poster = render_path.with_suffix(".jpg")
        try:
            await asyncio.to_thread(media._run, [str(self.settings.ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y",
                                                "-ss", "0.8", "-i", str(render_path), "-frames:v", "1", "-q:v", "3", str(poster)], timeout=20)
        except media.ProviderError:
            poster.unlink(missing_ok=True)
        manifest = {"job_id": job_id, "title": job["title"], "provider_mode": job["provider_mode"],
                    "generated_at": now(), "duration_seconds": job["duration_seconds"],
                    "aspect_ratio": job["aspect_ratio"], "style": job["style"], "scenes": job["scenes"],
                    "visual_assets": state["visual_assets"], "voice_assets": state["voice_assets"],
                    "music_asset": state.get("music_asset"),
                    "result": result, "cost_usd": None,
                    "note": "Free studio uses Pexels footage or uploaded assets, optional Gemini planning/narration, and FFmpeg editing. Draft placeholders are labeled. Prepared samples reuse existing assets. Demo uses illustrated cards; live mode uses configured paid providers."}
        def publish(doc):
            # File publication is inside the ownership transaction: a cancelled
            # or superseded worker cannot replace another run's final output.
            Path(result["path"]).replace(directory / "final.mp4")
            if poster.is_file():
                poster.replace(directory / "poster.jpg")
                doc["poster_url"] = f"/api/jobs/{job_id}/artifacts/poster.jpg"
            result["path"] = str(directory / "final.mp4")
            (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            doc.update(output_url=f"/api/jobs/{job_id}/artifacts/final.mp4",
                       has_speech=result.get("has_speech", False), result=result)
        self.store.mutate(job_id, publish, owner)
