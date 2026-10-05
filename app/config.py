from dataclasses import dataclass, field
from pathlib import Path
import os

from dotenv import load_dotenv


@dataclass
class Settings:
    database_path: Path = Path("data/frameforge.db")
    storage_dir: Path = Path("data/artifacts")
    queue_backend: str = "local"
    redis_url: str = "redis://localhost:6379/0"
    max_concurrent_jobs: int = 2
    max_stage_attempts: int = 3
    openai_api_key: str = field(default="", repr=False)
    openai_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4.1-mini"
    image_model: str = "gpt-image-1"
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "alloy"
    video_api_url: str = ""
    video_api_key: str = field(default="", repr=False)
    pexels_api_key: str = field(default="", repr=False)
    gemini_api_key: str = field(default="", repr=False)
    gemini_model: str = "gemini-3.1-flash-lite"
    gemini_tts_model: str = "gemini-3.1-flash-tts-preview"
    gemini_tts_voice: str = "Kore"
    provider_timeout_seconds: float = 120
    ffmpeg_path: str = "ffmpeg"
    start_worker: bool = True
    # Public-demo protection: each browser gets its own production list, and
    # quota-consuming (queued/running) jobs are capped per visitor and overall.
    max_active_jobs_per_visitor: int = 2
    max_queued_jobs: int = 20
    # Delete jobs and their files after this many hours; 0 keeps them forever.
    job_retention_hours: float = 0

    @property
    def live_ready(self) -> bool:
        return bool(self.openai_api_key.strip())

    @property
    def pexels_ready(self) -> bool:
        return bool(self.pexels_api_key.strip())

    @property
    def gemini_ready(self) -> bool:
        return bool(self.gemini_api_key.strip())

    @classmethod
    def from_env(cls):
        load_dotenv()
        queue = os.getenv("QUEUE_BACKEND", "local").lower()
        if queue not in {"local", "redis"}:
            raise ValueError("QUEUE_BACKEND must be local or redis")
        return cls(
            database_path=Path(os.getenv("DATABASE_PATH", "data/frameforge.db")),
            storage_dir=Path(os.getenv("STORAGE_DIR", "data/artifacts")),
            queue_backend=queue,
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            max_concurrent_jobs=max(1, min(8, int(os.getenv("MAX_CONCURRENT_JOBS", "2")))),
            max_stage_attempts=max(1, min(5, int(os.getenv("MAX_STAGE_ATTEMPTS", "3")))),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            llm_model=os.getenv("LLM_MODEL") or "gpt-4.1-mini",
            image_model=os.getenv("IMAGE_MODEL", "gpt-image-1"),
            tts_model=os.getenv("TTS_MODEL", "gpt-4o-mini-tts"),
            tts_voice=os.getenv("TTS_VOICE", "alloy"),
            video_api_url=os.getenv("VIDEO_API_URL", ""),
            video_api_key=os.getenv("VIDEO_API_KEY", ""),
            pexels_api_key=os.getenv("PEXELS_API_KEY", ""),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL") or "gemini-3.1-flash-lite",
            gemini_tts_model=os.getenv("GEMINI_TTS_MODEL") or "gemini-3.1-flash-tts-preview",
            gemini_tts_voice=os.getenv("GEMINI_TTS_VOICE") or "Kore",
            provider_timeout_seconds=max(10, float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "120"))),
            ffmpeg_path=os.getenv("FFMPEG_PATH") or "ffmpeg",
            max_active_jobs_per_visitor=max(1, int(os.getenv("MAX_ACTIVE_JOBS_PER_VISITOR", "2"))),
            max_queued_jobs=max(1, int(os.getenv("MAX_QUEUED_JOBS", "20"))),
            job_retention_hours=max(0.0, float(os.getenv("JOB_RETENTION_HOURS", "0"))),
        )
