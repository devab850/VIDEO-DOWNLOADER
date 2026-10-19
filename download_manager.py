
import secrets
import threading
import time
import uuid
from pathlib import Path

STORAGE_DIR = Path("/tmp/deva_downloads").resolve()
RETENTION_SECONDS = 30 * 60
JOB_RETENTION_SECONDS = 24 * 60 * 60

STORAGE_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.RLock()
_jobs = {}
_tokens = {}

# Files awaiting deletion, even after their expired token is removed.
_pending_delete = set()


def _safe_path(file_path):
    path = Path(file_path).resolve()

    if not path.is_relative_to(STORAGE_DIR):
        raise ValueError("File is outside download storage.")

    return path


def create_job():
    now = time.time()
    job_id = str(uuid.uuid4())

    with _lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "file_path": None,
            "download_token": None,
            "download_expires_at": None,
            "error": None,
        }

        return dict(_jobs[job_id])


def get_job(job_id):
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def update_job(job_id, **changes):
    with _lock:
        job = _jobs.get(job_id)

        if job is None:
            return False

        job.update(changes)
        job["updated_at"] = time.time()
        return True


def issue_download_token(job_id, file_path):
    path = _safe_path(file_path)

    if not path.is_file():
        raise FileNotFoundError("Completed file was not found.")

    now = time.time()
    token = secrets.token_urlsafe(32)
    expires_at = now + RETENTION_SECONDS

    with _lock:
        job = _jobs.get(job_id)

        if job is None:
            raise ValueError("Unknown download job.")

        _tokens[token] = {
            "job_id": job_id,
            "file_path": str(path),
            "expires_at": expires_at,
        }

        # If this file was previously queued for deletion, an active
        # token now protects it until that token expires.
        _pending_delete.discard(str(path))

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

        if entry is None:
            return None

        if now >= entry["expires_at"]:
            _pending_delete.add(entry["file_path"])
            _tokens.pop(token, None)
            return None

        try:
            path = _safe_path(entry["file_path"])
        except (ValueError, OSError):
            _tokens.pop(token, None)
            return None

        if not path.is_file():
            _tokens.pop(token, None)
            return None

        return path


def cleanup_expired_files():
    now = time.time()

    with _lock:
        # Expired tokens queue their files for deletion before removal.
        for token, entry in list(_tokens.items()):
            if now >= entry["expires_at"]:
                _pending_delete.add(entry["file_path"])
                _tokens.pop(token, None)

        # A file must stay if any valid token still references it.
        active_paths = {
            entry["file_path"]
            for entry in _tokens.values()
            if now < entry["expires_at"]
        }

        paths_to_delete = [
            path for path in _pending_delete
            if path not in active_paths
        ]

        for job_id, job in list(_jobs.items()):
            if now - job["created_at"] > JOB_RETENTION_SECONDS:
                _jobs.pop(job_id, None)

    # Do filesystem operations outside the registry lock.
    for file_path in paths_to_delete:
        try:
            path = _safe_path(file_path)
            path.unlink(missing_ok=True)

            with _lock:
                # Recheck active tokens before clearing the pending record.
                still_active = any(
                    entry["file_path"] == str(path)
                    and time.time() < entry["expires_at"]
                    for entry in _tokens.values()
                )

                if not still_active:
                    _pending_delete.discard(str(path))

        except (OSError, ValueError):
            # Keep the path queued so a later cleanup pass can retry.
            pass
