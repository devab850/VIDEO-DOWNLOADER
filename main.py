
import ipaddress
import socket
import subprocess
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
         "cachedir": False,
    "force_generic_extractor": False,
"verbose": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "extractor_retries": 1,
        "retries": 1,
        "ignoreerrors": False,
        "no_color": True,
        
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def is_direct_mp4_info(info):
    """Recognize yt-dlp's single direct MP4 format when codec metadata is absent."""
    formats = info.get("formats") or []
    if len(formats) != 1:
        return False
    item = formats[0]
    return (
        bool(item.get("url"))
        and str(item.get("format_id", "")) == "mp4"
        and (item.get("ext") or "").lower() == "mp4"
        and item.get("vcodec") in (None, "")
        and item.get("acodec") in (None, "")
    )


def public_formats(info):
    """Expose known streams and the narrowly recognized direct-MP4 fallback."""
    result = []
    seen = set()
    direct_mp4 = is_direct_mp4_info(info)

    for item in info.get("formats") or []:
        if not item.get("url"):
            continue

        vcodec = item.get("vcodec")
        acodec = item.get("acodec")
        format_id = str(item.get("format_id", ""))
        ext = item.get("ext")

        has_video = bool(vcodec and vcodec != "none")
        has_audio = bool(acodec and acodec != "none")
        # yt-dlp's Generic extractor may expose one direct MP4 URL without codec
        # metadata. Treat only this exact single-format shape as a video fallback.
        if direct_mp4 and format_id == "mp4":
            has_video = True
        if not has_video and not has_audio:
            continue

        height = item.get("height")
        dedupe_key = (format_id, height, ext, has_video, has_audio)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        result.append({
            "format_id": format_id,
            "ext": ext,
            "height": height,
            "width": item.get("width"),
            "filesize": item.get("filesize") or item.get("filesize_approx"),
            "has_video": has_video,
            "has_audio": has_audio,
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


def available_video_qualities(formats):
    """Return only resolution options backed by actual video formats."""
    heights = {
        item.get("height")
        for item in formats
        if item.get("has_video") and isinstance(item.get("height"), int)
    }
    return [
        {"value": f"{height}p", "height": height}
        for height in sorted(heights, reverse=True)
    ]


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

        print(
            "FORMAT DETAILS:",
            [
                {
                    "format_id": item.get("format_id"),
                    "ext": item.get("ext"),
                    "vcodec": item.get("vcodec"),
                    "acodec": item.get("acodec"),
                    "height": item.get("height"),
                    "has_url": bool(item.get("url")),
                }
                for item in (info.get("formats") or [])
            ],
            flush=True,
        )

        print(
            "FORMAT DEBUG:",
            "extractor=", info.get("extractor"),
            "format_count=", len(info.get("formats") or []),
            "keys=", sorted(info.keys()),
            flush=True,
        )


       

        formats = public_formats(info)
        direct_mp4 = is_direct_mp4_info(info)
        video_qualities = available_video_qualities(formats)
        if direct_mp4:
            # These are generated max-height choices, not source-native formats.
            video_qualities = [
                {"value": f"{height}p", "height": height}
                for height in (360, 480, 720, 1080)
            ]
        has_video_stream = any(item.get("has_video") for item in formats)
        has_audio_stream = any(item.get("has_audio") for item in formats)

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
            "video_qualities": video_qualities,
            "has_video_stream": has_video_stream,
            "has_audio_stream": has_audio_stream,
            "download_supported": bool(formats),
            "quality_mode": "available" if video_qualities else "auto_only",
            "quality_notice": (
                "This source exposes one direct MP4. Selected quality will be "
                "created up to the chosen height; the source will not be upscaled."
                if direct_mp4 else
                "Available resolutions are based on detected video streams."
                if video_qualities else
                "No explicit video resolutions were detected. Auto will try "
                "the best supported stream."
            ),
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

        available = info.get("formats") or []
        direct_mp4 = is_direct_mp4_info(info)

        if download_type == "video":
            if not direct_mp4 and not any(
                f.get("vcodec") not in (None, "none") for f in available
            ):
                raise ValueError("No supported video formats found.")

            if direct_mp4:
                # Select the exact direct MP4 format; generic 'best' selectors can
                # reject it because the extractor has no codec metadata.
                format_selector = str(available[0].get("format_id", "mp4"))
            elif quality == "auto":
                format_selector = "bestvideo*+bestaudio/best[vcodec!=none]/best"
            else:
                max_height = int(quality[:-1])
                format_selector = (
                    f"bestvideo*[height<={max_height}]+bestaudio/"
                    f"best[height<={max_height}][vcodec!=none]/"
                    f"best[height<={max_height}]"
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

        # A direct MP4 has no reported resolution. For a requested quality, use
        # ffmpeg to make a copy capped at that height (never upscale a smaller source).
        if direct_mp4 and download_type == "video" and quality != "auto":
            max_height = int(quality[:-1])
            transcoded_path = STORAGE_DIR / f"{job_id}_scaled.mp4"
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(file_path),
                "-map", "0:v:0", "-map", "0:a?",
                "-vf", fr"scale=-2:min(ih\,{max_height})",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(transcoded_path),
            ]
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=1800, check=False
            )
            if result.returncode != 0 or not transcoded_path.is_file() or transcoded_path.stat().st_size == 0:
                transcoded_path.unlink(missing_ok=True)
                detail = (result.stderr or "").strip()[-500:]
                raise ValueError("Could not create the requested quality version." + (f" {detail}" if detail else ""))
            file_path.unlink(missing_ok=True)
            file_path = transcoded_path

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
    except Exception as exc:
        print(
            f"DOWNLOAD ERROR [{job_id}]: {type(exc).__name__}: {exc}",
            flush=True,
        )
        message = str(exc).lower()
        if "no video formats" in message or "requested format is not available" in message:
            public_error = (
                "No usable video stream was found for this link. "
                "Try a supported public video URL."
            )
        elif "no supported audio formats" in message:
            public_error = "No usable audio stream was found for this link."
        elif "requested quality version" in message:
            public_error = "The source downloaded, but the requested quality version could not be created."
        else:
            public_error = (
                "Download failed. The source may be unsupported, restricted, "
                "or temporarily unavailable."
            )
        update_job(job_id, status="failed", error=public_error)


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
