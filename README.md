# AirBridge

A LAN-only file and photo bridge between an iPhone and a Windows 10 desktop. A
small FastAPI server runs on the PC; the phone connects over the same Wi-Fi
through a browser, with no app to install. Phone uploads land in a shared folder,
and the phone can download anything in that folder.

Nothing leaves your local network. There is no cloud, no account, and no WAN
exposure.

## Requirements

- Windows 10 (or newer), tested natively on Windows, not WSL2.
- [uv](https://docs.astral.sh/uv/) on your PATH. There is no separate `python` or
  `pip` step; uv manages the environment and installs dependencies on first run.
- The phone and the PC must be on the same Wi-Fi network.

## Quick start

Double-click `run.bat`, or from a terminal in the project folder:

```
uv run main.py
```

On startup the terminal prints a QR code and a URL. Scan the QR with the iPhone
camera (or open the URL in Safari) and the transfer page loads. The first launch
installs dependencies automatically.

By default access is gated by a token embedded in the QR link. The phone exchanges
it for a session cookie, so only devices that scanned the code can connect.

### Running without the token

For quick local testing on a trusted network you can disable the token:

```
uv run main.py --no-auth
```

Anyone on the LAN can then connect, so use this only when you trust the network.

## Command-line options

| Flag | Default | Description |
| --- | --- | --- |
| `--port N` | `8080` | Port to listen on. |
| `--dir PATH` | `./shared` | Shared folder for transferred files. |
| `--host ADDR` | `0.0.0.0` | Bind address (all interfaces by default). |
| `--no-auth` | off | Disable the access token (anyone on the LAN can connect). |
| `--max-mb N` | `0` | Per-file upload size cap in MB. `0` means unlimited. Uploads over the cap are rejected with HTTP 413 and leave nothing on disk. |
| `--https` | off | Serve over HTTPS with a cached self-signed certificate (needs the `tls` extra, see below). |

`--port` and `--dir` can also be set with the `AIRBRIDGE_PORT` and `AIRBRIDGE_DIR`
environment variables.

## Downloading files

Each file in the "Files on the PC" list has its own download button. For
downloading many files, or large ones, use **Download all** instead: it streams a
single ZIP over one connection, which is faster and more reliable than tapping
several individual downloads at once. (On iOS, firing many large downloads
simultaneously can stall one of the connections.)

## Uploading files

Drag files onto the Send panel, or tap to pick them. A batch uploads with a small
concurrency pool (a few files at a time) with per-file progress. If one file
fails, the rest of the batch still completes.

While a batch is transferring, AirBridge uses the Screen Wake Lock API where the
browser supports it (for example Safari on iOS) to keep the phone screen awake so
a long transfer is not interrupted when the screen would otherwise lock. Browsers
without the API are unaffected.

## Optional HTTPS

On a home LAN, plain HTTP is fine. On a network you do not fully control, you can
encrypt the link. HTTPS support lives in an optional extra so the core install
stays lean:

```
uv run --extra tls main.py --https
```

On first use this generates a self-signed certificate and key, caches them under
a gitignored `.airbridge/` folder, and reuses them on later runs. The certificate
includes the PC's LAN IP in its Subject Alternative Name, which iOS requires.

The first time the phone connects, Safari shows a one-time warning because the
certificate is self-signed (not issued by a public authority). This is expected.
Tapping through to continue still gives you full wire encryption between the phone
and the PC.

If the PC's LAN IP changes, the cached certificate no longer matches and iOS will
reject it. See the DHCP reservation tip below to keep the IP stable.

## Tips

- **Keep the URL stable:** set a DHCP reservation for the PC in your router so it
  always gets the same LAN IP. The connect URL (and, with `--https`, the cached
  certificate) then stay valid across reboots.
- **QR code looks blank:** some Windows consoles default to the cp1252 codepage
  and cannot render the QR block characters. `run.bat` sets `PYTHONIOENCODING=utf-8`
  so the QR renders. If you launch `uv run main.py` directly in such a console,
  set that variable first, or just open the printed URL instead.

## Troubleshooting

- **The phone cannot reach the PC:** confirm both devices are on the same Wi-Fi
  network (not a guest network or a separate band that blocks client-to-client
  traffic).
- **Windows Defender Firewall prompt:** allow AirBridge on private networks the
  first time it binds the port, or the phone will not be able to connect.
- **Connection refused:** check the port is not already in use, and that you
  opened the same port shown in the startup banner.

## Security posture

AirBridge is strictly a LAN tool. It keeps the token check, uses `SameSite=Lax`
session cookies plus a custom-header check on state-changing requests, sets a
tight Content-Security-Policy and other security headers, sanitizes all uploaded
filenames for Windows, and never serves a path outside the shared folder. It does
nothing to invite WAN exposure (no UPnP, no port forwarding, no public tunnels).
