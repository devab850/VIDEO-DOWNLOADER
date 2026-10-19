
from pathlib import Path

from fastapi.testclient import TestClient

import main
from download_manager import (
    STORAGE_DIR,
    create_job,
    issue_download_token,
)


client = TestClient(main.app)


def test_health_endpoint():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_download_status_unknown_job():
    response = client.get("/api/jobs/unknown-job-id")

    assert response.status_code == 404


def test_download_file_rejects_unknown_token():
    response = client.get("/api/files/invalid-token")

    assert response.status_code == 404


def test_completed_job_returns_temporary_link():
    job = create_job()
    file_path = STORAGE_DIR / f"{job['job_id']}.mp4"
    file_path.write_bytes(b"test video content")

    try:
        token, expires_at = issue_download_token(
            job["job_id"],
            file_path,
        )

        response = client.get(
            f"/api/jobs/{job['job_id']}"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["download_url"] == f"/api/files/{token}"
        assert data["expires_at"] == expires_at

        file_response = client.get(f"/api/files/{token}")
        assert file_response.status_code == 200
        assert file_response.content == b"test video content"

    finally:
        file_path.unlink(missing_ok=True)
