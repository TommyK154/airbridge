# Developing AirBridge

Everything here is for people working on AirBridge itself. Installed users
never need any of it.

Requirements: Windows, [uv](https://docs.astral.sh/uv/) on PATH. There is no
separate `python` or `pip` step; uv manages the environment.

```
uv run main.py              # headless server with QR in the terminal (run.bat)
tray_run.bat                # the tray app, from source
uv run --all-extras pytest  # tests (all extras, so nothing is skipped)
```

`main.py` prints the QR and serves until Ctrl+C. The tray app runs the same
server in the background with start/stop, QR popup, and login-autostart
toggles; from source it detaches from the console (use `--foreground` to keep
it attached for debugging) and logs to `.airbridge/tray.log` in the project
directory.

## Command-line options (both entry points)

| Flag | Default | Description |
| --- | --- | --- |
| `--port N` | `8080` | Port to listen on. The tray falls back to the next free port when the default is busy. |
| `--dir PATH` | `./shared` | Shared folder for transferred files. |
| `--host ADDR` | `0.0.0.0` | Bind address (all interfaces by default). |
| `--no-auth` | off | Disable the access token (anyone on the LAN can connect). |
| `--max-mb N` | `0` | Per-file upload size cap in MB. `0` means unlimited; over-cap uploads get HTTP 413 and leave nothing on disk. |
| `--https` | off | Serve over HTTPS with a cached self-signed certificate (needs the `tls` extra). iOS shows a one-time warning for the self-signed cert. HTTPS also enables the Save to Photos share button on iPhone. |

`--port` and `--dir` can also be set with the `AIRBRIDGE_PORT` and
`AIRBRIDGE_DIR` environment variables.

## Optional extras

| Extra | Packages | Enables |
| --- | --- | --- |
| `tls` | cryptography | `--https`. |
| `thumbnails` | pillow, pillow-heif | Image and HEIC thumbnails in the file list. |
| `videothumbs` | imageio-ffmpeg | Video thumbnails (a frame grabbed by the bundled ffmpeg). |
| `tray` | pystray, pillow | The `tray.py` entry point. |

Combine with `uv run --extra tls --extra thumbnails main.py --https`, or use
`--all-extras` for everything (which is what `tray_run.bat` does). The
installed app bundles all extras, so none of this applies to it.

## Security posture

AirBridge is strictly a LAN tool. It keeps the token check, uses `SameSite=Lax`
session cookies plus a custom-header check on state-changing requests, sets a
tight Content-Security-Policy and other security headers, sanitizes all
uploaded filenames for Windows, and never serves a path outside the shared
folder. It does nothing to invite WAN exposure (no UPnP, no port forwarding,
no public tunnels). Keep it that way.

## Building the installer

CI does this on every version tag (see `.github/workflows/release.yml`):
PyInstaller bundles the tray app, then Inno Setup wraps it in
`AirBridge-Setup-<version>.exe`. Locally:

```
uv run python build_assets/make_ico.py
uv run pyinstaller airbridge.spec --noconfirm
ISCC.exe installer.iss /DAppVersion=1.0.0
```

The frozen app stores its state in `%LOCALAPPDATA%\AirBridge` and shares
`%USERPROFILE%\AirBridge`; from source, state stays in the project directory
(`.airbridge/` and `./shared`).

To release: bump `version` in `pyproject.toml`, tag `vX.Y.Z`, push the tag.
CI runs the tests, builds the installer, and attaches it to a GitHub Release.
