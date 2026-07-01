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

## File map

- `main.py` ............ FastAPI app: auth middleware, endpoints, QR banner, CLI.
- `web/index.html` ..... the entire UI (HTML, CSS, JS inline).
- `pyproject.toml` ..... uv project and dependencies.
- `run.bat` ............ Windows launcher (`uv run main.py %*`).
- `README.md` .......... setup, usage, troubleshooting.
