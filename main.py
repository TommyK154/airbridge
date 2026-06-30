"""AirBridge: a LAN photo and file bridge between a phone and a desktop.

Run on the desktop. The phone connects over the same Wi-Fi via a browser,
no app required. On startup a QR code is printed to the terminal; scan it
with the phone camera and the transfer page opens.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import socket
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

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
WEB_DIR = BASE_DIR / "web"
CHUNK = 1 << 20  # 1 MiB streaming chunk
SESSION_COOKIE = "airbridge_session"

# File suffixes worth offering an inline preview for. The browser does the
# rendering, so a failed load falls back to a type badge on the client side.
PREVIEWABLE = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".heic", ".heif", ".avif", ".svg",
}

# NTFS forbids these characters in a filename, plus ASCII control chars.
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Reserved Windows device names (case-insensitive), checked against the stem.
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"}
_RESERVED_NAMES |= {f"COM{i}" for i in range(1, 10)}
_RESERVED_NAMES |= {f"LPT{i}" for i in range(1, 10)}
MAX_NAME_LEN = 200


class Config:
    """Runtime configuration, populated in main()."""

    shared_dir: Path = BASE_DIR / "shared"
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
        items.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "previewable": path.suffix.lower() in PREVIEWABLE,
                "ext": path.suffix.lstrip(".").upper() or "FILE",
            }
        )
    items.sort(key=lambda item: item["modified"], reverse=True)
    return {"files": items}


@app.get("/api/raw/{name}")
async def raw(name: str):
    """Serve a file inline (for image previews on the page)."""
    path = resolve_in_shared(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, content_disposition_type="inline")


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


# --------------------------------------------------------------------------- #
# Startup banner
# --------------------------------------------------------------------------- #
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
        default=int(os.environ.get("AIRBRIDGE_PORT", "8080")),
        help="Port to listen on (default 8080)",
    )
    parser.add_argument(
        "--dir",
        default=os.environ.get("AIRBRIDGE_DIR", str(BASE_DIR / "shared")),
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
    args = parser.parse_args()

    cfg.shared_dir = Path(args.dir).expanduser().resolve()
    cfg.shared_dir.mkdir(parents=True, exist_ok=True)
    cfg.auth_enabled = not args.no_auth
    cfg.token = None if args.no_auth else secrets.token_urlsafe(9)
    cfg.max_mb = max(0, args.max_mb)
    cfg.max_bytes = cfg.max_mb * 1024 * 1024

    ip = get_lan_ip()
    base = f"http://{ip}:{args.port}"
    url = base + (f"/?t={cfg.token}" if cfg.auth_enabled else "/")

    print_banner(url, base, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
