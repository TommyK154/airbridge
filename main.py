"""AirBridge: a LAN photo and file bridge between a phone and a desktop.

Run on the desktop. The phone connects over the same Wi-Fi via a browser,
no app required. On startup a QR code is printed to the terminal; scan it
with the phone camera and the transfer page opens.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import importlib.util
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import sys

import qrcode
import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from starlette.background import BackgroundTask

BASE_DIR = Path(__file__).parent

# Path split between running from source and running as a frozen (PyInstaller)
# app. Frozen: bundled read-only assets live under sys._MEIPASS, writable state
# goes to the user profile (never next to the exe), and the default shared
# folder is a visible folder in the user's home (not Documents, which OneDrive
# may silently sync to the cloud; this is a LAN-only tool). From source:
# everything stays inside the project directory, exactly as before.
FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
WEB_DIR = RESOURCE_DIR / "web"
if FROZEN:
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AirBridge"
    DEFAULT_SHARED_DIR = Path.home() / "AirBridge"
else:
    DATA_DIR = BASE_DIR / ".airbridge"
    DEFAULT_SHARED_DIR = BASE_DIR / "shared"

CHUNK = 1 << 20  # 1 MiB streaming chunk
SESSION_COOKIE = "airbridge_session"
THUMB_MAX = 320  # longest side, in pixels, of a generated thumbnail

# Shared URL list. Persisted next to the thumbnail cache in the data dir so
# links survive a server restart.
LINKS_PATH = DATA_DIR / "links.json"
LINKS_MAX = 50  # rolling cap; oldest links are dropped past this
LINKS_URL_MAX = 2048  # reject absurdly long URLs to keep the store bounded
LINKS_TITLE_MAX = 200
_links_lock = threading.Lock()  # guards read/modify/write of LINKS_PATH

# File suffixes worth offering an inline preview for. The browser does the
# rendering, so a failed load falls back to a type badge on the client side.
PREVIEWABLE = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".heic", ".heif", ".avif", ".svg",
}

# Video suffixes that get a frame-grab thumbnail when the optional
# 'videothumbs' extra (imageio-ffmpeg) is installed. Kept in sync with the
# share-to-Photos list in web/index.html.
VIDEO_PREVIEWABLE = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}

# NTFS forbids these characters in a filename, plus ASCII control chars.
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Reserved Windows device names (case-insensitive), checked against the stem.
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"}
_RESERVED_NAMES |= {f"COM{i}" for i in range(1, 10)}
_RESERVED_NAMES |= {f"LPT{i}" for i in range(1, 10)}
MAX_NAME_LEN = 200


class Config:
    """Runtime configuration, populated in main()."""

    shared_dir: Path = DEFAULT_SHARED_DIR
    token: str | None = None
    auth_enabled: bool = True
    max_mb: int = 0  # per-file upload cap in MB, 0 means unlimited
    max_bytes: int = 0  # derived from max_mb in main()


cfg = Config()
app = FastAPI(title="AirBridge", docs_url=None, redoc_url=None, openapi_url=None)


# --------------------------------------------------------------------------- #
# Auth: a token in the QR URL grants a session cookie. Without it, requests
# from other devices on the LAN are refused.
# --------------------------------------------------------------------------- #
@app.middleware("http")
async def auth_gate(request: Request, call_next):
    if not cfg.auth_enabled:
        return await call_next(request)

    query_token = request.query_params.get("t")
    if query_token and cfg.token and secrets.compare_digest(query_token, cfg.token):
        # Valid token: set the session cookie and strip the token from the URL.
        resp = RedirectResponse(url=request.url.path, status_code=302)
        resp.set_cookie(
            SESSION_COOKIE,
            cfg.token,
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24,
        )
        return resp

    cookie_token = request.cookies.get(SESSION_COOKIE)
    if cookie_token and cfg.token and secrets.compare_digest(cookie_token, cfg.token):
        return await call_next(request)

    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'>"
            "<title>AirBridge</title>"
            "<body style='font-family:system-ui;background:#0e1216;color:#e6edf3;"
            "display:grid;place-items:center;height:100vh;margin:0;text-align:center'>"
            "<div><h1 style='letter-spacing:.18em'>AIRBRIDGE</h1>"
            "<p style='color:#8b97a6'>Scan the QR code shown in the desktop terminal "
            "to connect.</p></div></body>",
            status_code=401,
        )
    return JSONResponse({"detail": "Unauthorized. Scan the QR code to connect."}, status_code=401)


# --------------------------------------------------------------------------- #
# Security headers: applied to every response, including the auth gate's.
# The CSP keeps 'unsafe-inline' for style and script because the UI is a single
# inline page by design (no external assets), per CLAUDE.md.
# --------------------------------------------------------------------------- #
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for key, value in _SECURITY_HEADERS.items():
        response.headers[key] = value
    return response


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def sanitize(name: str) -> str:
    """Reduce a client filename to a safe basename for NTFS.

    iOS can send names with characters or reserved device names that Windows
    rejects, so normalize aggressively before writing.
    """
    # Take the last path segment by hand. Path(...).name is OS-aware and would
    # treat a leading "a:" as a drive letter on Windows, dropping it.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    # Replace illegal characters and ASCII control characters.
    name = _ILLEGAL_CHARS.sub("_", name)
    # Strip surrounding whitespace, then trailing dots and spaces (NTFS forbids
    # a trailing dot or space).
    name = name.strip().rstrip(". ")

    if not name or name in {".", ".."}:
        return "upload.bin"

    # Prefix reserved device names so the stem is no longer reserved.
    stem = name.split(".", 1)[0]
    if stem.upper() in _RESERVED_NAMES:
        name = "_" + name

    # Cap the total length, preserving the extension where possible.
    if len(name) > MAX_NAME_LEN:
        suffix = Path(name).suffix
        if len(suffix) < MAX_NAME_LEN:
            stem_part = name[: MAX_NAME_LEN - len(suffix)]
            name = stem_part + suffix
        else:
            name = name[:MAX_NAME_LEN]

    return name


def unique_path(path: Path) -> Path:
    """Avoid clobbering an existing file by suffixing ' (1)', ' (2)', ..."""
    if not path.exists():
        return path
    i = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def resolve_in_shared(name: str) -> Path:
    """Resolve a basename inside the shared dir, rejecting path traversal."""
    base = cfg.shared_dir.resolve()
    target = (base / Path(name).name).resolve()
    if target != base and base not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return target


@functools.lru_cache(maxsize=1)
def thumbnails_available() -> bool:
    """True when the optional 'thumbnails' extra (Pillow) is installed.

    Uses find_spec so Pillow is not imported into memory just to check; the
    actual import happens lazily in the thumbnail endpoint. HEIC support is
    added there by registering pillow-heif when it is present.
    """
    return importlib.util.find_spec("PIL") is not None


@functools.lru_cache(maxsize=1)
def video_thumbnails_available() -> bool:
    """True when the optional 'videothumbs' extra (imageio-ffmpeg) is installed."""
    return importlib.util.find_spec("imageio_ffmpeg") is not None


def build_video_thumbnail(src: Path, dest: Path) -> None:
    """Write a small JPEG thumbnail of a video's first second to dest.

    Runs the ffmpeg binary bundled by imageio-ffmpeg to grab one frame,
    scaled so the longest side is THUMB_MAX. Tries a one-second seek first
    (skips black lead-in frames), then falls back to the very first frame
    for clips shorter than that. Raises on any failure so the caller can
    fall back to the client-side type badge.
    """
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    scale = f"scale=w={THUMB_MAX}:h={THUMB_MAX}:force_original_aspect_ratio=decrease"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Never flash a console window when running under the windowed tray exe.
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    for seek in (["-ss", "1"], []):
        cmd = [
            ffmpeg, "-y", "-loglevel", "error", *seek,
            "-i", str(src), "-frames:v", "1", "-vf", scale, str(dest),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=20, creationflags=no_window
            )
        except subprocess.TimeoutExpired:
            break
        if result.returncode == 0 and dest.is_file() and dest.stat().st_size > 0:
            return
    dest.unlink(missing_ok=True)
    raise RuntimeError(f"ffmpeg could not extract a frame from {src.name}")


def build_thumbnail(src: Path, dest: Path) -> None:
    """Write a small JPEG thumbnail of src to dest (longest side THUMB_MAX).

    Imports Pillow lazily and registers the HEIC opener if pillow-heif is
    available. Raises on any decode or unsupported-format error so the caller
    can fall back to serving the original file.
    """
    from PIL import Image, ImageOps

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass  # HEIC will simply not decode; other formats still work.

    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail((THUMB_MAX, THUMB_MAX))
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, "JPEG", quality=80)


def require_airbridge_header(request: Request) -> None:
    """CSRF defense in depth: require a custom header on state-changing calls.

    A custom header forces a CORS preflight that the server never grants
    cross-origin, so a cross-site page cannot reach these endpoints even with a
    valid session cookie.
    """
    if request.headers.get("X-AirBridge") != "1":
        raise HTTPException(status_code=403, detail="Missing X-AirBridge header")


def human_size(num: int) -> str:
    """Format a byte count as a short human-readable string."""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            precision = 0 if unit == "B" else 1
            return f"{size:.{precision}f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def log_transfer(
    request: Request, action: str, name: str, size: int | None = None
) -> None:
    """Print one concise line per transfer to stdout."""
    ip = request.client.host if request.client else "?"
    stamp = datetime.now().strftime("%H:%M:%S")
    detail = f" ({human_size(size)})" if size is not None else ""
    print(f"[{stamp}] {ip} {action:<4} {name}{detail}", flush=True)


# --------------------------------------------------------------------------- #
# Shared links: a small persisted list of URLs either device can post, view,
# open, and delete. Stored as JSON, guarded by _links_lock, capped at LINKS_MAX.
# --------------------------------------------------------------------------- #
def load_links() -> list[dict]:
    """Return the stored links, or an empty list if the file is absent or bad.

    Never raises: a missing or corrupt store is treated as empty so a stray
    write or manual edit cannot take the endpoints down.
    """
    try:
        data = json.loads(LINKS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


def save_links(links: list[dict]) -> None:
    """Persist links to LINKS_PATH atomically (temp file then os.replace)."""
    LINKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix="links_", suffix=".json", dir=str(LINKS_PATH.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(links, out, ensure_ascii=False, indent=2)
        os.replace(tmp_name, LINKS_PATH)
    except BaseException:
        os.unlink(tmp_name)
        raise


def link_source(request: Request) -> str:
    """Guess whether a request came from a phone or the PC by User-Agent.

    Used only as the default source when the client does not submit one, so a
    coarse substring check is fine.
    """
    ua = request.headers.get("user-agent", "")
    mobile = ("iPhone", "iPad", "iPod", "Android", "Mobile")
    return "phone" if any(tag in ua for tag in mobile) else "pc"


def link_title_fallback(url: str) -> str:
    """Derive a display title from a URL's host, dropping a leading 'www.'."""
    netloc = urlparse(url).netloc
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or url


