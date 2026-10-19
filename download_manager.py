import secrets
import threading
import time
import uuid
from pathlib import Path

STORAGE_DIR = Path("/tmp/deva_downloads")
RETENTION_SECONDS = 30 * 60

STORAGE_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()
_jobs = {}
_tokens = {}

def create_job():
    job_id = str(uuid.uuid4())

    with _lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "file_path": None,
            "download_token": None,
            "download_expires_at": None,
            "error": None,
        }

    return get_job(job_id)

def get_job(job_id):
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None

def update_job(job_id, **changes):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return False

        job.update(changes)
        job["updated_at"] = time.time()
        return True

def issue_download_token(job_id, file_path):
    path = Path(file_path).resolve()

    if not path.is_relative_to(STORAGE_DIR.resolve()):
        raise ValueError("File is outside the download storage directory.")

    if not path.is_file():
        raise FileNotFoundError("Completed download file was not found.")

    now = time.time()
    token = secrets.token_urlsafe(32)
    expires_at = now + RETENTION_SECONDS

    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise ValueError("Unknown download job.")

        _tokens[token] = {
            "job_id": job_id,
            "file_path": str(path),
            "expires_at": expires_at,
        }

        job.update({
            "status": "completed",
            "file_path": str(path),
            "download_token": token,
            "download_expires_at": expires_at,
            "updated_at": now,
        })

    return token, expires_at

def resolve_download_token(token):
    now = time.time()

    with _lock:
        entry = _tokens.get(token)

        if not entry:
            return None

        if now >= entry["expires_at"]:
            _tokens.pop(token, None)
            return None

        path = Path(entry["file_path"]).resolve()

        if not path.is_relative_to(STORAGE_DIR.resolve()):
            _tokens.pop(token, None)
            return None

        if not path.is_file():
            _tokens.pop(token, None)
            return None

        return path

def cleanup_expired_files():
    now = time.time()
    expired_paths = []

    with _lock:
        for token, entry in list(_tokens.items()):
            if now >= entry["expires_at"]:
                expired_paths.append(Path(entry["file_path"]))
                _tokens.pop(token, None)

        for job_id, job in list(_jobs.items()):
            if now - job["created_at"] > 24 * 60 * 60:
                _jobs.pop(job_id, None)

    for path in set(expired_paths):
        try:
            path.resolve().relative_to(STORAGE_DIR.resolve())
            path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
