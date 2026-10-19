import ipaddress
import json
import socket
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl, Field
import yt_dlp

app = FastAPI(title="DEVA Universal Video Downloader API", version="0.1.0")

# For a public deployment, replace "*" with your exact Blogger origin, e.g.
# https://yourblog.blogspot.com and your custom domain if any.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

MAX_URL_LENGTH = 2048
MAX_DURATION_SECONDS = 4 * 60 * 60  # safety limit for this starter service
MAX_FILE_MB = 1024

class AnalyzeRequest(BaseModel):
    url: str = Field(min_length=8, max_length=MAX_URL_LENGTH)

def validate_public_http_url(raw_url: str) -> str:
    """Basic URL validation. For production, also enforce network egress controls."""
    raw_url = raw_url.strip()
    if len(raw_url) > MAX_URL_LENGTH:
        raise HTTPException(400, "URL is too long.")
    parsed = urlparse(raw_url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "Only public HTTP/HTTPS URLs are supported.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "Please enter a valid public video URL.")
    host = parsed.hostname.rstrip(".").lower()
    if host in ("localhost", "localhost.localdomain"):
        raise HTTPException(400, "Private/local URLs are not allowed.")
    try:
        infos = socket.getaddrinfo(host, None)
        ips = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos}
        if not ips or any(
            ip.is_private or ip.is_loopback or ip.is_link_local or
            ip.is_reserved or ip.is_multicast or ip.is_unspecified
            for ip in ips
        ):
            raise HTTPException(400, "Private or non-public network addresses are not allowed.")
    except socket.gaierror:
        raise HTTPException(400, "The website hostname could not be resolved.")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))

def safe_info(url: str):
    # No cookies, browser profiles, login sessions, DRM, or private credentials.
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
        "extract_flat": False,
        "cachedir": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

@app.get("/")
def root():
    return {"service": "DEVA Universal Video Downloader API", "status": "ok"}

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    url = validate_public_http_url(req.url)
    try:
        info = safe_info(url)
        if not info:
            raise HTTPException(422, "Video information could not be found.")
        formats = []
        for f in (info.get("formats") or []):
            # Return metadata only; the browser is not given extractor stream URLs.
            if not f.get("url"):
                continue
            vcodec = f.get("vcodec")
            acodec = f.get("acodec")
            if vcodec == "none" and acodec == "none":
                continue
            height = f.get("height")
            ext = f.get("ext")
            fmt = {
                "format_id": str(f.get("format_id", "")),
                "ext": ext,
                "height": height,
                "width": f.get("width"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "has_video": vcodec not in (None, "none"),
                "has_audio": acodec not in (None, "none"),
                "note": (f.get("format_note") or "")[:80],
            }
            formats.append(fmt)
        formats.sort(key=lambda x: (x.get("height") or 0, x.get("has_audio", False)), reverse=True)
        return {
            "title": (info.get("title") or "Video")[:300],
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "uploader": (info.get("uploader") or info.get("channel") or "")[:150],
            "webpage_url": info.get("webpage_url") or url,
            "extractor": info.get("extractor_key") or info.get("extractor") or "Unknown",
            "formats": formats[:40],
            "download_supported": bool(formats),
            "notice": "Only use for content you own or are authorized to download. DRM-protected, private, login-only, and restricted content is not supported."
        }
    except HTTPException:
        raise
    except Exception as exc:
        # Avoid exposing internal stack traces or sensitive extractor details.
        msg = str(exc).lower()
        if "unsupported url" in msg or "not supported" in msg:
            detail = "This website or video format is not supported by the current engine."
        elif "drm" in msg:
            detail = "DRM-protected content is not supported."
        else:
            detail = "Could not analyze this link. It may be unsupported, private, restricted, or temporarily unavailable."
        raise HTTPException(422, detail)

@app.post("/api/download")
def download(req: AnalyzeRequest):
    # Deliberately not implemented as a remote server-side file relay in this starter:
    # relaying arbitrary files can create abuse, bandwidth, and security risks.
    # Use the platform's official download option or download directly from a lawful
    # direct media URL. A production implementation needs authentication, quotas,
    # egress controls, storage policy, and explicit rights checks.
    validate_public_http_url(req.url)
    raise HTTPException(
        501,
        "Server-side download relay is disabled in this starter. Use the source's authorized download link, or configure a protected download worker for content you own or have permission to download."
    )
