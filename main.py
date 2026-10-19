
import ipaddress
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Literal
import yt_dlp

from download_manager import (
    STORAGE_DIR,
    create_job,
    get_job,
    update_job,
    issue_download_token,
    resolve_download_token,
    cleanup_expired_files,
)

app = FastAPI(
    title="DEVA Universal Video Downloader API",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

MAX_URL_LENGTH = 2048
MAX_DURATION_SECONDS = 4 * 60 * 60
MAX_FILE_MB = 1024
CLEANUP_INTERVAL_SECONDS = 60

BLOCKED_HOSTS = {
    "youtube.com",
    "youtu.be",
    "instagram.com",
}

_cleanup_thread_started = False


class AnalyzeRequest(BaseModel):
    url: str = Field(min_length=8, max_length=MAX_URL_LENGTH)


class DownloadRequest(AnalyzeRequest):
    # Defaults preserve the existing frontend/API behavior.
    download_type: Literal["video", "audio_mp3", "audio_original"] = "video"
    quality: Literal["auto", "360p", "480p", "720p", "1080p"] = "auto"


def validate_public_http_url(raw_url: str) -> str:
    raw_url = raw_url.strip()

    if len(raw_url) > MAX_URL_LENGTH:
        raise HTTPException(400, "URL is too long.")

    parsed = urlparse(raw_url)

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "Only HTTP/HTTPS URLs are supported.")

    if not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "Please enter a valid public video URL.")

    host = parsed.hostname.rstrip(".").lower()

    if any(
        host == blocked or host.endswith("." + blocked)
        for blocked in BLOCKED_HOSTS
    ):
        raise HTTPException(
            400,
            "This source is not supported by this service.",
        )

    try:
        infos = socket.getaddrinfo(host, None)
        ips = {
            ipaddress.ip_address(info[4][0].split("%")[0])
            for info in infos
        }

        if not ips or any(not ip.is_global for ip in ips):
            raise HTTPException(
                400,
                "Private or non-public network addresses are not allowed.",
            )

    except socket.gaierror:
        raise HTTPException(400, "Website hostname could not be resolved.")

    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        parsed.query,
        "",
    ))