def normalize_url(raw: str) -> str:
    """Validate and normalize a submitted URL to an http(s) address.

    Trims whitespace, prepends https:// to a bare host (so "example.com" and
    "host:port" work), and rejects anything that is not http/https with a real
    host. A non-web scheme such as javascript:, data:, or file: is refused: with
    an authority (file://...) it is caught by the scheme check, and without one
    (javascript:alert(1)) prepending https:// leaves a non-numeric "port" that
    fails to parse.
    """
    url = (raw or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="A URL is required")
    if len(url) > LINKS_URL_MAX:
        raise HTTPException(status_code=400, detail="URL is too long")

    bad = HTTPException(status_code=400, detail="Only http and https URLs are allowed")
    if "://" in url:
        # An explicit scheme with an authority: it must be http or https.
        if urlparse(url).scheme not in ("http", "https"):
            raise bad
    else:
        # No authority: treat as a bare host and prepend https. A stray scheme
        # like "javascript:alert(1)" then parses to a bogus, non-numeric port.
        url = "https://" + url

    parsed = urlparse(url)
    try:
        parsed.port  # raises ValueError on a non-numeric port
    except ValueError:
        raise bad
    if not parsed.hostname:
        raise bad
    return url


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.post("/api/upload")
async def upload(request: Request, files: list[UploadFile] = File(...)):
    require_airbridge_header(request)
    saved = []
    for upload_file in files:
        name = sanitize(upload_file.filename or "upload.bin")
        dest = unique_path(cfg.shared_dir / name)
        written = 0
        exceeded = False
        with dest.open("wb") as out:
            while chunk := await upload_file.read(CHUNK):
                written += len(chunk)
                if cfg.max_bytes and written > cfg.max_bytes:
                    exceeded = True
                    break
                out.write(chunk)
        await upload_file.close()
        if exceeded:
            # Close happened on leaving the with block, so the partial file can
            # be removed on Windows before we report the failure.
            dest.unlink(missing_ok=True)
            raise HTTPException(
                status_code=413,
                detail=f"'{name}' exceeds the {cfg.max_mb} MB limit",
            )
        log_transfer(request, "UP", dest.name, dest.stat().st_size)
        saved.append(dest.name)
    return {"saved": saved, "count": len(saved)}


