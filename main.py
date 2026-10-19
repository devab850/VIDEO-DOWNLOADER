
import ipaddress
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

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

# Use a browser-like User-Agent and learn simple JS cookie checks per source.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


def headers_for_url(url: str) -> dict:
    return {"User-Agent": BROWSER_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}


def _read_small_response(response, limit: int = 256 * 1024) -> str:
    """Read a bounded prefix; never buffer an entire media file in memory."""
    chunks = []
    remaining = limit
    try:
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            chunk = chunk[:remaining]
            chunks.append(chunk)
            remaining -= len(chunk)
            if remaining <= 0 or b"</html>" in chunk.lower():
                break
    finally:
        response.close()
    return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")


def _apply_simple_document_cookies(session: requests.Session, page_url: str, body: str) -> bool:
    """Support simple JS cookie-check pages; not CAPTCHA, login, or DRM bypass."""
    host = (urlparse(page_url).hostname or "").lower()
    changed = False
    assignments = re.findall(r"document\.cookie\s*=\s*[\"'](.*?)[\"']", body, flags=re.IGNORECASE | re.DOTALL)
    for assignment in assignments:
        parts = assignment.split(";")
        if "=" not in parts[0]:
            continue
        name, value = parts[0].split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or any(ch in name for ch in "\\r\\n;="):
            continue
        path, domain, secure = "/", host, False
        for attribute in parts[1:]:
            key, _, attr_value = attribute.strip().partition("=")
            key = key.lower()
            if key == "path" and attr_value.startswith("/"):
                path = attr_value
            elif key == "domain" and attr_value:
                candidate = attr_value.lstrip(".").lower()
                if host == candidate or host.endswith("." + candidate):
                    domain = candidate
            elif key == "secure":
                secure = True
        session.cookies.set(name, value, domain=domain, path=path, secure=secure)
        changed = True
    return changed


@contextmanager
def source_access(url: str):
    """Best-effort preflight and temporary domain-scoped Netscape cookie jar.

    yt-dlp remains the primary extractor. This only retries simple JS cookie pages;
    cookies remain domain-scoped in the cookie jar instead of being sent as a raw
    Cookie header to every CDN host. If preflight fails, yt-dlp still gets a chance.
    """
    session = requests.Session()
    session.headers.update(headers_for_url(url))
    try:
        response = session.get(url, timeout=(5, 15), allow_redirects=True, stream=True)
        content_type = (response.headers.get("Content-Type") or "").lower()
        final_url = response.url or url
        if any(kind in content_type for kind in ("text/html", "text/plain", "application/xhtml")) or not content_type:
            body = _read_small_response(response)
            if "document.cookie" in body.lower() and ("location.reload" in body.lower() or "location.href" in body.lower()):
                if _apply_simple_document_cookies(session, final_url, body):
                    retry = session.get(final_url, timeout=(5, 15), allow_redirects=True, stream=True)
                    retry.close()
        else:
            response.close()
    except requests.RequestException as exc:
        print(f"SOURCE PREFLIGHT NOTE: {type(exc).__name__}: {exc}", flush=True)

    fd, cookie_path = tempfile.mkstemp(prefix="deva-cookies-", suffix=".txt")
    os.close(fd)
    try:
        jar = MozillaCookieJar(cookie_path)
        for cookie in session.cookies:
            jar.set_cookie(cookie)
        jar.save(ignore_discard=True, ignore_expires=True)
        yield {"cookiefile": cookie_path, "http_headers": headers_for_url(url)}
    finally:
        session.close()
        try:
            os.unlink(cookie_path)
        except OSError:
            pass

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
    with source_access(url) as access:
        opts = {
            "cachedir": False,
            "http_headers": access["http_headers"],
            "cookiefile": access["cookiefile"],
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
    """Recognize a single direct MP4 even when Generic/HTML5 labels it format_id='0'.

    Some pages expose one direct MP4 URL but omit codec metadata. In that specific
    single-format case, the actual format_id may be '0', 'mp4', or another extractor
    identifier; requiring the literal ID 'mp4' incorrectly rejects valid media.
    """
    formats = info.get("formats") or []
    if len(formats) != 1:
        return False
    item = formats[0]
    return (
        bool(item.get("url"))
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
        # A single direct MP4 may have an extractor ID such as '0', not 'mp4'.
        # is_direct_mp4_info() has already restricted this fallback to one MP4 URL
        # with absent codec metadata, so it is safe to mark that sole format as video.
        if direct_mp4:
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



def validate_output_file(file_path: Path, download_type: str) -> None:
    """Reject empty, unreadable, or stream-less outputs before marking a job complete."""
    if not file_path.is_file() or file_path.stat().st_size < 1024:
        raise ValueError("Downloaded file is missing or too small to be valid.")

    command = [
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(file_path),
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=60, check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-300:]
        raise ValueError("Downloaded file failed media validation." + (f" {detail}" if detail else ""))

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Downloaded file returned invalid media metadata.") from exc

    streams = data.get("streams") or []
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if download_type == "video" and not has_video:
        raise ValueError("Downloaded file contains no readable video stream.")
    if download_type in ("audio_mp3", "audio_original") and not has_audio:
        raise ValueError("Downloaded file contains no readable audio stream.")

    duration = (data.get("format") or {}).get("duration")
    try:
        if duration is not None and float(duration) <= 0:
            raise ValueError("Downloaded file has an invalid duration.")
    except (TypeError, ValueError) as exc:
        raise ValueError("Downloaded file has an invalid duration.") from exc


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
            if direct_mp4:
                # Generic extractor exposes a direct MP4 but may omit codec metadata.
                # Download that verified direct-MP4 candidate, then extract audio below.
                format_selector = str(available[0].get("format_id", "mp4"))
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
            "http_headers": headers_for_url(url),
            "max_filesize": MAX_FILE_MB * 1024 * 1024,
            "format": format_selector,
            "outtmpl": output_template,
            "overwrites": False,
            "continuedl": False,
        }

        if download_type == "audio_mp3":
            if not direct_mp4:
                options["format"] = "bestaudio/best"
            options["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        elif download_type == "audio_original" and direct_mp4:
            # Extract an audio-only file while preserving the source audio codec where possible.
            options["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "best",
            }]

        with source_access(url) as access:
            options["http_headers"] = access["http_headers"]
            options["cookiefile"] = access["cookiefile"]
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

        validate_output_file(file_path, download_type)

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
