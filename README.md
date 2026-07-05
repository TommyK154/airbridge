# AirBridge

Move photos and files between your iPhone and your Windows PC over your own
Wi-Fi. No app to install on the phone, no cloud, no account: a tiny server runs
on the PC, the phone connects through its browser, and nothing ever leaves your
local network.

## Install (Windows)

1. Download `AirBridge-Setup-<version>.exe` from the
   [latest release](../../releases/latest).
2. Run it. Windows SmartScreen may show "Windows protected your PC" because the
   app is not code-signed; click **More info**, then **Run anyway**. This
   happens once.
3. Click through the installer. On the last page, leave "Launch AirBridge now"
   checked.

That's it. An AirBridge icon appears in the system tray (bottom-right, possibly
behind the `^` chevron), the server starts by itself, and a window pops up with
a QR code. Point the iPhone camera at it and the transfer page opens in Safari.

Day-to-day use:

- **Left-click** the tray icon: show the QR code again.
- **Right-click**: Start/Stop Server, Show QR, Open in Browser, Open Shared
  Folder, run-at-login toggle, Exit.
- Files from the phone land in `C:\Users\<you>\AirBridge` (one click away via
  **Open Shared Folder**). Anything you drop in that folder can be downloaded
  by the phone.
- Each time the server starts it generates a fresh access link, so scan the QR
  again after a restart.

The first time the server runs, Windows Defender Firewall asks to allow
AirBridge on private networks; allow it, or the phone cannot connect.

## What it does

- Bidirectional transfers: phone to PC uploads, PC to phone downloads, and a
  one-tap ZIP of everything.
- Image previews and HEIC thumbnails in the file list.
- A Links tab for tossing URLs between the phone and the PC.
- Parallel uploads with per-file progress, and a screen wake lock so long
  transfers are not interrupted by the phone locking.
- Access control by default: a token embedded in the QR link is exchanged for a
  session cookie, so only devices that scanned the code can connect.

## Security posture

AirBridge is strictly a LAN tool. It keeps the token check, uses `SameSite=Lax`
session cookies plus a custom-header check on state-changing requests, sets a
tight Content-Security-Policy and other security headers, sanitizes all uploaded
filenames for Windows, and never serves a path outside the shared folder. It does
nothing to invite WAN exposure (no UPnP, no port forwarding, no public tunnels).

## Troubleshooting

- **The phone cannot reach the PC:** confirm both devices are on the same Wi-Fi
  network (not a guest network or a band that blocks client-to-client traffic).
- **No QR window and no tray icon:** check the log at
  `%LOCALAPPDATA%\AirBridge\tray.log`.
- **Port already taken:** AirBridge picks the next free port automatically; the
  QR code and URL always reflect the real address.
- **Keep the URL stable:** set a DHCP reservation for the PC in your router so
  it always gets the same LAN IP.

---

## Running from source (developers)

Everything below is for people working on AirBridge itself. Installed users
never need any of it.

Requirements: Windows, [uv](https://docs.astral.sh/uv/) on PATH. There is no
separate `python` or `pip` step; uv manages the environment.

```
uv run main.py              # headless server with QR in the terminal (run.bat)
tray_run.bat                # the tray app, from source
uv run pytest               # tests
```

`main.py` prints the QR and serves until Ctrl+C. The tray app runs the same
server in the background with start/stop, QR popup, and login-autostart
toggles; from source it detaches from the console (use `--foreground` to keep
it attached for debugging) and logs to `.airbridge/tray.log` in the project
directory.

### Command-line options (both entry points)

| Flag | Default | Description |
| --- | --- | --- |
| `--port N` | `8080` | Port to listen on. The tray falls back to the next free port when the default is busy. |
| `--dir PATH` | `./shared` | Shared folder for transferred files. |
| `--host ADDR` | `0.0.0.0` | Bind address (all interfaces by default). |
| `--no-auth` | off | Disable the access token (anyone on the LAN can connect). |
| `--max-mb N` | `0` | Per-file upload size cap in MB. `0` means unlimited; over-cap uploads get HTTP 413 and leave nothing on disk. |
| `--https` | off | Serve over HTTPS with a cached self-signed certificate (needs the `tls` extra). iOS shows a one-time warning for the self-signed cert. |

`--port` and `--dir` can also be set with the `AIRBRIDGE_PORT` and
`AIRBRIDGE_DIR` environment variables.

### Optional extras

| Extra | Packages | Enables |
| --- | --- | --- |
| `tls` | cryptography | `--https`. |
| `thumbnails` | pillow, pillow-heif | Image and HEIC thumbnails in the file list. |
| `tray` | pystray, pillow | The `tray.py` entry point. |

Combine with `uv run --extra tls --extra thumbnails main.py --https`, or use
`--all-extras` for everything (which is what `tray_run.bat` does). The installed
app bundles all extras, so none of this applies to it.

### Building the installer

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

## License

[MIT](LICENSE)
