"""AirBridge: a LAN photo and file bridge between a phone and a desktop.

Run on the desktop. The phone connects over the same Wi-Fi via a browser,
no app required. On startup a QR code is printed to the terminal; scan it
with the phone camera and the transfer page opens.
"""
from __future__ import annotations

import argparse
import os
import secrets
import socket
import tempfile
import zipfile
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


class Config:
    """Runtime configuration, populated in main()."""

    shared_dir: Path = BASE_DIR / "shared"
    token: str | None = None
    auth_enabled: bool = True


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
# Helpers
# --------------------------------------------------------------------------- #
def sanitize(name: str) -> str:
    """Reduce a client filename to a safe basename."""
    name = Path(name).name.strip()
    if not name or name in {".", ".."}:
        return "upload.bin"
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


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    saved = []
    for upload_file in files:
        name = sanitize(upload_file.filename or "upload.bin")
        dest = unique_path(cfg.shared_dir / name)
        with dest.open("wb") as out:
            while chunk := await upload_file.read(CHUNK):
                out.write(chunk)
        await upload_file.close()
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
async def download(name: str):
    """Serve a file as an attachment (triggers a save on the phone)."""
    path = resolve_in_shared(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, filename=path.name)


@app.get("/api/download-all")
async def download_all():
    files = [p for p in cfg.shared_dir.iterdir() if p.is_file()]
    if not files:
        raise HTTPException(status_code=404, detail="No files to download")

    # Build the archive on disk (not in memory) so large batches are safe,
    # then delete it once the response has been sent. Photos and videos are
    # already compressed, so store without re-compressing for speed.
    tmp = tempfile.NamedTemporaryFile(prefix="airbridge_", suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as archive:
        for path in files:
            archive.write(path, arcname=path.name)
    return FileResponse(
        tmp.name,
        filename="airbridge.zip",
        media_type="application/zip",
        background=BackgroundTask(os.unlink, tmp.name),
    )


@app.delete("/api/files/{name}")
async def delete_file(name: str):
    path = resolve_in_shared(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    path.unlink()
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
    qr.print_ascii(invert=True)
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
    args = parser.parse_args()

    cfg.shared_dir = Path(args.dir).expanduser().resolve()
    cfg.shared_dir.mkdir(parents=True, exist_ok=True)
    cfg.auth_enabled = not args.no_auth
    cfg.token = None if args.no_auth else secrets.token_urlsafe(9)

    ip = get_lan_ip()
    base = f"http://{ip}:{args.port}"
    url = base + (f"/?t={cfg.token}" if cfg.auth_enabled else "/")

    print_banner(url, base, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