@app.get("/api/files")
async def list_files():
    items = []
    for path in cfg.shared_dir.iterdir():
        if not path.is_file():
            continue
        stat = path.stat()
        suffix = path.suffix.lower()
        items.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "previewable": suffix in PREVIEWABLE
                or (suffix in VIDEO_PREVIEWABLE and video_thumbnails_available()),
                "ext": path.suffix.lstrip(".").upper() or "FILE",
            }
        )
    items.sort(key=lambda item: item["modified"], reverse=True)
    return {
        "files": items,
        "thumbnails": thumbnails_available() or video_thumbnails_available(),
    }


@app.get("/api/raw/{name}")
async def raw(name: str):
    """Serve a file inline (for image previews on the page)."""
    path = resolve_in_shared(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, content_disposition_type="inline")


@app.get("/api/thumb/{name}")
async def thumb(name: str):
    """Serve a small JPEG thumbnail for an image or video, cached by mtime.

    Images fall back to serving the original file inline when the thumbnails
    extra is not installed or the image cannot be decoded (for example SVG),
    so a preview still appears and the client badge fallback still applies.
    Videos never fall back to the original (that would stream megabytes into
    an img tag that cannot render them); they 404 instead, which the client
    turns into a type badge.
    """
    path = resolve_in_shared(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    is_video = path.suffix.lower() in VIDEO_PREVIEWABLE
    if is_video:
        if not video_thumbnails_available():
            raise HTTPException(status_code=404, detail="No video thumbnails")
    elif not thumbnails_available():
        return FileResponse(path, content_disposition_type="inline")

    mtime_ns = path.stat().st_mtime_ns
    key = hashlib.sha1(f"{path.name}\x00{mtime_ns}".encode()).hexdigest()
    cache_path = DATA_DIR / "thumbs" / f"{key}.jpg"

    if not cache_path.is_file():
        try:
            if is_video:
                build_video_thumbnail(path, cache_path)
            else:
                build_thumbnail(path, cache_path)
        except Exception:
            if is_video:
                raise HTTPException(status_code=404, detail="Frame grab failed")
            # Unsupported format or decode error: serve the original inline.
            return FileResponse(path, content_disposition_type="inline")

    return FileResponse(
        cache_path, media_type="image/jpeg", content_disposition_type="inline"
    )


@app.get("/api/download/{name}")
async def download(request: Request, name: str):
    """Serve a file as an attachment (triggers a save on the phone)."""
    path = resolve_in_shared(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    log_transfer(request, "DOWN", path.name, path.stat().st_size)
    return FileResponse(path, filename=path.name)


@app.get("/api/download-all")
async def download_all(request: Request):
    files = [p for p in cfg.shared_dir.iterdir() if p.is_file()]
    if not files:
        raise HTTPException(status_code=404, detail="No files to download")

    # Build the archive on disk (not in memory) so large batches are safe,
    # then delete it once the response has been sent. Photos and videos are
    # already compressed, so store without re-compressing for speed.
    tmp = tempfile.NamedTemporaryFile(prefix="airbridge_", suffix=".zip", delete=False)
    tmp.close()
    total = 0
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as archive:
        for path in files:
            total += path.stat().st_size
            archive.write(path, arcname=path.name)
    log_transfer(request, "ALL", f"{len(files)} files", total)
    return FileResponse(
        tmp.name,
        filename="airbridge.zip",
        media_type="application/zip",
        background=BackgroundTask(os.unlink, tmp.name),
    )


@app.delete("/api/files/{name}")
async def delete_file(request: Request, name: str):
    require_airbridge_header(request)
    path = resolve_in_shared(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    path.unlink()
    log_transfer(request, "DEL", path.name)
    return {"deleted": path.name}


@app.post("/api/links")
async def create_link(request: Request):
    """Add a URL to the shared list. Both phone and PC post here."""
    require_airbridge_header(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Expected a JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")

    url = normalize_url(body.get("url", ""))
    title = str(body.get("title") or "").strip()[:LINKS_TITLE_MAX]
    submitted_source = body.get("source")
    source = submitted_source if submitted_source in ("phone", "pc") else link_source(request)

    link = {
        "id": uuid.uuid4().hex,
        "url": url,
        "title": title or link_title_fallback(url),
        "source": source,
        "created": time.time(),
    }
    with _links_lock:
        links = load_links()
        links.append(link)
        # Keep only the newest LINKS_MAX, dropping the oldest from the front.
        if len(links) > LINKS_MAX:
            links = links[-LINKS_MAX:]
        save_links(links)
    log_transfer(request, "LINK", link["url"])
    return link


@app.get("/api/links")
async def list_links():
    """Return the shared links, newest first."""
    with _links_lock:
        links = load_links()
    links.sort(key=lambda item: item.get("created", 0), reverse=True)
    return {"links": links}


@app.delete("/api/links/{link_id}")
async def delete_link(request: Request, link_id: str):
    require_airbridge_header(request)
    with _links_lock:
        links = load_links()
        remaining = [link for link in links if link.get("id") != link_id]
        if len(remaining) == len(links):
            raise HTTPException(status_code=404, detail="Not found")
        save_links(remaining)
    log_transfer(request, "DELL", link_id)
    return {"deleted": link_id}


# --------------------------------------------------------------------------- #
# Startup banner
# --------------------------------------------------------------------------- #
def ensure_self_signed(ip: str, cert_path: Path, key_path: Path) -> None:
    """Create a cached self-signed cert and key on first use, if missing.

    The Subject Alternative Name includes the LAN IP and 127.0.0.1, which iOS
    requires or it rejects the certificate. cryptography ships in the optional
    'tls' extra and is imported lazily so the core install stays lean.
    """
    if cert_path.exists() and key_path.exists():
        return
    try:
        import datetime as _dt
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        raise SystemExit(
            "HTTPS needs the optional 'tls' extra. "
            "Run: uv run --extra tls main.py --https"
        )

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AirBridge")])
    san = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address(ip)),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def get_lan_ip() -> str:
    """Best-effort LAN IP by inspecting the route toward the internet.

    No packets are actually sent on a UDP socket connect.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def print_banner(url: str, base: str, port: int) -> None:
    line = "=" * 56
    print()
    print(line)
    print("  AIRBRIDGE  ::  phone <-> desktop file transfer")
    print(line)
    print()
    print("  Scan this with your iPhone camera:")
    print()
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    try:
        qr.print_ascii(invert=True)
    except UnicodeEncodeError:
        # Some Windows consoles use cp1252 and cannot encode the block glyphs.
        # Fall back to just the URL below, which is printed regardless.
        print("  (QR code needs a UTF-8 console. Open the URL below instead.)")
    print()
    print(f"  Or open in Safari:  {url}")
    print(f"  Shared folder:      {cfg.shared_dir}")
    print(f"  Listening on:       {base} (port {port})")
    if cfg.auth_enabled:
        print("  Access:             token required (in the QR link)")
    else:
        print("  Access:             OPEN, no token (--no-auth)")
    print()
    print("  Phone and desktop must be on the same Wi-Fi network.")
    print("  Stop the server with Ctrl+C.")
    print(line)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="AirBridge LAN file transfer server")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on (default 8080, or AIRBRIDGE_PORT)",
    )
    parser.add_argument(
        "--dir",
        default=os.environ.get("AIRBRIDGE_DIR", str(DEFAULT_SHARED_DIR)),
        help="Shared folder for transferred files",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default 0.0.0.0, all interfaces)",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable the access token (anyone on the LAN can connect)",
    )
    parser.add_argument(
        "--max-mb",
        type=int,
        default=0,
        help="Per-file upload size cap in MB (default 0, meaning unlimited)",
    )
    parser.add_argument(
        "--https",
        action="store_true",
        help="Serve over HTTPS with a cached self-signed cert (needs the 'tls' extra)",
    )
    args = parser.parse_args()
    port = (
        args.port
        if args.port is not None
        else int(os.environ.get("AIRBRIDGE_PORT", "8080"))
    )

    cfg.shared_dir = Path(args.dir).expanduser().resolve()
    cfg.shared_dir.mkdir(parents=True, exist_ok=True)
    cfg.auth_enabled = not args.no_auth
    cfg.token = None if args.no_auth else secrets.token_urlsafe(9)
    cfg.max_mb = max(0, args.max_mb)
    cfg.max_bytes = cfg.max_mb * 1024 * 1024

    ip = get_lan_ip()
    ssl_kwargs: dict[str, str] = {}
    scheme = "http"
    if args.https:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        cert_path = DATA_DIR / "cert.pem"
        key_path = DATA_DIR / "key.pem"
        ensure_self_signed(ip, cert_path, key_path)
        ssl_kwargs = {"ssl_certfile": str(cert_path), "ssl_keyfile": str(key_path)}
        scheme = "https"

    base = f"{scheme}://{ip}:{port}"
    url = base + (f"/?t={cfg.token}" if cfg.auth_enabled else "/")

    print_banner(url, base, port)
    uvicorn.run(app, host=args.host, port=port, log_level="warning", **ssl_kwargs)


if __name__ == "__main__":
    main()
