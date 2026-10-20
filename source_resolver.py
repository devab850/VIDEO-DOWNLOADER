"""Generic fallback for sites that yt-dlp cannot read directly.

What it handles (without any per-site code):
  1. Cookie-check pages: a tiny page that runs  document.cookie="x=1"; location.reload()
     -> we read the cookie, set it, and fetch the page again.
  2. Video links written inside the page HTML (<source>, <video>, og:video,
     JSON-LD contentUrl, "file": "...mp4", escaped \\/ links, .m3u8 ...).
  3. Players inside an <iframe> (one level deep).

Every request is validated with the `validator` callback you pass in (SSRF guard),
including every redirect hop.
"""
import hashlib
import html as htmllib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

MAX_BYTES = 3 * 1024 * 1024
MAX_HOPS = 5
MAX_COOKIE_ROUNDS = 4
MAX_IFRAMES = 3
MAX_CANDIDATES = 8
TIMEOUT = 15

_COOKIE_RE = re.compile(r"""document\.cookie\s*=\s*(["'])(.*?)\1""", re.S)
_IFRAME_RE = re.compile(r"""<iframe\b[^>]*?\bsrc\s*=\s*["']([^"']+)""", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OG_TITLE_RE = re.compile(
    r"""<meta[^>]+property\s*=\s*["']og:title["'][^>]*content\s*=\s*["']([^"']*)""", re.I)
_OG_IMAGE_RE = re.compile(
    r"""<meta[^>]+property\s*=\s*["']og:image["'][^>]*content\s*=\s*["']([^"']*)""", re.I)

# Extension must END there (so "preview.mp4.jpg" is not taken), and may be followed
# by "/" (some engines use .../video.mp4/?token=...) and a query string.
_EXT = r"\.(?:mp4|m3u8|webm|mpd)(?![\w.\-])/?(?:\?[^\"'\s<>\\]*)?"

# Ordered by reliability. Earlier patterns win when ranking.
_MEDIA_PATTERNS = [
    re.compile(r"""<(?:source|video)\b[^>]*?\bsrc\s*=\s*["']([^"']+)""", re.I),
    re.compile(
        r"""<meta[^>]+(?:og:video(?::url|:secure_url)?|twitter:player:stream)["'][^>]*"""
        r"""content\s*=\s*["']([^"']+)""", re.I),
    re.compile(
        r"""["']?(?:file|src|source|video_url|video_alt_url\d*|videoUrl|hls|stream|url)["']?\s*[:=]\s*"""
        r"""["']([^"']+?""" + _EXT + r""")["']""", re.I),
    re.compile(r'''"contentUrl"\s*:\s*"([^"]+)"'''),
    re.compile(r"""(https?://[^\s"'<>\\]+?""" + _EXT + r""")""", re.I),
]
_BAD_PARTS = ("vast", "splash.php", "/ads/", "doubleclick", "googlesyndication",
              "screenshots", "/preview", "thumb")
_TOKEN_HINT = re.compile(r"(token|acctoken|signature|signing|expires|[?&]sig=|[?&]hash=)", re.I)


@dataclass
class Candidate:
    url: str
    referer: str


@dataclass
class PageInfo:
    page_url: str
    cookies: Dict[str, str] = field(default_factory=dict)  # learned from THIS site only
    candidates: List[Candidate] = field(default_factory=list)
    title: Optional[str] = None
    thumbnail: Optional[str] = None


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def _get(session, url, validator, referer=None):
    """GET with manual redirects so every hop is validated. Returns (final_url, text)."""
    headers = {"Referer": referer} if referer else {}
    for _ in range(MAX_HOPS):
        validator(url)
        r = session.get(url, headers=headers, timeout=TIMEOUT,
                        stream=True, allow_redirects=False)
        try:
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location")
                if not loc:
                    raise ValueError("Redirect without Location")
                url = urljoin(url, loc)
                continue
            ctype = (r.headers.get("Content-Type") or "").lower()
            if not ("html" in ctype or "text" in ctype or "json" in ctype or "xml" in ctype or not ctype):
                return url, ""  # binary / video: not a page we can read
            r.raise_for_status()
            chunks, size = [], 0
            for chunk in r.iter_content(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    break
                chunks.append(chunk)
            body = b"".join(chunks)
            return url, body.decode(r.encoding or "utf-8", "replace")
        finally:
            r.close()
    raise ValueError("Too many redirects")


def _learn_cookies(text: str) -> Dict[str, str]:
    """Only trusts tiny pages (cookie-check pages are tiny)."""
    if len(text) > 4000:
        return {}
    found = {}
    for _, value in _COOKIE_RE.findall(text):
        first = value.split(";", 1)[0]
        if "=" in first:
            name, val = first.split("=", 1)
            name = name.strip()
            if name:
                found[name] = val.strip()
    return found


_POW_RE = re.compile(
    r"""<script[^>]*\bid=["']bc["'][^>]*>\s*(\{.*?\})\s*</script>""", re.S)
POW_MAX_SECONDS = 8
POW_MAX_TRIES = 8_000_000


def solve_pow_challenge(text: str) -> Optional[str]:
    """Solve the simple "Checking your browser" proof-of-work page.

    The page carries {"ch": <challenge>, "t": <target>}; the browser looks for the
    smallest number n such that the first 32 bits of SHA-256("<ch>:<n>") are <= target,
    then reloads the same URL with  __pow=<ch>~<n>&__t=<ms>_<tries>.
    Returns that query string (without leading ?/&) or None if this is not such a page.
    """
    if "BOTCATCH" not in text or len(text) > 20000:
        return None
    m = _POW_RE.search(text)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        ch, target = str(data["ch"]), int(data["t"]) & 0xFFFFFFFF
    except (ValueError, KeyError, TypeError):
        return None
    prefix = (ch + ":").encode()
    started = time.time()
    for n in range(POW_MAX_TRIES):
        digest = hashlib.sha256(prefix + str(n).encode()).digest()
        if int.from_bytes(digest[:4], "big") <= target:
            ms = int((time.time() - started) * 1000)
            return f"__pow={ch}~{n}&__t={ms}_{n}"
        if n % 50000 == 0 and time.time() - started > POW_MAX_SECONDS:
            break
    return None


def _with_query(url: str, extra: str) -> str:
    parts = urlparse(url)
    query = parts.query
    # drop an older __pow/__t so a rejected attempt is not stacked
    query = "&".join(q for q in query.split("&") if q and not q.startswith(("__pow=", "__t=")))
    query = f"{query}&{extra}" if query else extra
    return parts._replace(query=query).geturl()


def _fetch_with_cookie_check(session, url, validator, referer=None):
    """Fetch a page, passing the two kinds of tiny "gate" pages sites use:
    cookie-check (document.cookie=...; reload) and proof-of-work ("Checking your browser")."""
    learned: Dict[str, str] = {}
    final_url, text = url, ""
    pow_tried = False
    for _ in range(MAX_COOKIE_ROUNDS + 1):
        final_url, text = _get(session, url, validator, referer)

        new = _learn_cookies(text)
        fresh = {k: v for k, v in new.items() if learned.get(k) != v}
        if fresh:
            learned.update(fresh)
            host = urlparse(final_url).hostname or ""
            for k, v in fresh.items():
                session.cookies.set(k, v, domain=host, path="/")
            url = final_url
            continue

        if not pow_tried:
            answer = solve_pow_challenge(text)
            if answer:
                pow_tried = True
                url = _with_query(final_url, answer)
                continue
        break
    return final_url, text, learned


def _clean_url(raw: str, base: str) -> Optional[str]:
    u = htmllib.unescape(raw).replace("\\/", "/").replace("\\u0026", "&").strip()
    if not u or u.startswith(("data:", "blob:", "javascript:")):
        return None
    u = urljoin(base, u)
    if urlparse(u).scheme not in ("http", "https"):
        return None
    low = u.lower()
    if any(bad in low for bad in _BAD_PARTS):
        return None
    return u


def extract_media_urls(text: str, base_url: str) -> List[str]:
    text = text.replace("\\/", "/")
    seen, out = set(), []
    for pattern in _MEDIA_PATTERNS:
        for raw in pattern.findall(text):
            u = _clean_url(raw, base_url)
            if u and u not in seen:
                seen.add(u)
                out.append(u)
    return out


def _meta(text: str, base_url: str):
    title = None
    m = _OG_TITLE_RE.search(text) or _TITLE_RE.search(text)
    if m:
        title = re.sub(r"\s+", " ", htmllib.unescape(m.group(1))).strip()[:300] or None
    thumb = None
    m = _OG_IMAGE_RE.search(text)
    if m:
        thumb = _clean_url(m.group(1), base_url)
    return title, thumb


# Some sites show an "ad gate" (no player) until a cookie such as "clicked" exists.
# These are tried ONLY when the normal page had no video, and only sent to that site.
GATE_COOKIES = {"clicked": "1"}


def _collect(session, url, validator):
    """Fetch one page (+ its iframes) and return (final_url, text, learned_cookies, candidates)."""
    final_url, text, cookies = _fetch_with_cookie_check(session, url, validator)
    found, seen = [], set()
    for u in extract_media_urls(text, final_url):
        seen.add(u)
        found.append(Candidate(u, final_url))

    # Players inside iframes (one level deep).
    for raw in _IFRAME_RE.findall(text)[:MAX_IFRAMES]:
        frame_url = _clean_url(raw, final_url)
        if not frame_url:
            continue
        try:
            f_final, f_text, _ = _fetch_with_cookie_check(
                session, frame_url, validator, referer=final_url)
        except Exception:
            continue
        for u in extract_media_urls(f_text, f_final):
            if u not in seen:
                seen.add(u)
                found.append(Candidate(u, f_final))
    return final_url, text, cookies, found


def inspect_page(url: str, validator: Callable[[str], None]) -> PageInfo:
    """Fetch a page like a browser would and collect everything useful from it."""
    session = _new_session()
    final_url, text, cookies, found = _collect(session, url, validator)
    cookies = dict(cookies)

    if not found:
        # Retry once with the "gate" cookies (same host only).
        host = urlparse(final_url).hostname or ""
        for k, v in GATE_COOKIES.items():
            session.cookies.set(k, v, domain=host, path="/")
        try:
            f2, t2, c2, found2 = _collect(session, url, validator)
        except Exception:
            found2 = []
        if found2:
            final_url, text, found = f2, t2, found2
            cookies.update(c2)
            cookies.update(GATE_COOKIES)

    host = (urlparse(final_url).hostname or "").lower()
    for c in session.cookies:
        dom = (c.domain or "").lstrip(".").lower()
        if dom and (host == dom or host.endswith("." + dom)):
            cookies.setdefault(c.name, c.value)

    info = PageInfo(page_url=final_url, cookies=cookies)
    info.title, info.thumbnail = _meta(text, final_url)
    info.candidates = found
    # Links that carry a token/signature are the real, playable ones: try them first.
    info.candidates.sort(key=lambda c: 0 if _TOKEN_HINT.search(c.url) else 1)
    info.candidates = info.candidates[:MAX_CANDIDATES]
    return info
