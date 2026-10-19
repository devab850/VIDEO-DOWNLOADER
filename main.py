

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
        if info:
            print(
                "FORMAT DEBUG:",
                "extractor=", info.get("extractor"),
                "format_count=", len(info.get("formats") or []),
                "keys=", list(info.keys()),
                flush=True,
            )

        if not info:
            raise HTTPException(
                422,
                "Video information could not be found.",
            )

        formats = public_formats(info)
        video_qualities = available_video_qualities(formats)
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
                "Available resolutions are based on detected video streams."
                if video_qualities else
                "No explicit video resolutions were detected. Auto will try "
                "the best supported stream; download may fail if the source "
                "does not expose a real video stream."
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

        formats = public_formats(info)
        available = info.get("formats") or []

        if download_type == "video":
            if not any(f.get("vcodec") not in (None, "none") for f in available):
                raise ValueError("No supported video formats found.")
            if quality == "auto":
                # Prefer best video + audio, then a combined stream, then
                # the best format yt-dlp can legitimately select.
                format_selector = (
                    "bestvideo*+bestaudio/best[vcodec!=none]/best"
                )
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
