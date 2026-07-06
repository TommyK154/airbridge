"""Smoke tests for AirBridge: pure helpers, config, registry, and a real
server lifecycle on loopback. No GUI is exercised (pystray and tkinter run
only in manual testing).

Run with: uv run pytest
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import urllib.request
import uuid
import winreg
from pathlib import Path

import pytest
from fastapi import HTTPException

import main as airbridge
import tray


def make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        port=None,
        dir="./testshare",
        host="127.0.0.1",
        no_auth=True,
        max_mb=0,
        https=False,
        autostart=False,
        foreground=True,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# --------------------------------------------------------------------------- #
# Pure helpers in main.py
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a:b*c?.jpg", "a_b_c_.jpg"),
        ("CON.txt", "_CON.txt"),
        ("trailing. ", "trailing"),
        ("", "upload.bin"),
        ("..", "upload.bin"),
        ("../../etc/passwd", "passwd"),
        ("normal name.png", "normal name.png"),
    ],
)
def test_sanitize(raw: str, expected: str) -> None:
    assert airbridge.sanitize(raw) == expected


def test_sanitize_caps_length() -> None:
    name = airbridge.sanitize("x" * 500 + ".jpg")
    assert len(name) <= airbridge.MAX_NAME_LEN
    assert name.endswith(".jpg")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "https://example.com"),
        ("http://a.b/c?d=1", "http://a.b/c?d=1"),
        ("  https://x.io  ", "https://x.io"),
    ],
)
def test_normalize_url_accepts(raw: str, expected: str) -> None:
    assert airbridge.normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw", ["javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", ""]
)
def test_normalize_url_rejects(raw: str) -> None:
    with pytest.raises(HTTPException):
        airbridge.normalize_url(raw)


def test_unique_path(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    first.write_text("x")
    second = airbridge.unique_path(first)
    assert second.name == "a (1).txt"


def test_human_size() -> None:
    assert airbridge.human_size(0) == "0 B"
    assert airbridge.human_size(1536) == "1.5 KB"


# --------------------------------------------------------------------------- #
# Tray config, registry, and login command
# --------------------------------------------------------------------------- #
def test_config_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tray, "CONFIG_PATH", tmp_path / "config.json")
    config = tray.load_config()
    assert config["start_server_on_login"] is False
    config["start_server_on_login"] = True
    tray.save_config(config)
    assert tray.load_config()["start_server_on_login"] is True


def test_load_config_survives_corrupt_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(tray, "CONFIG_PATH", path)
    assert tray.load_config()["start_server_on_login"] is False


def test_login_command_quotes_spaced_dir() -> None:
    args = make_args(dir=r"C:\My Files\share", port=8099)
    command = tray.login_command(args, start_server=True)
    assert '"C:\\My Files\\share"' in command
    assert "--autostart" in command
    assert "--port 8099" in command


def test_login_command_omits_default_port() -> None:
    command = tray.login_command(make_args(), start_server=False)
    assert "--port" not in command
    assert "--autostart" not in command


def test_registry_round_trip(monkeypatch) -> None:
    value_name = f"AirBridgeTest-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(tray, "RUN_VALUE", value_name)
    assert tray.login_entry_exists() is False
    tray.set_run_value("test-command")
    try:
        assert tray.login_entry_exists() is True
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, tray.RUN_KEY) as key:
            stored, kind = winreg.QueryValueEx(key, value_name)
        assert stored == "test-command" and kind == winreg.REG_SZ
    finally:
        tray.delete_run_value()
    assert tray.login_entry_exists() is False
    tray.delete_run_value()  # idempotent on a missing value


# --------------------------------------------------------------------------- #
# Port fallback and server lifecycle
# --------------------------------------------------------------------------- #
def test_find_free_port_skips_busy() -> None:
    # Listen (not just bind): a listening socket is what a real port conflict
    # looks like, and on Windows only exclusive-use probing detects it.
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy = blocker.getsockname()[1]
    try:
        chosen = tray.find_free_port("127.0.0.1", busy)
        assert chosen != busy
    finally:
        blocker.close()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_on_free_port(controller: tray.ServerController, attempts: int = 3) -> None:
    """Start the server, retrying with a new port if another process grabbed
    the probed one first (a real race on busy CI runners)."""
    for _ in range(attempts - 1):
        try:
            controller.start()
            return
        except RuntimeError:
            controller.args.port = free_port()
    controller.start()


def test_server_lifecycle(tmp_path: Path) -> None:
    controller = tray.ServerController(make_args(port=free_port(), dir=str(tmp_path)))
    start_on_free_port(controller)
    try:
        assert controller.running
        assert controller.port == controller.args.port
        port = controller.port

        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(f"{base}/api/files", timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["files"] == []

        # Upload one file through the real endpoint, then delete it.
        boundary = "testboundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="hi.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "hello airbridge\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        request = urllib.request.Request(
            f"{base}/api/upload",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-AirBridge": "1",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            assert json.loads(resp.read())["saved"] == ["hi.txt"]
        assert (tmp_path / "hi.txt").read_text() == "hello airbridge"

        delete = urllib.request.Request(
            f"{base}/api/files/hi.txt",
            method="DELETE",
            headers={"X-AirBridge": "1"},
        )
        with urllib.request.urlopen(delete, timeout=5) as resp:
            assert resp.status == 200
    finally:
        controller.stop()
    assert not controller.running

    # A stopped controller can start again. Use a fresh port: rebinding the
    # one just released can flake on busy CI runners.
    controller.args.port = free_port()
    start_on_free_port(controller)
    assert controller.running
    controller.stop()
    assert not controller.running


# --------------------------------------------------------------------------- #
# Video thumbnails (optional videothumbs extra; skipped when not installed)
# --------------------------------------------------------------------------- #
def make_test_video(dest: Path) -> None:
    """Render a half-second synthetic clip with the bundled ffmpeg.

    Kept under one second on purpose: it also exercises the retry path in
    build_video_thumbnail where the one-second seek finds no frame.
    """
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=0.5:size=64x48:rate=10",
            str(dest),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def test_build_video_thumbnail(tmp_path: Path) -> None:
    pytest.importorskip("imageio_ffmpeg")
    video = tmp_path / "clip.mp4"
    make_test_video(video)
    dest = tmp_path / "thumb.jpg"
    airbridge.build_video_thumbnail(video, dest)
    assert dest.read_bytes()[:2] == b"\xff\xd8"  # JPEG magic bytes


def test_build_video_thumbnail_rejects_garbage(tmp_path: Path) -> None:
    pytest.importorskip("imageio_ffmpeg")
    fake = tmp_path / "fake.mov"
    fake.write_text("not a video")
    dest = tmp_path / "thumb.jpg"
    with pytest.raises(Exception):
        airbridge.build_video_thumbnail(fake, dest)
    assert not dest.exists()


def test_videos_not_previewable_without_extra(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "clip.mov").write_bytes(b"x")
    monkeypatch.setattr(airbridge.cfg, "shared_dir", tmp_path)
    monkeypatch.setattr(airbridge, "video_thumbnails_available", lambda: False)
    data = asyncio.run(airbridge.list_files())
    assert data["files"][0]["previewable"] is False


def test_video_thumbnail_endpoint(tmp_path: Path) -> None:
    pytest.importorskip("imageio_ffmpeg")
    make_test_video(tmp_path / "clip.mp4")
    controller = tray.ServerController(make_args(port=free_port(), dir=str(tmp_path)))
    start_on_free_port(controller)
    try:
        base = f"http://127.0.0.1:{controller.port}"
        with urllib.request.urlopen(f"{base}/api/files", timeout=5) as resp:
            data = json.loads(resp.read())
        assert data["thumbnails"] is True
        assert data["files"][0]["previewable"] is True

        with urllib.request.urlopen(f"{base}/api/thumb/clip.mp4", timeout=30) as resp:
            assert resp.status == 200
            assert resp.headers["content-type"] == "image/jpeg"
            assert resp.read()[:2] == b"\xff\xd8"
    finally:
        controller.stop()


# The server thread raising on the busy port is the behavior under test.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_start_reports_busy_port() -> None:
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy = blocker.getsockname()[1]
    try:
        controller = tray.ServerController(make_args(port=busy))
        with pytest.raises(RuntimeError, match=str(busy)):
            controller.start()
    finally:
        blocker.close()
