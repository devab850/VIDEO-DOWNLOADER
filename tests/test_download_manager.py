
import time
import uuid

from download_manager import (
    STORAGE_DIR,
    create_job,
    get_job,
    issue_download_token,
    resolve_download_token,
    cleanup_expired_files,
)


def test_create_job():
    job = create_job()

    assert job["status"] == "queued"
    assert get_job(job["job_id"]) is not None


def test_download_token_resolves_file():
    job = create_job()
    path = STORAGE_DIR / f"test-{uuid.uuid4().hex}.mp4"
    path.write_bytes(b"test video")

    token, expires_at = issue_download_token(
        job["job_id"],
        path,
    )

    assert resolve_download_token(token) == path.resolve()
    assert expires_at > time.time()

    path.unlink(missing_ok=True)


def test_expired_token_is_rejected():
    job = create_job()
    path = STORAGE_DIR / f"test-{uuid.uuid4().hex}.mp4"
    path.write_bytes(b"test video")

    token, _ = issue_download_token(job["job_id"], path)

    # Simulate expiry without waiting 30 minutes.
    from download_manager import _tokens

    _tokens[token]["expires_at"] = time.time() - 1

    assert resolve_download_token(token) is None

    cleanup_expired_files()