def safe_info(url: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "extractor_retries": 1,
        "retries": 1,
        "ignoreerrors": False,
        "no_color": True,
        "cachedir": False,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def public_formats(info):
    result = []

    for item in info.get("formats") or []:
        if not item.get("url"):
            continue

        vcodec = item.get("vcodec")
        acodec = item.get("acodec")

        if vcodec == "none" and acodec == "none":
            continue

        result.append({
            "format_id": str(item.get("format_id", "")),
            "ext": item.get("ext"),
            "height": item.get("height"),
            "width": item.get("width"),
            "filesize": (
                item.get("filesize")
                or item.get("filesize_approx")
            ),
            "has_video": vcodec not in (None, "none"),
            "has_audio": acodec not in (None, "none"),
            "note": (item.get("format_note") or "")[:80],
        })

    result.sort(
        key=lambda item: (
            item.get("height") or 0,
            item.get("has_audio", False),
        ),
        reverse=True,
    )

    return result[:40]


@app.get("/")
def root():
    return {
        "service": "DEVA Universal Video Downloader API",
        "status": "ok",
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    url = validate_public_http_url(req.url)

    try:
        info = safe_info(url)

        if not info:
            raise HTTPException(
                422,
                "Video information could not be found.",
            )

        formats = public_formats(info)

        return {
            "title": (info.get("title") or "Video")[:300],
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "uploader": (
                info.get("uploader") or info.get("channel") or ""
            )[:150],
            "extractor": (
                info.get("extractor_key")
                or info.get("extractor")
                or "Unknown"
            ),
            "formats": formats,
            "download_supported": bool(formats),
            "notice": (
                "Only download content you own or are authorized "
                "to download. Restricted and DRM-protected content "
                "is not supported."
            ),
        }

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            422,
            "Could not analyze this link. It may be unsupported, "
            "restricted, or temporarily unavailable.",
        )


def process_download(job_id: str, url: str, download_type: str = "video", quality: str = "auto"):
    try:
        update_job(job_id, status="processing", error=None)

        # Revalidate immediately before processing.
        url = validate_public_http_url(url)
        info = safe_info(url)

        if not info:
            raise ValueError("Video information unavailable.")

        duration = info.get("duration")

        if duration and duration > MAX_DURATION_SECONDS:
            raise ValueError("Video exceeds the duration limit.")

        formats = public_formats(info)
        available = info.get("formats") or []

        if download_type == "video":
            if not any(f.get("vcodec") not in (None, "none") for f in available):
                raise ValueError("No supported video formats found.")
            if quality == "auto":
                format_selector = "best[acodec!=none][vcodec!=none]/best"
            else:
                max_height = int(quality[:-1])
                format_selector = (
                    f"bestvideo[height<={max_height}]+bestaudio/"
                    f"best[height<={max_height}]/best"
                )
        else:
            if not any(f.get("acodec") not in (None, "none") for f in available):
                raise ValueError("No supported audio formats found.")
            format_selector = "bestaudio/best"

        output_template = str(STORAGE_DIR / f"{job_id}.%(ext)s")

        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 20,
            "retries": 1,
            "extractor_retries": 1,
            "cachedir": False,
            "max_filesize": MAX_FILE_MB * 1024 * 1024,
            "format": format_selector,
            "outtmpl": output_template,
            "overwrites": False,
            "continuedl": False,
        }

        if download_type == "audio_mp3":
            options["format"] = "bestaudio/best"
            options["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

        candidates = [
            path for path in STORAGE_DIR.glob(f"{job_id}.*")
            if path.is_file()
            and not path.name.endswith((".part", ".ytdl"))
        ]

        if not candidates:
            raise ValueError("Download did not produce a completed file.")

        file_path = max(candidates, key=lambda path: path.stat().st_size)

        if file_path.stat().st_size > MAX_FILE_MB * 1024 * 1024:
            file_path.unlink(missing_ok=True)
            raise ValueError("Downloaded file exceeds the size limit.")

        token, expires_at = issue_download_token(job_id, file_path)

        update_job(
            job_id,
            status="completed",
            download_token=token,
            download_expires_at=expires_at,
            file_name=file_path.name,
        )

    except Exception:
        update_job(
            job_id,
            status="failed",
            error=(
                "Download failed. The source may be unsupported, "
                "restricted, or temporarily unavailable."
            ),
        )


@app.post("/api/download", status_code=202)
def download(req: DownloadRequest, background_tasks: BackgroundTasks):
    url = validate_public_http_url(req.url)

    job = create_job()

    background_tasks.add_task(
        process_download,
        job["job_id"],
        url,
        req.download_type,
        req.quality,
    )

    return {
        "job_id": job["job_id"],
        "status": "queued",
        "status_url": f"/api/jobs/{job['job_id']}",
    }


@app.get("/api/jobs/{job_id}")
def download_status(job_id: str):
    job = get_job(job_id)

    if not job:
        raise HTTPException(404, "Download job not found.")

    response = {
        "job_id": job["job_id"],
        "status": job["status"],
        "error": job.get("error"),
    }

    if job["status"] == "completed":
        response["download_url"] = (
            f"/api/files/{job['download_token']}"
        )
        response["expires_at"] = job["download_expires_at"]

    return response


@app.get("/api/files/{token}")
def get_download_file(token: str):
    path = resolve_download_token(token)

    if path is None:
        raise HTTPException(
            404,
            "Download link is invalid, expired, or unavailable.",
        )

    return FileResponse(
        path=path,
        filename=path.name,
        media_type="application/octet-stream",
    )


def cleanup_loop():
    while True:
        try:
            cleanup_expired_files()

            # Remove abandoned files left by failed jobs or restarts.
            now = time.time()

            for path in STORAGE_DIR.iterdir():
                try:
                    if (
                        path.is_file()
                        and now - path.stat().st_mtime > 2 * 60 * 60
                    ):
                        path.unlink(missing_ok=True)
                except OSError:
                    pass

        except Exception:
            pass

        time.sleep(CLEANUP_INTERVAL_SECONDS)


@app.on_event("startup")
def start_cleanup_worker():
    global _cleanup_thread_started

    STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    if not _cleanup_thread_started:
        threading.Thread(
            target=cleanup_loop,
            daemon=True,
            name="deva-file-cleanup",
        ).start()
        _cleanup_thread_started = True
