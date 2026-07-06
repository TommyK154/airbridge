# CLAUDE.md

Project guide for AirBridge. Read this before making changes.

## What this is

A LAN-only file and photo bridge between an iPhone and a Windows 10 desktop. A
small FastAPI server runs on the PC; the phone connects over the same Wi-Fi
through a browser (no app). Bidirectional: phone uploads land in a shared folder,
and the phone can download anything in that folder.

## Platform

This is a Windows-side project. It uses Windows uv, binds a listening socket,
and is meant to pass through Windows Defender Firewall. Run and test it natively
on Windows (PowerShell), not under WSL2.

## How to run and test

- Run: `uv run main.py` (first launch installs deps automatically). Add `--no-auth`
  for quick local testing without the token.
- There is no `python` or `pip` on this machine. Always use uv: `uv run ...`,
  `uv pip install ...`, `uv venv ...`. Translate any vanilla Python command to its
  uv equivalent without being asked.
- Manual test loop: start with `--no-auth --port 8099 --dir ./testshare`, then
  exercise the endpoints with curl (upload, list, download, download-all, delete)
  before testing on the phone.
- Automated tests: `uv run pytest` (tests/test_smoke.py, no GUI coverage).

## Conventions

- Never use em dashes in code comments, docs, commit messages, or any output.
  Use periods, commas, parentheses, or colons instead.
- Keep the dependency surface small. Prefer the standard library. New runtime
  dependencies need a clear reason, and anything heavy or optional goes in
  `[project.optional-dependencies]`, not the core list.
- Security posture: this is strictly a LAN tool. Never add anything that invites
  WAN exposure (no UPnP, no port-forward helpers, no public tunnels). Keep the
  token check, keep `SameSite=Lax` cookies, sanitize all filenames, and never
  serve a path outside the shared directory.
- The web UI is a single self-contained `web/index.html` with no external assets
  (no CDN fonts or scripts). Keep it that way so the page loads instantly and
  offline, and so a tight Content-Security-Policy stays possible.
- Match the existing style: type hints, small focused functions, docstrings on
  non-obvious behavior.

## Frozen vs source paths

The app also ships as a frozen PyInstaller exe (built by CI). `main.py` defines
the split: frozen builds keep writable state in `%LOCALAPPDATA%\AirBridge` and
share `%USERPROFILE%\AirBridge`; from source, state stays in the project dir
(`.airbridge/`, `./shared`). Use `DATA_DIR`, `WEB_DIR`, `DEFAULT_SHARED_DIR`,
and `FROZEN` from `main.py` rather than building paths from `BASE_DIR`.

## File map

- `main.py` ............ FastAPI app: auth middleware, endpoints, QR banner, CLI.
- `web/index.html` ..... the entire UI (HTML, CSS, JS inline).
- `tray.py` ............ system tray entry point (server thread, QR popup, login toggles).
- `pyproject.toml` ..... uv project, dependencies, extras, dev group.
- `run.bat` ............ Windows launcher (`uv run main.py %*`).
- `tray_run.bat` ....... tray launcher (syncs all extras, tray.py self-detaches).
- `tests/` ............. pytest smoke tests (helpers, registry, server lifecycle).
- `airbridge.spec` ..... PyInstaller build (onedir, windowed, bundles web/ and ffmpeg).
- `installer.iss` ...... Inno Setup installer (per-user, start-at-login task).
- `build_assets/` ...... make_ico.py, generates the .ico from tray.make_icon.
- `.github/workflows/` . ci.yml (tests) and release.yml (installer on v* tags).
- `README.md` .......... end users only: install, everyday use, kept Apple-clean.
- `DEVELOPING.md` ...... developer docs: source run, CLI flags, extras, build, release.
