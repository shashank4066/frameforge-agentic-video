"""SQLite is the source of truth; queue messages are only wake-up hints."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter
import json
import sqlite3
import time
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(Exception):
    pass


class Store:
    def __init__(self, database_path: Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, document TEXT NOT NULL,
                    created_at TEXT NOT NULL, lease_owner TEXT, lease_until REAL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, lease_until);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    stage TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_job ON events(job_id, id);
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create(self, request, initializer=None):
        job_id = uuid.uuid4().hex
        doc = request.model_dump()
        doc.update(id=job_id, title=request.title or request.brief[:64], status="queued",
                   current_stage="concept", progress=0, script="", scenes=[], error=None,
                   created_at=now(), updated_at=now(), review_checkpoint=None, cost_usd=None,
                   _state={"done": [], "plan_approved": False, "media_approved": False,
                           "visual_assets": {}, "voice_assets": {}, "attempts": {},
                           "stage_seconds": {}, "repair_count": 0})
        if initializer is not None:
            initializer(doc)
        with self.connection() as conn:
            conn.execute("INSERT INTO jobs(id,status,document,created_at) VALUES (?,?,?,?)",
                         (job_id, doc["status"], json.dumps(doc), doc["created_at"]))
        self.event(job_id, "queue", "info", "Production created. Free studio uses stock footage or uploads; demo uses local cards; live uses configured providers.")
        return doc

    def get(self, job_id):
        with self.connection() as conn:
            row = conn.execute("SELECT document FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, limit=100):
        with self.connection() as conn:
            rows = conn.execute("SELECT document FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def mutate(self, job_id, change, owner=None):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            if owner is not None and (row["lease_owner"] != owner or row["status"] != "running" or row["lease_until"] < time.time()):
                raise Conflict("Worker no longer owns this job")
            doc = json.loads(row["document"])
            change(doc)
            doc["updated_at"] = now()
            conn.execute("UPDATE jobs SET status=?, document=? WHERE id=?",
                         (doc["status"], json.dumps(doc), job_id))
            return doc

    def claim(self, owner, lease_seconds=60):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""SELECT * FROM jobs WHERE status='queued'
                OR (status='running' AND lease_until < ?) ORDER BY created_at LIMIT 1""", (time.time(),)).fetchone()
            if not row:
                return None
            doc = json.loads(row["document"])
            recovering = doc["status"] == "running"
            doc.update(status="running", updated_at=now(), error=None)
            conn.execute("UPDATE jobs SET status='running', document=?, lease_owner=?, lease_until=? WHERE id=?",
                         (json.dumps(doc), owner, time.time() + lease_seconds, doc["id"]))
        if recovering:
            self.event(doc["id"], "queue", "warning", "Expired worker lease recovered; resuming persisted stages.")
        return doc

    def assert_owner(self, job_id, owner):
        with self.connection() as conn:
            row = conn.execute("SELECT status, lease_owner, lease_until FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "running" or row["lease_owner"] != owner or row["lease_until"] < time.time():
            raise Conflict("Worker no longer owns this job")

    def renew(self, job_id, owner, lease_seconds=60):
        with self.connection() as conn:
            count = conn.execute("UPDATE jobs SET lease_until=? WHERE id=? AND lease_owner=? AND status='running' AND lease_until>=?",
                                 (time.time() + lease_seconds, job_id, owner, time.time())).rowcount
        return bool(count)

    def release(self, job_id, owner):
        with self.connection() as conn:
            conn.execute("UPDATE jobs SET lease_owner=NULL, lease_until=0 WHERE id=? AND lease_owner=?", (job_id, owner))

    def event(self, job_id, stage, level, message):
        with self.connection() as conn:
            conn.execute("INSERT INTO events(job_id,stage,level,message,created_at) VALUES (?,?,?,?,?)",
                         (job_id, stage, level, message[:1500], now()))

    def events(self, job_id):
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        return [dict(row) for row in rows]

    def metrics(self):
        with self.connection() as conn:
            jobs = [json.loads(row[0]) for row in conn.execute("SELECT document FROM jobs")]
        statuses = Counter(job["status"] for job in jobs)
        lines = ["# HELP frameforge_jobs Number of jobs by status", "# TYPE frameforge_jobs gauge"]
        lines.extend(f'frameforge_jobs{{status="{status}"}} {statuses[status]}' for status in
                     ["queued", "running", "awaiting_review", "completed", "failed", "cancelled"])
        for stage in ["concept", "script", "scenes", "visuals", "voice", "subtitles", "validate", "compose"]:
            attempts = sum(job["_state"]["attempts"].get(stage, 0) for job in jobs)
            seconds = sum(job["_state"]["stage_seconds"].get(stage, 0) for job in jobs)
            lines.extend([f'frameforge_stage_attempts_total{{stage="{stage}"}} {attempts}',
                          f'frameforge_stage_seconds_total{{stage="{stage}"}} {seconds:.3f}'])
        return "\n".join(lines) + "\n"


def public_job(doc):
    return {**{key: value for key, value in doc.items() if not key.startswith("_")},
            "completed_stages": doc["_state"]["done"],
            "stage_attempts": doc["_state"]["attempts"],
            "stage_seconds": doc["_state"]["stage_seconds"]}
